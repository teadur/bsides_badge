# Badge workflows for one or more badges on USB serial ports.
#
#   make upload       upload to every badge in PORTS
#   make badge-test   Wi-Fi test on all badges, pull their logs, write a report
#   make wifi-check   upload, then badge-test
#
# Override on the command line, e.g.
#   make upload PORTS=/dev/ttyACM1 BADGE_VERSION=2026
#   make upload NO_COMPILE=1          upload .py sources instead of .mpy
#   make logs CLEAR=1

PORTS ?= /dev/ttyACM0 /dev/ttyACM1
PY ?= uv run python3
BADGE = $(PY) scripts/badge.py
LOG_DIR = badge-logs
RUN_STAMP = $(LOG_DIR)/.run-start
BADGE_VERSION ?=
CLEAR ?=
NO_COMPILE ?=
PIP_INSTALL ?= uv pip install

VERSION_ARG = $(if $(BADGE_VERSION),--badge-version $(BADGE_VERSION))
CLEAR_ARG = $(if $(CLEAR),--clear)
COMPILE_ARG = $(if $(NO_COMPILE),--no-compile)

.DEFAULT_GOAL := help
.PHONY: help tools upload logs wifi-test badge-test report wifi-check test

help:
	@echo "Targets (PORTS=\"$(PORTS)\"):"
	@echo "  tools       install esptool, mpremote and mpy-cross ($(PIP_INSTALL))"
	@echo "  upload      upload precompiled application files; hardware version"
	@echo "              is detected (BADGE_VERSION=2026 overrides, NO_COMPILE=1"
	@echo "              uploads .py sources)"
	@echo "  badge-test  run the Wi-Fi test on all badges at once, save their"
	@echo "              link logs, and bundle the run into one report file"
	@echo "  wifi-check  upload, then badge-test"
	@echo "  wifi-test   only the Wi-Fi test"
	@echo "  logs        only save each badge's link log to $(LOG_DIR)/"
	@echo "              (CLEAR=1 also deletes it on the badge)"
	@echo "  report      bundle the files of the last badge-test run again"
	@echo "  test        run the unit tests"

tools:
	$(PIP_INSTALL) esptool mpremote mpy-cross

upload:
	@set -e; for port in $(PORTS); do \
		echo "=== upload $$port"; \
		$(BADGE) upload --port $$port $(VERSION_ARG) $(COMPILE_ARG); \
	done

logs:
	@set -e; for port in $(PORTS); do \
		echo "=== logs $$port"; \
		$(BADGE) logs --port $$port $(CLEAR_ARG); \
	done

# Both badges must run at the same time, so start them in parallel and keep
# each side's output in $(LOG_DIR)/wifi-test-<port>.txt.
wifi-test:
	@mkdir -p $(LOG_DIR); \
	for port in $(PORTS); do \
		out=$(LOG_DIR)/wifi-test-$$(basename $$port).txt; \
		( $(PY) -m mpremote connect $$port \
			run tests/tictactoe_wifi_pair_hardware.py 2>&1 \
			| tee $$out | sed "s|^|[$$(basename $$port)] |" ) & \
	done; \
	wait; \
	status=0; \
	for port in $(PORTS); do \
		out=$(LOG_DIR)/wifi-test-$$(basename $$port).txt; \
		if grep -q "TTT-WIFI .*: PASS" $$out; then \
			echo "PASS $$port"; \
		else \
			echo "FAIL $$port (see $$out)"; status=1; \
		fi; \
	done; \
	exit $$status

# The logs and the report are collected even when the test fails; that is
# when they matter most. The exit status is the test's.
badge-test:
	@mkdir -p $(LOG_DIR); touch $(RUN_STAMP)
	@$(MAKE) --no-print-directory wifi-test; status=$$?; \
	$(MAKE) --no-print-directory logs; \
	$(MAKE) --no-print-directory report; \
	exit $$status

# One file to attach: a PASS/FAIL summary, then every test output and link
# log written since the last badge-test started.
report:
	@test -f $(RUN_STAMP) || { echo "No badge-test run yet."; exit 1; }; \
	files="$$(find $(LOG_DIR) -maxdepth 1 -newer $(RUN_STAMP) -name 'wifi-test-*.txt' | sort) \
		$$(find $(LOG_DIR) -maxdepth 1 -newer $(RUN_STAMP) -name 'linklog-*.txt' | sort)"; \
	out=$(LOG_DIR)/report-$$(date +%Y%m%d-%H%M%S).txt; \
	{ \
		echo "# badge test report $$(date '+%Y-%m-%d %H:%M:%S')"; \
		echo "# git $$(git rev-parse --short HEAD 2>/dev/null) $$(git branch --show-current 2>/dev/null)"; \
		echo "# ports $(PORTS)"; \
		for f in $$files; do \
			case $$f in *wifi-test-*) \
				result=$$(grep -o "TTT-WIFI .*: \(PASS\|FAIL.*\)" $$f | tail -1); \
				echo "# $$(basename $$f .txt): $${result:-no result}";; \
			esac; \
		done; \
		for f in $$files; do echo; echo "===== $$f"; cat $$f; done; \
	} > $$out; \
	echo "Report: $$out ($$(echo $$files | wc -w) files)"

wifi-check: upload
	@$(MAKE) --no-print-directory badge-test

test:
	$(PY) -m unittest discover -s tests

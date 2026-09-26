# Badge workflows for one or more badges on USB serial ports.
#
#   make upload                      upload to every badge in PORTS
#   make logs                        save each badge's Wi-Fi link log
#   make wifi-test                   scripted tic-tac-toe game over Wi-Fi only
#   make wifi-check                  upload, wifi-test, then logs
#
# Override on the command line, e.g.
#   make upload PORTS=/dev/ttyACM1 BADGE_VERSION=2026
#   make logs CLEAR=1

PORTS ?= /dev/ttyACM0 /dev/ttyACM1
PY ?= uv run python3
BADGE = $(PY) scripts/badge.py
LOG_DIR = badge-logs
BADGE_VERSION ?=
CLEAR ?=

VERSION_ARG = $(if $(BADGE_VERSION),--badge-version $(BADGE_VERSION))
CLEAR_ARG = $(if $(CLEAR),--clear)

.DEFAULT_GOAL := help
.PHONY: help upload logs wifi-test wifi-check test

help:
	@echo "Targets (PORTS=\"$(PORTS)\"):"
	@echo "  upload      upload application files; hardware version is detected"
	@echo "              (BADGE_VERSION=2026 overrides)"
	@echo "  logs        save each badge's link log to $(LOG_DIR)/ (CLEAR=1 also"
	@echo "              deletes it on the badge)"
	@echo "  wifi-test   run the Wi-Fi-only tic-tac-toe test on all badges at once"
	@echo "  wifi-check  upload, wifi-test, then logs"
	@echo "  test        run the unit tests"

upload:
	@set -e; for port in $(PORTS); do \
		echo "=== upload $$port"; \
		$(BADGE) upload --port $$port $(VERSION_ARG); \
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

# Collect the logs even when the test fails; that is when they matter most.
wifi-check: upload
	@$(MAKE) --no-print-directory wifi-test; status=$$?; \
	$(MAKE) --no-print-directory logs; exit $$status

test:
	$(PY) -m unittest discover -s tests

# Badge workflows for one or more badges on USB serial ports.
#
#   make upload       upload to every badge in PORTS
#   make badge-test   every hardware test on all badges, pull their logs,
#                     write a report
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

# Every hardware test target, in the order badge-test runs them, and the
# marker each one prints (TEST-MARKER <device ID>: PASS/FAIL) so report can
# find them regardless of which ones actually ran.
PAIR_TESTS = wifi-test menu-test pong-wifi-test pong-menu-test
ALL_TESTS = $(PAIR_TESTS) solo-test
ALL_MARKERS = TTT-WIFI MENU-TEST PONG-WIFI PONG-MENU SOLO-TEST

.DEFAULT_GOAL := help
.PHONY: help tools upload reset logs $(ALL_TESTS) badge-test report wifi-check test

help:
	@echo "Targets (PORTS=\"$(PORTS)\"):"
	@echo "  tools          install esptool, mpremote and mpy-cross ($(PIP_INSTALL))"
	@echo "  upload         upload precompiled application files; hardware version"
	@echo "                 is detected (BADGE_VERSION=2026 overrides, NO_COMPILE=1"
	@echo "                 uploads .py sources)"
	@echo "  badge-test     run every hardware test on all badges at once, save"
	@echo "                 their link logs, and bundle the run into one report"
	@echo "  wifi-check     upload, then badge-test"
	@echo "  wifi-test      only the test that starts tic-tac-toe directly"
	@echo "  menu-test      only the test that opens tic-tac-toe from the menu"
	@echo "  pong-wifi-test only the test that starts Pong directly"
	@echo "  pong-menu-test only the test that opens Pong from the menu"
	@echo "  solo-test      opens every single-player game from the menu in"
	@echo "                 turn on each badge (no pairing, so it also runs"
	@echo "                 fine on just one)"
	@echo "  reset          hard-reset every badge (upload and logs do it by"
	@echo "                 themselves when a badge stops answering)"
	@echo "  logs           only save each badge's link log to $(LOG_DIR)/"
	@echo "                 (CLEAR=1 also deletes it on the badge)"
	@echo "  report         bundle the files of the last badge-test run again"
	@echo "  test           run the unit tests"

tools:
	$(PIP_INSTALL) esptool mpremote mpy-cross

upload:
	@set -e; for port in $(PORTS); do \
		echo "=== upload $$port"; \
		$(BADGE) upload --port $$port $(VERSION_ARG) $(COMPILE_ARG); \
	done

reset:
	@set -e; for port in $(PORTS); do \
		echo "=== reset $$port"; \
		$(BADGE) reset --port $$port; \
	done

logs:
	@set -e; for port in $(PORTS); do \
		echo "=== logs $$port"; \
		$(BADGE) logs --port $$port $(CLEAR_ARG); \
	done

# wifi-test starts tic-tac-toe by itself; menu-test starts the normal
# application and opens Games -> Tic-tac-toe with simulated button presses.
# pong-wifi-test and pong-menu-test are the same idea for Pong. solo-test
# opens every single-player game in turn; it does not pair badges, so it
# is the only one of the five that also makes sense on just one badge.
wifi-test: TEST_SCRIPT = tests/tictactoe_wifi_pair_hardware.py
wifi-test: TEST_MARKER = TTT-WIFI
menu-test: TEST_SCRIPT = tests/tictactoe_menu_hardware.py
menu-test: TEST_MARKER = MENU-TEST
pong-wifi-test: TEST_SCRIPT = tests/pong_wifi_pair_hardware.py
pong-wifi-test: TEST_MARKER = PONG-WIFI
pong-menu-test: TEST_SCRIPT = tests/pong_menu_hardware.py
pong-menu-test: TEST_MARKER = PONG-MENU
solo-test: TEST_SCRIPT = tests/singleplayer_menu_hardware.py
solo-test: TEST_MARKER = SOLO-TEST

# The two badges in a pairing test must run at the same time, so start
# every badge in parallel and keep each side's output in
# $(LOG_DIR)/<test>-<port>.txt.
$(ALL_TESTS):
	@mkdir -p $(LOG_DIR); \
	for port in $(PORTS); do \
		out=$(LOG_DIR)/$@-$$(basename $$port).txt; \
		( $(PY) -m mpremote connect $$port run $(TEST_SCRIPT) 2>&1 \
			| tee $$out | sed "s|^|[$$(basename $$port)] |" ) & \
	done; \
	wait; \
	status=0; \
	for port in $(PORTS); do \
		out=$(LOG_DIR)/$@-$$(basename $$port).txt; \
		if grep -q "$(TEST_MARKER) .*: PASS" $$out; then \
			echo "PASS $@ $$port"; \
		else \
			echo "FAIL $@ $$port (see $$out)"; status=1; \
		fi; \
	done; \
	exit $$status

# The logs and the report are collected even when a test fails; that is
# when they matter most. It fails if any test failed. The pairing tests
# need at least two ports; solo-test runs regardless.
badge-test:
	@mkdir -p $(LOG_DIR); touch $(RUN_STAMP)
	@status=0; \
	set -- $(PORTS); \
	if [ $$# -ge 2 ]; then \
		for t in $(PAIR_TESTS); do \
			$(MAKE) --no-print-directory $$t || status=1; \
		done; \
	else \
		echo "Only one port in PORTS: skipping the pairing tests ($(PAIR_TESTS))."; \
	fi; \
	$(MAKE) --no-print-directory solo-test || status=1; \
	$(MAKE) --no-print-directory logs; \
	$(MAKE) --no-print-directory report; \
	exit $$status

# One file to attach: a PASS/FAIL summary, then every test output and link
# log written since the last badge-test started.
report:
	@test -f $(RUN_STAMP) || { echo "No badge-test run yet."; exit 1; }; \
	files="$$(for t in $(ALL_TESTS); do \
			find $(LOG_DIR) -maxdepth 1 -newer $(RUN_STAMP) -name "$$t-*.txt"; \
		done | sort) \
		$$(find $(LOG_DIR) -maxdepth 1 -newer $(RUN_STAMP) -name 'linklog-*.txt' | sort)"; \
	markers=$$(echo $(ALL_MARKERS) | sed 's/ /\\|/g'); \
	out=$(LOG_DIR)/report-$$(date +%Y%m%d-%H%M%S).txt; \
	{ \
		echo "# badge test report $$(date '+%Y-%m-%d %H:%M:%S')"; \
		echo "# git $$(git rev-parse --short HEAD 2>/dev/null) $$(git branch --show-current 2>/dev/null)"; \
		echo "# ports $(PORTS)"; \
		for f in $$files; do \
			case $$f in *-test-*) \
				result=$$(grep -o "\($$markers\) .*: \(PASS\|FAIL.*\)" $$f | tail -1); \
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

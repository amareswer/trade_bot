# Trade-bot local automation.
#
# The only thing here right now is the weekly progress monitor's schedule.
# Everything is machine-local (launchd on macOS); moving to a new machine =
# re-run `make install-monitor` after the venv + .env are in place.

REPO_DIR      := $(shell cd "$(dir $(lastword $(MAKEFILE_LIST)))" && pwd)
PLIST_LABEL   := com.tradebot.weeklymonitor
PLIST_TEMPLATE:= $(REPO_DIR)/deploy/$(PLIST_LABEL).plist.template
PLIST_TARGET  := $(HOME)/Library/LaunchAgents/$(PLIST_LABEL).plist

.PHONY: help
help:
	@echo "make install-monitor    — schedule the weekly stock-bot monitor (launchd, Mon 17:30 local)"
	@echo "make uninstall-monitor  — remove that schedule"
	@echo "make monitor-status     — show whether it's loaded + last run"
	@echo "make monitor-run        — run the weekly monitor once now (prints report, no Telegram)"

.PHONY: install-monitor
install-monitor: $(PLIST_TEMPLATE) scripts/weekly_monitor.sh
	@command -v launchctl >/dev/null 2>&1 || { echo "launchctl not found — this target is macOS only. On Linux use cron: see deploy/WEEKLY_MONITOR.md"; exit 1; }
	@chmod +x "$(REPO_DIR)/scripts/weekly_monitor.sh"
	@mkdir -p "$(HOME)/Library/LaunchAgents"
	@sed 's|__REPO__|$(REPO_DIR)|g' "$(PLIST_TEMPLATE)" > "$(PLIST_TARGET)"
	@launchctl unload "$(PLIST_TARGET)" 2>/dev/null || true
	@launchctl load "$(PLIST_TARGET)"
	@echo "Installed $(PLIST_TARGET)"
	@echo "Runs Monday 17:30 local. 'make monitor-status' to check, 'make monitor-run' to test now."

.PHONY: uninstall-monitor
uninstall-monitor:
	@launchctl unload "$(PLIST_TARGET)" 2>/dev/null || true
	@rm -f "$(PLIST_TARGET)"
	@echo "Removed $(PLIST_TARGET) (the code stays; only the schedule is gone)"

.PHONY: monitor-status
monitor-status:
	@launchctl list | grep "$(PLIST_LABEL)" || echo "not loaded (run: make install-monitor)"
	@echo "---"
	@tail -n 20 "$(REPO_DIR)/logs/weekly_monitor_cron.log" 2>/dev/null || echo "no run log yet"

.PHONY: monitor-run
monitor-run:
	@"$(REPO_DIR)/.venv/bin/python" -m stock_bot.analysis.weekly_monitor --no-baseline-update

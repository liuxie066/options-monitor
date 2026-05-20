# Linux / macOS install setup platform profile

Decision:

- Linux and macOS are first-class setup platforms with one shared `PlatformProfile` model.
- Linux defaults: `systemd`, `/var/lib/options-monitor`, `/etc/options-monitor/options-monitor.env`.
- macOS defaults: `launchd`, `$HOME/Library/Application Support/options-monitor`, `$HOME/Library/Application Support/options-monitor/options-monitor.env`.
- Repo-local `.env/options-monitor.env` remains the local/manual-run env file, not the long-running service default.
- `scripts/install.sh` still only installs code/dependencies and updates the `current` symlink; it must not write real config, write secrets, enable services, connect OpenD/Feishu, or touch SQLite.
- `setup check` is the operator onboarding surface for platform defaults, optional server dependency visibility, recommended runtime/env paths, and next-step commands.

Validation:

- `bash -n scripts/install.sh`
- `git diff --check`
- `python3 -m compileall src/application/platform_profile.py src/application/setup/check.py src/application/service_deploy.py`
- `python3 -m pytest tests/test_platform_profile.py tests/test_setup_check.py tests/test_install_script.py tests/test_service_deploy.py tests/test_effective_settings.py -q`
- `python3 -m pytest`


# Service upgrade cache boundary

Decision:

- `service upgrade` keeps the release model unchanged: each release has its own code snapshot and `.venv`, and `current` remains a symlink.
- Host-level tools and caches live outside release directories. `git`, `python3`, and `uv` are detected/used as host tools; upgrade must not install `uv` into each release.
- Release code materialization uses a host git cache at `_cache/git/options-monitor.git`: first run mirror-clones, later runs fetch tags and archive the target tag into `releases/<version>`.
- Because archived release directories intentionally have no `.git`, upgrade-specific version discovery and remote URL resolution must fall back to `_cache/git/options-monitor.git` when the current release is not a git checkout.
- Runtime dependency downloads use stable cache directories under `_cache/uv` and `_cache/pip`; installed packages still go into the release-local `.venv`.
- uv virtualenv creation pins the interpreter with `uv venv --python python3 .venv` to avoid implicit managed-Python downloads.

Validation:

- `python3 -m pytest tests/test_service_deploy.py -q`
- `python3 -m basedpyright --level error src/application/service_upgrade.py src/interfaces/cli/main.py`
- `git diff --check -- src/application/service_upgrade.py src/interfaces/cli/main.py tests/test_service_deploy.py docs/RELEASE_PROCESS.md docs/DEPLOY_LINUX_MAC.md docs/TOOL_REFERENCE.md`
- `python3 scripts/release_check.py`

# Optional Portfolio Management capability

`om-pm-plugin` is installed separately into the Python environment running OM:

```sh
python -m pip install ./plugins/portfolio-management
python -m pip uninstall om-pm-plugin
```

OM supplies `src.application.portfolio_management_contract`; this package is an
OM extension, not a standalone PM service or client SDK. It has no automatic
installation hook or default runtime source-path injection. Core imports,
configuration validation, `om help`, and `om-agent spec` work without it.
`portfolio_management.enabled` still defaults to false; disabled resolution does
not import the plugin. Enabling without the package raises the shared
`PortfolioManagementUnavailableError`, mapped to `PORTFOLIO_MANAGEMENT_UNAVAILABLE`.

The existing infrastructure import is a compatibility constructor plus contract
exports. HTTP transport now lives in `om_pm_plugin.client`; exceptions, endpoint
constants and evidence validators have one core owner. Existing HTTP behavior,
loopback restrictions, scope/provenance validation and no-retry semantics remain.
The existing durable refresh endpoint is retained, not extended with new trade
synchronization or trading behavior.

Service upgrades preserve the current instance's installed package independently
of its enabled setting. A release with plugin support respects explicit uninstall;
the first migration from the built-in PM client installs this extension only when
the effective legacy integration is enabled. The existing dependency cache includes
the selected package's metadata and source; target import/version checks run before
switching the active release. The first transition requires the fixed controller
from the new source to drive the old instance; an already released updater does
not load these fixes from its target. See `docs/RELEASE_PROCESS.md` in the OM source tree.

For development/CI, explicitly install this package in an isolated test
environment before running the existing full automatic test discovery. Do not
skip PM tests. Alternatively install with `pip --target <temporary-directory>`
and add that installed directory to `PYTHONPATH` for the test process only.
`tests/test_portfolio_management_optional_package.py` checks installed-package
metadata and independently blocks plugin imports in child processes to verify
absent-package startup, errors and disabled behavior. Existing PM client tests
exercise the extracted HTTP implementation through the compatibility constructor.

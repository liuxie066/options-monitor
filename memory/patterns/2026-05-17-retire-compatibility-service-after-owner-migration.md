# Retire Compatibility Service After Owner Migration

When a compatibility service is no longer needed, first migrate tests to the real owner modules instead of keeping a test-only facade. Then delete the compatibility file and add a structural test that requires the file to stay absent.

For the option ledger cutover, the successful sequence was:

- move runtime writes to `src.application.ledger.service`;
- move implementation to explicit owners under `src/application/ledger/`;
- migrate tests from `src.application.option_positions_service as svc` to `ledger.repository`, `ledger.bootstrap`, `ledger.writer`, `ledger.manual_trades`, `ledger.interventions`, and `ledger.maintenance`;
- delete `src/application/option_positions_service.py`;
- make retirement tests assert physical absence plus owner-definition presence.

After the service file is gone, also remove compatibility read facades and test file names that preserve the old concept. In this cutover, `option_positions_facade.py` was deleted after test callers moved to `ledger.read_model`, and `test_option_positions_service.py` / `test_option_positions_sqlite_service.py` were renamed to `test_ledger_maintenance.py` / `test_ledger_sqlite_workflows.py`.

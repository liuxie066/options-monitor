# Position Advice

`om-position-advice-plugin` is the optional pure policy and Chinese explanation
package for OM's existing `remaining_yield_capture.v3` short Put/Call advice.
It has no network, ledger, configuration or notification writes. OM supplies
validated observations, fees, market timing and report I/O.

Install explicitly from the release root with
`python -m pip install ./plugins/position-advice`, or use the release installer's
`--with-position-advice`. Development requirements also include this package.
Runtime requirements exclude it. Installing does not enable advice:
`close_advice.enabled` remains the single switch.

Core `domain.domain.close_advice.CloseAdviceInput`, safe numeric helpers, sorting
and notification selection remain usable without this package. Core
`evaluate_close_advice(input)` loads the installed package lazily and raises
`PositionAdviceUnavailableError` when unavailable. Application callers can use
`position_advice_plugin_status(config)` for `disabled`, `unavailable` or
`available` without importing policy; disabled performs no package lookup.
`require_position_advice_plugin()` returns the module with
`evaluate_close_advice(input)` and
`format_close_advice_reason(*, recommendation, flags)`.

Inputs default to `input_source="ledger"` and require `position_lot_id`.
Temporary manual inputs use `input_source="user_provided"`, an `input_id`, and
no ledger lot, with optional `input_as_of_utc` provenance. They use identical
economics. Missing cost/fee evidence stays `not_evaluable`; independently valid
spread and moneyness remain visible without inferred profit metrics.

Notification selection rejects explicitly noncurrent `assessment_mode` values;
legacy report rows without the field retain their existing selection contract.

Controlled upgrades preserve installed package selection independently of
enablement. Legacy built-in installations migrate only when the effective
runtime configuration enabled advice. A plugin-aware release with the package
removed stays removed. Selection and package source are dependency-cache inputs;
version and policy/presentation imports are validated before switching releases.

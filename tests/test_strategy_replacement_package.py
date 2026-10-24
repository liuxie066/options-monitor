"""Public replacement/capacity sub-entries also exist in the isolated wheel."""
from test_strategy_core_package import installed, run  # noqa: F401


def test_installed_finite_lookup_and_public_subentries(installed):
    work, _, python, env, _ = installed
    result = run(python, '''
from pathlib import Path
import om_strategy_core as core
from domain.domain import strategy_core
assert "site-packages" in str(Path(strategy_core.__file__).resolve())
assert set(strategy_core._TEMPLATES) == {("csp_baseline", "0.2.0"), ("cc_baseline", "0.2.0")}
for mode, template in (("put", "csp_baseline"), ("call", "cc_baseline")):
    policy = core.resolve_strategy(mode=mode, config=core.InsuranceUnderwritingConfig(),
                                   template_id=template, template_version="0.2.0")
    assert policy.identity["identity_format"] == "strategy_identity.v2"
    assert core.rank_candidates([], mode=mode, strategy=policy) == []
    try:
        core.resolve_strategy(mode=mode, config=policy.config, template_id=template, template_version="missing")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown implementation accepted")
result = core.calculate_capacity({"strike": .001, "multiplier": 100, "currency": "USD"}, mode="put",
    capacity_inputs=core.PutCapacityInputs((("USD", 1.),)),
    exchange_rate_converter=core.CurrencyConverter(core.ExchangeRates()))
assert result["max_new_contracts"] == 9
assert core.runtime_identity(mode="installed")["mode"] == "installed"
print("installed lookup and capacity verified")
''', cwd=work, env=env)
    assert result == 'installed lookup and capacity verified'

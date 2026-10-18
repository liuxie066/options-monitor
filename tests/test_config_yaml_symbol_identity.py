from __future__ import annotations

from copy import deepcopy
from functools import partial
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


@pytest.fixture
def identity_fixture(tmp_path, monkeypatch):
    repo, runtime, home, stage = (tmp_path / name for name in ("repo", "runtime", "home", "stage"))
    for directory in (repo, runtime, home, stage):
        directory.mkdir()
    source, system = repo / "config.yaml", repo / "system.json"
    env_file = home / "options-monitor.env"
    env_file.write_text("", encoding="utf-8")
    data_path = runtime / "portfolio.runtime.json"
    data_path.write_text("{}\n", encoding="utf-8")
    # Keep pytest's node marker only; application settings must be fixture-owned.
    for name in tuple(os.environ):
        if name != "PYTEST_CURRENT_TEST":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("OM_ENV_FILE", str(env_file))
    monkeypatch.setenv("OM_DATA_CONFIG", str(data_path))
    monkeypatch.setenv("OM_SECRET_BACKEND", "env")
    assert set(os.environ) <= {
        "PYTEST_CURRENT_TEST", "HOME", "OM_RUNTIME_ROOT", "OM_ENV_FILE", "OM_DATA_CONFIG", "OM_SECRET_BACKEND",
    }
    system.write_text(json.dumps({
        "defaults": {"trade_intake": {"enabled": False}, "bot": {"enabled": False}},
        "markets": {"hk": {}, "us": {}},
    }), encoding="utf-8")

    from src.application import (
        agent_tool_config, config_authoring_transaction as transaction, config_yaml,
        config_yaml_symbols, runtime_config_paths, runtime_paths, secret_store, symbol_aliases,
    )
    from src.application.bot.control import llm_model_profiles
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.secret_store import runtime as secret_runtime

    calls = {name: [] for name in ("forbidden", "writes", "publish", "prepared", "prepare_source", "stage", "resolve")}

    def forbid(name, *_args, **_kwargs):
        calls["forbidden"].append(name)
        raise AssertionError(f"unexpected effect or fallback: {name}")

    for module, name in (
        (agent_tool_config, "resolve_runtime_root"),
        (agent_tool_config, "build_effective_env"),
        (runtime_config_paths, "build_effective_env"),
        (runtime_paths, "build_effective_env"),
        (secret_store, "default_secret_provider"),
        (secret_runtime, "default_secret_provider"),
        (symbol_aliases, "load_runtime_symbol_aliases"),
        (llm_model_profiles, "build_effective_env"),
        (llm_model_profiles, "build_secret_provider"),
        (llm_model_profiles, "resolve_secret_status"),
        (transaction, "locked_config_authoring"),
        (transaction, "validate_config_target_access_identity"),
        (transaction, "_atomic_write_bytes"),
    ):
        monkeypatch.setattr(module, name, partial(forbid, f"{module.__name__}.{name}"))
    monkeypatch.setattr(agent_tool_config, "repo_base", lambda: repo)

    real_writer = config_yaml.write_json_atomic

    def write_output(path, payload):
        calls["writes"].append(path)
        assert path in (runtime / "config.us.json", runtime / "config.hk.json")
        real_writer(path, payload)

    monkeypatch.setattr(config_yaml, "write_json_atomic", write_output)
    real_temporary_directory = transaction.tempfile.TemporaryDirectory

    def temporary_directory(**kwargs):
        assert "dir" not in kwargs
        context = real_temporary_directory(dir=stage, **kwargs)
        assert Path(context.name).parent == stage
        calls["stage"].append(Path(context.name))
        return context

    monkeypatch.setattr(transaction, "tempfile", SimpleNamespace(TemporaryDirectory=temporary_directory))
    real_runtime_resolver = transaction.resolve_yaml_runtime_config
    real_bot_resolver = transaction.resolve_yaml_bot_config

    def resolve(kind, resolver, **kwargs):
        assert kwargs["repo_root"] == repo
        assert Path(kwargs["config_path"]).parent.parent == stage
        assert "system_config_path" not in kwargs
        result = resolver(**kwargs, system_config_path=system)
        calls["resolve"].append((kind, deepcopy(result)))
        return result

    monkeypatch.setattr(transaction, "resolve_yaml_runtime_config", partial(resolve, "runtime", real_runtime_resolver))
    monkeypatch.setattr(transaction, "resolve_yaml_bot_config", partial(resolve, "bot", real_bot_resolver))
    real_prepare = transaction._prepare_generation

    def prepare(**kwargs):
        assert kwargs["source_path"] == source
        assert kwargs["runtime_root"] == runtime
        prepared = real_prepare(**kwargs)
        calls["prepare_source"].append(kwargs["source_bytes"])
        calls["prepared"].append(deepcopy(prepared))
        return prepared

    monkeypatch.setattr(transaction, "_prepare_generation", prepare)
    real_publish = config_yaml_symbols.publish_yaml_config_generation

    def publish(**kwargs):
        assert kwargs["apply"] is False
        assert kwargs["repo_root"] == repo
        assert kwargs["config_yaml_path"] == source
        assert kwargs["runtime_root"] == runtime
        calls["publish"].append(deepcopy(kwargs["config_doc"]))
        return real_publish(**kwargs)

    monkeypatch.setattr(config_yaml_symbols, "publish_yaml_config_generation", publish)

    def write_source(symbols, overrides, *, market="hk"):
        document = {
            "accounts": {"fixture": {"type": "futu", "futu_account_id": "REAL_00000001", "trade_intake_enabled": False}},
            "portfolio": {"data_config": str(data_path)},
            "bot": {"enabled": False},
            "templates": {"put_base": {}, "call_base": {}},
            "symbol_defaults": {
                "sell_put": {"enabled": False, "strategy": "insurance_underwriting", "min_dte": 2, "max_dte": 10},
                "covered_call": {"enabled": False, "strategy": "insurance_underwriting", "min_dte": 2, "max_dte": 10},
            },
            "markets": {market: {"accounts": ["fixture"], "symbols": symbols, "overrides": overrides}},
        }
        source.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return document

    def snapshot():
        return {
            str(path.relative_to(tmp_path)): None if path.is_dir() else path.read_bytes()
            for path in sorted(tmp_path.rglob("*"))
        }

    yield {
        "root": tmp_path, "repo": repo, "runtime": runtime, "source": source,
        "system": system, "stage": stage, "calls": calls, "write_source": write_source,
        "snapshot": snapshot,
        "build": config_yaml.build_yaml_runtime_config_file,
        "mutate": config_yaml_symbols.mutate_yaml_symbol_config,
        "set": config_yaml_symbols.set_yaml_symbol_config,
        "error": AgentToolError,
        "build_args": {"repo_root": repo, "config_path": source, "system_config_path": system,
                       "runtime_root": runtime, "dry_run": False},
        "edit_args": {"repo_root": repo, "market": "hk", "config_path": source,
                      "rebuild_runtime_root": runtime, "apply": False},
    }
    assert calls["forbidden"] == []


def _assert_preview(fixture, result, before):
    calls = fixture["calls"]
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["write_applied"] is False
    assert result["backup_path"] is None
    assert result["audit_id"] is None
    assert result["rebuild"] is None
    assert result["summary"]["canonical_symbol"] == "9992.HK"
    assert result["summary"]["affected_accounts"] == ["fixture"]
    assert len(calls["publish"]) == len(calls["prepared"]) == len(calls["stage"]) == 1
    assert len(calls["prepare_source"]) == 1
    assert yaml.safe_load(calls["prepare_source"][0]) == calls["publish"][0]
    source_sha = sha256(fixture["source"].read_bytes()).hexdigest()
    candidate_sha = sha256(calls["prepare_source"][0]).hexdigest()
    assert result["source_revision"] == {"before_sha256": source_sha, "after_sha256": candidate_sha}
    assert [kind for kind, _ in calls["resolve"]] == ["runtime", "bot"]
    assert calls["writes"] == calls["forbidden"] == []
    prepared = calls["prepared"][0]
    assert result["validation"] == prepared["markets"]
    assert prepared["bot"]["ok"] is True
    assert [item["role"] for item in prepared["target_payloads"]] == ["runtime_hk", "bot"]
    payloads = {}
    for item in prepared["target_payloads"]:
        expected = fixture["runtime"] / ("config.hk.json" if item["role"] == "runtime_hk" else "resolved/config.bot.json")
        assert item["path"] == expected
        assert item["source"] is False
        assert not expected.exists()
        payloads[item["role"]] = json.loads(item["payload"])
        metadata = prepared["markets"]["hk"] if item["role"] == "runtime_hk" else prepared["bot"]
        assert metadata["output_config_path"] == str(expected)
        assert metadata["sha256"] == sha256(item["payload"]).hexdigest()
        generated = payloads[item["role"]]
        assert generated["_resolved"]["config_yaml_path"] == "config.yaml"
        assert generated["_resolved"]["config_yaml_sha256"] == candidate_sha
        source_role = "market_user" if item["role"] == "runtime_hk" else "config_yaml"
        source_metadata = next(row for row in generated["_generated"]["sources"] if row["role"] == source_role)
        assert source_metadata["path"] == "config.yaml"
        assert source_metadata["sha256"] == candidate_sha
        system_metadata = next(row for row in generated["_generated"]["sources"] if row["role"] == "system")
        assert system_metadata["loaded"] is True
        assert system_metadata["path"] == "system.json"
        assert system_metadata["sha256"] == sha256(fixture["system"].read_bytes()).hexdigest()
    assert payloads["runtime_hk"]["trade_intake"]["enabled"] is False
    assert payloads["runtime_hk"]["portfolio"]["data_config"] == str(fixture["runtime"] / "portfolio.runtime.json")
    assert payloads["bot"]["bot"]["enabled"] is False
    assert fixture["snapshot"]() == before
    return calls["publish"][0], payloads["runtime_hk"]


@pytest.mark.parametrize(
    ("raw_symbol", "raw_key", "canonical", "market"),
    [
        pytest.param("POP", "9992.HK", "9992.HK", "hk", id="alias-list"),
        pytest.param("9992.HK", "POP", "9992.HK", "hk", id="alias-override"),
        pytest.param("9992.HK", "9992.HK", "9992.HK", "hk", id="canonical"),
        pytest.param(" pop ", "PoP", "9992.HK", "hk", id="space-case"),
        pytest.param("泡泡玛特", "POP", "9992.HK", "hk", id="unicode"),
        pytest.param("700", "HK.00700", "0700.HK", "hk", id="numeric-hk"),
        pytest.param("HK.00700", "00700.HK", "0700.HK", "hk", id="broker-hk"),
        pytest.param("US.POP", "POP.US", "POP.US", "us", id="explicit-us-pop"),
        pytest.param("US.MET", "MET.US", "MET.US", "us", id="explicit-us-met"),
        pytest.param(" nvda ", "NVDA", "NVDA", "us", id="ordinary-us"),
    ],
)
def test_public_build_and_read_share_canonical_identity(identity_fixture, raw_symbol, raw_key, canonical, market):
    fixture = identity_fixture
    other = "3690.HK" if market == "hk" else "AAPL"
    override = {"sell_put": {"enabled": True, "max_strike": 123, "min_dte": 2, "max_dte": 10}}
    fixture["write_source"]([raw_symbol, other], {raw_key: override}, market=market)
    output = fixture["runtime"] / f"config.{market}.json"
    before = fixture["snapshot"]()

    result = fixture["build"](**fixture["build_args"], market=market, output_config_path=output)

    assert result["ok"] is True
    assert result["write_applied"] is True
    assert result["dry_run"] is False
    assert result["symbols"] == [canonical, other]
    assert result["accounts"] == ["fixture"]
    assert result["output_config_path"] == str(output)
    assert fixture["calls"]["writes"] == [output]
    generated = json.loads(output.read_bytes())
    assert [item["symbol"] for item in generated["symbols"]] == [canonical, other]
    assert generated["symbols"][0]["sell_put"]["max_strike"] == 123
    assert generated["trade_intake"]["enabled"] is False
    assert generated["_generated"]["market"] == generated["_resolved"]["market"] == market
    for path_key, hash_key, source in (
        ("config_yaml_path", "config_yaml_sha256", fixture["source"]),
        ("system_config_path", "system_config_sha256", fixture["system"]),
    ):
        assert result[path_key] == str(source)
        assert result[hash_key] == sha256(source.read_bytes()).hexdigest()
    after = {**before, str(output.relative_to(fixture["root"])): output.read_bytes()}
    assert fixture["snapshot"]() == after

    from src.application.tool_execution import execute_tool

    records = []
    for query in (raw_symbol, canonical):
        response = execute_tool("symbol_config_read", {"config_path": str(output), "symbol": query})
        assert response["ok"] is True, response.get("error")
        assert response["data"]["found"] is True
        assert response["data"]["canonical_symbol"] == canonical
        records.append(response["data"]["symbol_config"])
    assert records[0] == records[1]
    assert records[0]["symbol"] == canonical
    assert fixture["snapshot"]() == after
    assert fixture["calls"]["forbidden"] == []


def test_public_build_preserves_unknown_nonempty_compatibility(identity_fixture):
    fixture = identity_fixture
    fixture["write_source"](["not a known symbol"], {"NOT A KNOWN SYMBOL": {"fetch": {"limit_expirations": 3}}})
    output = fixture["runtime"] / "config.hk.json"
    before = fixture["snapshot"]()
    result = fixture["build"](**fixture["build_args"], market="hk", output_config_path=output)
    assert result["symbols"] == ["NOT A KNOWN SYMBOL"]
    assert json.loads(output.read_bytes())["symbols"][0]["fetch"]["limit_expirations"] == 3
    assert fixture["snapshot"]() == {**before, str(output.relative_to(fixture["root"])): output.read_bytes()}


@pytest.mark.parametrize(
    ("market", "symbols", "overrides", "messages"),
    [
        pytest.param("hk", ["POP", "9992.HK"], {}, ("symbols", "'POP'", "'9992.HK'"), id="list-alias-first"),
        pytest.param("hk", ["9992.HK", "POP"], {}, ("symbols", "'POP'", "'9992.HK'"), id="list-canonical-first"),
        pytest.param("hk", ["POP"], {"POP": {}, "9992.HK": {}}, ("overrides", "'POP'", "'9992.HK'"), id="override-alias-first"),
        pytest.param("hk", ["POP"], {"9992.HK": {}, "POP": {}}, ("overrides", "'POP'", "'9992.HK'"), id="override-canonical-first"),
        pytest.param("us", ["NVDA"], {"nvda": {}, " NVDA ": {}}, ("overrides", "'nvda'", "' NVDA '", "NVDA"), id="override-case-first"),
        pytest.param("us", ["NVDA"], {" NVDA ": {}, "nvda": {}}, ("overrides", "'nvda'", "' NVDA '", "NVDA"), id="override-space-first"),
        pytest.param("hk", [], {}, ("symbols", "non-empty list"), id="empty-list"),
        pytest.param("hk", [700], {}, ("symbols[0]", "must be a symbol string"), id="nonstring"),
        pytest.param("hk", [" "], {}, ("symbols[0]", "non-empty symbol"), id="blank"),
        pytest.param("us", ["POP"], {}, ("symbols[0]", "9992.HK"), id="wrong-market"),
        pytest.param("hk", ["0700.HK"], {"POP": {}}, ("overrides.9992.HK", "also appear in symbols"), id="nonmember-override"),
    ],
)
def test_build_rejects_invalid_identity_before_output(identity_fixture, market, symbols, overrides, messages):
    fixture = identity_fixture
    fixture["write_source"](symbols, overrides, market=market)
    output = fixture["runtime"] / f"config.{market}.json"
    output.write_bytes(b"retained output\n")
    absent = fixture["root"] / "uncreated" / f"config.{market}.json"
    before = fixture["snapshot"]()
    for target in (output, absent):
        with pytest.raises(fixture["error"]) as error:
            fixture["build"](**fixture["build_args"], market=market, output_config_path=target)
        assert error.value.code == "CONFIG_ERROR"
        for message in messages:
            assert message in error.value.message
        assert fixture["snapshot"]() == before
        assert fixture["calls"]["writes"] == []
        assert not absent.parent.exists()


@pytest.mark.parametrize(
    ("operation", "raw_symbol", "raw_key", "request_symbol"),
    [
        pytest.param("edit", "POP", "POP", "9992.HK", id="edit-alias-source"),
        pytest.param("edit", "9992.HK", "9992.HK", "POP", id="edit-canonical-source"),
        pytest.param("upsert", "POP", "9992.HK", "9992.HK", id="upsert-mixed-keys"),
        pytest.param("set", "POP", "POP", "9992.HK", id="set-alias-source"),
        pytest.param("set", "9992.HK", " pop ", "POP", id="set-mixed-keys"),
        pytest.param("set", "POP", 9992, "9992.HK", id="set-numeric-override"),
        pytest.param("edit", " pop ", None, "9992.HK", id="create-raw-override"),
    ],
)
def test_authoring_preview_preserves_existing_source_identity(identity_fixture, operation, raw_symbol, raw_key, request_symbol):
    fixture = identity_fixture
    target = {"sell_put": {"enabled": False, "max_strike": 90}, "covered_call": {"enabled": False, "min_strike": 120},
              "fetch": {"limit_expirations": 3}}
    other = {"covered_call": {"enabled": False, "min_strike": 500}}
    overrides = {"0700.HK": other}
    if raw_key is not None:
        overrides[raw_key] = target
    document = fixture["write_source"]([raw_symbol, "0700.HK"], overrides)
    before = fixture["snapshot"]()

    if operation == "set":
        result = fixture["set"](**fixture["edit_args"], symbol=request_symbol, sell_put_max_strike=101)
    else:
        result = fixture["mutate"](**fixture["edit_args"], payload={
            "action": "edit", "symbol": request_symbol, "upsert": operation == "upsert", "set": {"sell_put.max_strike": 101},
        })

    candidate, generated = _assert_preview(fixture, result, before)
    key = raw_key if raw_key is not None else raw_symbol
    expected = deepcopy(document)
    expected["markets"]["hk"]["overrides"].setdefault(key, {}).setdefault("sell_put", {})["max_strike"] = 101
    assert candidate == expected
    assert result["summary"]["changed_paths"] == [f"markets.hk.overrides.{key}.sell_put.max_strike"]
    assert result["summary"]["before_error"] is None
    assert result["summary"]["after_effective"]["symbol"] == "9992.HK"
    assert [item["symbol"] for item in generated["symbols"]] == ["9992.HK", "0700.HK"]
    assert generated["symbols"][0]["sell_put"]["max_strike"] == 101


@pytest.mark.parametrize("operation", ["add", "upsert", "set"])
def test_authoring_new_target_uses_canonical_spelling(identity_fixture, operation):
    fixture = identity_fixture
    document = fixture["write_source"](["0700.HK"], {"0700.HK": {"fetch": {"limit_expirations": 3}}})
    before = fixture["snapshot"]()
    if operation == "set":
        result = fixture["set"](**fixture["edit_args"], symbol="POP", sell_put_enabled=True, sell_put_max_strike=101)
    else:
        result = fixture["mutate"](**fixture["edit_args"], payload={
            "action": "add" if operation == "add" else "edit", "symbol": "POP",
            "upsert": True, "inherit_defaults": True,
            "set": {"sell_put.enabled": True, "sell_put.max_strike": 101},
        })
    candidate, generated = _assert_preview(fixture, result, before)
    market = candidate["markets"]["hk"]
    assert market["symbols"] == ["0700.HK", "9992.HK"]
    assert set(market["overrides"]) == {"0700.HK", "9992.HK"}
    assert market["overrides"]["0700.HK"] == document["markets"]["hk"]["overrides"]["0700.HK"]
    assert market["overrides"]["9992.HK"]["sell_put"] == {"enabled": True, "max_strike": 101}
    assert "markets.hk.symbols[]" in result["summary"]["changed_paths"]
    assert all("POP" not in path for path in result["summary"]["changed_paths"])
    assert generated["symbols"][1]["symbol"] == "9992.HK"
    assert generated["symbols"][1]["sell_put"]["max_strike"] == 101


def test_add_repairs_unique_alias_orphan_without_creating_collision(identity_fixture):
    fixture = identity_fixture
    document = fixture["write_source"](["0700.HK"], {"POP": {"fetch": {"limit_expirations": 3}}})
    before = fixture["snapshot"]()
    result = fixture["mutate"](**fixture["edit_args"], payload={
        "action": "add", "symbol": "9992.HK", "inherit_defaults": True,
        "set": {"sell_put.enabled": True, "sell_put.max_strike": 101},
    })
    candidate, generated = _assert_preview(fixture, result, before)
    expected = deepcopy(document)
    expected["markets"]["hk"]["symbols"].append("9992.HK")
    expected["markets"]["hk"]["overrides"]["POP"] = {"sell_put": {"enabled": True, "max_strike": 101}}
    assert candidate == expected
    assert result["summary"]["before_error"] is not None
    assert result["summary"]["changed_paths"] == ["markets.hk.symbols[]", "markets.hk.overrides.POP"]
    assert generated["symbols"][1]["symbol"] == "9992.HK"


def test_remove_preview_selects_unique_alias_and_preserves_other_target(identity_fixture):
    fixture = identity_fixture
    other = {"fetch": {"limit_expirations": 3}}
    document = fixture["write_source"](["POP", "0700.HK"], {"9992.HK": {}, "0700.HK": other})
    before = fixture["snapshot"]()
    result = fixture["mutate"](**fixture["edit_args"], payload={"action": "remove", "symbol": "9992.HK"})
    candidate, generated = _assert_preview(fixture, result, before)
    expected = deepcopy(document)
    expected["markets"]["hk"].update(symbols=["0700.HK"], overrides={"0700.HK": other})
    assert candidate == expected
    assert result["summary"]["changed_paths"] == ["markets.hk.symbols", "markets.hk.overrides.9992.HK"]
    assert [item["symbol"] for item in generated["symbols"]] == ["0700.HK"]


def test_add_existing_alias_rejects_before_publication(identity_fixture):
    fixture = identity_fixture
    fixture["write_source"](["POP"], {"9992.HK": {}})
    before = fixture["snapshot"]()
    with pytest.raises(fixture["error"]) as error:
        fixture["mutate"](**fixture["edit_args"], payload={"action": "add", "symbol": "9992.HK"})
    assert error.value.code == "INPUT_ERROR"
    assert error.value.message == "symbol already exists: 9992.HK"
    assert fixture["snapshot"]() == before
    assert all(values == [] for values in fixture["calls"].values())


@pytest.mark.parametrize("facade", ["generic", "set"])
@pytest.mark.parametrize(
    ("symbols", "overrides", "field"),
    [
        pytest.param(["POP", "9992.HK"], {}, "symbols", id="list-alias-first"),
        pytest.param(["9992.HK", "POP"], {}, "symbols", id="list-canonical-first"),
        pytest.param(["POP"], {"POP": {}, "9992.HK": {}}, "overrides", id="override-alias-first"),
        pytest.param(["POP"], {"9992.HK": {}, "POP": {}}, "overrides", id="override-canonical-first"),
    ],
)
def test_authoring_collision_rejects_before_publication(identity_fixture, facade, symbols, overrides, field):
    fixture = identity_fixture
    fixture["write_source"](symbols, overrides)
    before = fixture["snapshot"]()
    with pytest.raises(fixture["error"]) as error:
        if facade == "set":
            fixture["set"](**fixture["edit_args"], symbol="9992.HK", sell_put_max_strike=101)
        else:
            fixture["mutate"](**fixture["edit_args"], payload={
                "action": "edit", "symbol": "9992.HK", "set": {"sell_put.max_strike": 101},
            })
    assert error.value.code == "CONFIG_ERROR"
    assert f"markets.hk.{field}" in error.value.message
    assert "'POP'" in error.value.message
    assert "'9992.HK'" in error.value.message
    assert fixture["snapshot"]() == before
    assert all(values == [] for values in fixture["calls"].values())


@pytest.mark.parametrize("facade", ["generic", "set"])
def test_malformed_existing_alias_can_be_repaired_to_disabled(identity_fixture, facade):
    fixture = identity_fixture
    document = fixture["write_source"](["POP"], {"POP": {
        "sell_put": {"enabled": True, "min_dte": None, "max_dte": 10, "max_strike": 101},
        "covered_call": {"enabled": False},
    }})
    before = fixture["snapshot"]()
    if facade == "set":
        result = fixture["set"](**fixture["edit_args"], symbol="9992.HK", sell_put_enabled=False)
    else:
        result = fixture["mutate"](**fixture["edit_args"], payload={
            "action": "edit", "symbol": "9992.HK", "set": {"sell_put.enabled": False},
        })
    candidate, generated = _assert_preview(fixture, result, before)
    expected = deepcopy(document)
    expected["markets"]["hk"]["overrides"]["POP"]["sell_put"]["enabled"] = False
    assert candidate == expected
    assert result["summary"]["before_effective"] is None
    assert "missing min_dte" in result["summary"]["before_error"]
    assert result["summary"]["changed_paths"] == ["markets.hk.overrides.POP.sell_put.enabled"]
    assert generated["symbols"][0]["symbol"] == "9992.HK"
    assert generated["symbols"][0]["sell_put"]["enabled"] is False
    assert generated["symbols"][0]["sell_call"]["enabled"] is False

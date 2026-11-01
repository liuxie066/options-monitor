from __future__ import annotations

import pytest

from src.interfaces.cli import account_selection as mod


def metadata(*ids, status="ok", **overrides):
    return {
        "status": status, "market": "US", "trd_env": "SIMULATE", "truncated": False,
        "permission_unknown_count": 0, "identity_unknown_count": 0,
        "candidates": [{"account_id": value, "masked_account_id": "*****0001",
                        "acc_type": "MARGIN", "sim_acc_type": "OPTION"} for value in ids],
        **overrides,
    }


def select(monkeypatch, answers, results=()):
    replies, discoveries = iter(answers), iter(results)
    calls, prompts = [], []

    def discover(**kwargs):
        assert prompts and "输入 yes 同意查询" in prompts[-1]
        calls.append(kwargs)
        return next(discoveries)

    def answer(prompt):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(mod, "discover_account_candidates", discover)
    result = mod.select_account(host="synthetic", port=12345, market="us", trd_env="SIMULATE", input_fn=answer)
    return result, calls, prompts


def test_declined_discovery_uses_manual_pending_id_and_recovers_bad_input(monkeypatch, capsys):
    result, calls, prompts = select(monkeypatch, ["no", "manual", "bad", "", "1.0", "912340001"])
    assert result.account_id == "912340001"
    assert result.validation == "pending_validation"
    assert calls == []
    output = capsys.readouterr().out + "".join(prompts) + repr(result)
    assert "待验证" in output
    assert "912340001" not in output


@pytest.mark.parametrize("raw", ["0", "001", "١٢٣"])
def test_manual_retains_existing_authoring_digits_syntax_without_claiming_sdk_match(monkeypatch, raw):
    result, calls, _ = select(monkeypatch, ["manual", raw])
    assert result.account_id == raw
    assert result.validation == "pending_validation"
    assert calls == []


def test_single_candidate_requires_confirmation_and_returns_actual_id(monkeypatch, capsys):
    result, calls, prompts = select(monkeypatch, ["yes", "yes"], [metadata("912340001")])
    assert result.action == "selected"
    assert result.account_id == "912340001"
    assert result.validation == "metadata_matched"
    assert calls == [{"host": "synthetic", "port": 12345, "market": "us", "trd_env": "SIMULATE"}]
    assert len(prompts) == 2
    assert "912340001" not in capsys.readouterr().out + "".join(prompts) + repr(result)


def test_default_manual_terminal_input_is_hidden(monkeypatch):
    prompts = []

    def hidden(prompt):
        prompts.append(prompt)
        return "810001111"

    def action(prompt):
        return "manual"

    # Stand in for built-in input to exercise the hidden terminal branch.
    monkeypatch.setattr(mod, "input", action, raising=False)
    monkeypatch.setattr(mod, "getpass", hidden)
    result = mod.select_account(host="synthetic", port=12345, market="us", trd_env="REAL", input_fn=action)
    assert result.account_id == "810001111"
    assert len(prompts) == 1


def test_manual_terminal_refuses_plaintext_fallback(monkeypatch):
    import warnings

    def action(prompt):
        return "manual"

    def unhidden(prompt):
        warnings.warn("no tty", mod.GetPassWarning)
        pytest.fail("must stop before getpass falls back to echoing the ID")

    monkeypatch.setattr(mod, "input", action, raising=False)
    monkeypatch.setattr(mod, "getpass", unhidden)
    with pytest.raises(ValueError, match="无法隐藏"):
        mod.select_account(host="synthetic", port=12345, market="us", trd_env="REAL", input_fn=action)


def test_reject_single_candidate_does_not_select_it_or_auto_query(monkeypatch):
    result, calls, _ = select(monkeypatch, ["yes", "no", "exit"], [metadata("912340001")])
    assert result.account_id is None
    assert result.action == "cancelled"
    assert len(calls) == 1


def test_same_tail_same_type_requires_explicit_sequence_and_confirmation(monkeypatch, capsys):
    result, calls, prompts = select(
        monkeypatch, ["yes", "wrong", "0", "3", "2", "yes"], [metadata("912340001", "922340001")],
    )
    assert result.account_id == "922340001"
    assert len(calls) == 1
    output = capsys.readouterr().out + "".join(prompts)
    assert "1. *****0001" in output and "2. *****0001" in output
    assert "同尾号" in output and "MARGIN" in output and "OPTION" in output
    assert "912340001" not in output and "922340001" not in output


@pytest.mark.parametrize("status,text", [
    ("empty", "列表为空"), ("no_match", "没有匹配"),
    ("permission_unavailable", "权限证据缺失"), ("identity_unavailable", "不可无损识别"),
])
def test_nonselectable_result_keeps_specific_reason_and_manual_fallback(monkeypatch, capsys, status, text):
    result, calls, _ = select(monkeypatch, ["yes", "manual", "912340001"], [metadata(status=status)])
    assert result.validation == "pending_validation"
    assert result.discovery_status == status
    assert len(calls) == 1
    assert text in capsys.readouterr().out


@pytest.mark.parametrize("code,text", [
    ("AUTH_EXPIRED", "登录"), ("UNREACHABLE", "无法连接"), ("NEED_2FA", "手机验证"),
    ("NEED_PIC_VERIFY", "图形验证"), ("RATE_LIMIT", "请求受限"), ("TRANSIENT", "超时"),
    ("UNKNOWN", "查询失败"),
])
def test_discovery_failure_can_return_to_parent_for_endpoint_change(monkeypatch, capsys, code, text):
    result, calls, _ = select(monkeypatch, ["yes", "change"], [metadata(status="unavailable", error_code=code)])
    assert result.action == "change_endpoint"
    assert result.account_id is None
    assert len(calls) == 1
    assert text in capsys.readouterr().out


def test_retry_requires_new_consent_and_uses_only_fresh_candidates(monkeypatch, capsys):
    result, calls, _ = select(
        monkeypatch, ["yes", "no", "yes", "yes"], [metadata("912340001"), metadata("922340001")],
    )
    assert result.account_id == "922340001"
    assert len(calls) == 2
    assert "912340001" not in capsys.readouterr().out


def test_multiple_back_does_not_select_an_account_or_requery(monkeypatch):
    result, calls, _ = select(monkeypatch, ["yes", "back", "change"], [metadata("912340001", "922340001")])
    assert result.action == "change_endpoint" and result.account_id is None
    assert len(calls) == 1


def test_truncation_and_unknowns_are_not_hidden(monkeypatch, capsys):
    select(monkeypatch, ["yes", "yes"], [metadata("912340001", truncated=True,
                                               permission_unknown_count=1, identity_unknown_count=1)])
    output = capsys.readouterr().out
    assert "有界列表" in output and "权限证据" in output and "不可无损识别" in output


@pytest.mark.parametrize("failure", [EOFError, KeyboardInterrupt])
def test_cancellation_propagates_without_discovery_or_writes(monkeypatch, failure):
    def discover(**kwargs):
        pytest.fail("must not query without consent")

    def answer(prompt):
        raise failure()

    monkeypatch.setattr(mod, "discover_account_candidates", discover)
    with pytest.raises(failure):
        mod.select_account(host="synthetic", port=12345, market="us", trd_env="REAL", input_fn=answer)

"""Shared content pages preserve redaction, scope, budgets and continuation."""
import json
import time

import pytest

from src.application import query_cursor
from src.application import read_content as content
from src.application.research.redaction import redact_text


@pytest.fixture(autouse=True)
def signer(monkeypatch):
    monkeypatch.setattr(query_cursor, "_key", lambda: "isolated-content-key")


@pytest.mark.parametrize("resource", ["project", "run", "daily_decision_brief_read", "option_performance_report"])
def test_pages_reassemble_redacted_unicode_and_keep_scope(resource):
    pem = "-----BEGIN " + "PRIVATE KEY-----\nprivate\n-----END " + "PRIVATE KEY-----"
    original = "前文\n" + "长文本abc" * 1800 + "\n" + pem + "\n尾文"
    raw = original.encode()
    scope = {"account": "lx", "market": "HK", "config_key": "hk", "run_id": "r1"}
    cursor, parts = None, []
    for _ in range(100):
        page = content.page_text(raw, relative_name="body", resource=resource, scope=scope,
                                 cursor=cursor, max_chars=700)
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= (24000 if resource == "project" else 9000)
        assert all(page["scope"][key] == value for key, value in scope.items())
        assert page["source"]["redaction_version"] == "project-lines-v1"
        parts.append(page["text"])
        cursor = page["next_cursor"]
        if not cursor:
            assert page["body_complete"]
            break
    else:
        pytest.fail("continuation must make progress")
    assert "".join(parts) == redact_text(original, preserve_newlines=True)


def test_cursor_binds_scope_and_source():
    page = content.page_text(b"x" * 10000, relative_name="body", scope={"account": "lx"})
    for raw, scope, code in ((b"y" * 10000, {"account": "lx"}, "source_changed"),
                             (b"x" * 10000, {"account": "sy"}, "cursor_invalidated")):
        with pytest.raises(query_cursor.CursorError, match=code):
            content.page_text(raw, relative_name="body", scope=scope, cursor=page["next_cursor"])


def test_content_checks_cancellation_deadline_and_encoding():
    for raw, options, code in ((b"text", {"cancelled": lambda: True}, "cancelled"),
                               (b"text", {"deadline_monotonic": time.monotonic() - 1}, "time_deadline"),
                               (b"\xff", {}, "unsupported"),
                               (b"\x00", {}, "unsupported"),
                               (b"x" * (1024 * 1024 + 1), {}, "file_too_large")):
        with pytest.raises(query_cursor.CursorError, match=code):
            content.page_text(raw, relative_name="body", scope={}, **options)


def test_missing_signer_keeps_partial_evidence(monkeypatch):
    def missing():
        raise query_cursor.CursorError("continuation_unavailable")
    monkeypatch.setattr(query_cursor, "_key", missing)
    page = content.page_text(b"x" * 10000, relative_name="body", resource="run", scope={})
    assert page["next_cursor"] is None and not page["body_complete"]
    assert page["continuation_status"] == "continuation_unavailable"
    assert page["coverage"] == {"status": "partial", "complete_for": "point", "has_more": True, "complete": False}

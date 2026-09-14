"""Real filesystem contract checks for the project-only reader."""
import hashlib
import os
import time
from pathlib import Path

import pytest

from src.application.agent_tools import project_reader as reader
from src.application.research.redaction import redact_text


@pytest.fixture(autouse=True)
def signing(monkeypatch):
    monkeypatch.setattr(reader, "_key", lambda: "test-only-cursor-key")


def _file(root, name, text="sample\n"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize("count", [201, 401])
@pytest.mark.parametrize("action", ["list", "search"])
def test_all_pages_advance_without_duplicates(tmp_path, count, action):
    expected = set()
    for n in reversed(range(count)):
        name = f"docs/{n % 3}/file-{n:04}.md" if action == "search" else f"docs/file-{n:04}.md"
        _file(tmp_path, name, "needle\n")
        expected.add(name)
    names, cursor, cursors = [], None, set()
    for _ in range(count):
        result = reader.project_files(tmp_path, action=action, query="needle" if action == "search" else "", scope={"account": "lx"}, cursor=cursor, relative_name="docs" if action == "list" else "")
        names.extend(row["relative_name"] for row in result["entries"])
        cursor = result["next_cursor"]
        if not cursor:
            break
        assert cursor not in cursors
        cursors.add(cursor)
    assert set(names) == expected
    assert len(names) == count


def test_search_budget_continues_after_nonmatching_200(tmp_path):
    for n in reversed(range(401)):
        _file(tmp_path, f"docs/{n:04}.md", "needle" if n == 400 else "absent")
    first = reader.project_files(tmp_path, action="search", query="needle", scope={})
    assert first["scanned"] == 200 and first["entries"] == []
    second = reader.project_files(tmp_path, action="search", query="needle", scope={}, cursor=first["next_cursor"])
    assert second["scanned"] == 200 and second["entries"] == []
    third = reader.project_files(tmp_path, action="search", query="needle", scope={}, cursor=second["next_cursor"])
    assert third["entries"][0]["relative_name"] == "docs/0400.md"


def test_directory_10001_rejects_without_cursor(tmp_path, monkeypatch):
    directory = tmp_path / "docs"
    directory.mkdir()
    for n in range(10001):
        (directory / str(n)).touch()
    with pytest.raises(reader.ProjectReaderError, match="directory_too_large"):
        reader.project_files(tmp_path, relative_name="docs", scope={})
    _file(tmp_path, "docs/target.md", "only-target needle")
    def no_directory_enumeration(*_args, **_kwargs):
        pytest.fail("single-file search must not enumerate even an oversized parent")
    monkeypatch.setattr(reader, "list_names", no_directory_enumeration)
    page = reader.project_files(tmp_path, action="search", relative_name="docs/target.md", query="needle", scope={})
    assert [row["relative_name"] for row in page["entries"]] == ["docs/target.md"]
    assert page["next_cursor"] is None and page["coverage"]["complete"]


def test_actual_paths_links_fifo_and_binary(tmp_path):
    source = _file(tmp_path, "docs/read.md")
    (tmp_path / "docs/link.md").symlink_to(source)
    (tmp_path / "docs/alias").symlink_to(tmp_path / "docs", target_is_directory=True)
    os.link(source, tmp_path / "docs/hard.md")
    os.mkfifo(tmp_path / "docs/pipe.md")
    (tmp_path / "docs/binary.md").write_bytes(b"\x00\xff")
    for name in ("../outside", "/etc/passwd", "docs/link.md", "docs/alias/read.md", "docs/hard.md", "docs/pipe.md", "docs/binary.md", "docs/.env"):
        start = time.monotonic()
        with pytest.raises(reader.ProjectReaderError):
            reader.project_files(tmp_path, action="read", relative_name=name, scope={})
        assert time.monotonic() - start < 1


def test_long_line_redaction_and_source_range(tmp_path):
    original = "a" * 9000 + "\n-----BEGIN " + "PRIVATE KEY-----\nHIDDEN-CONTENT\n-----END PRIVATE KEY-----\ntail"
    file = _file(tmp_path, "docs/read.md", original)
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    cursor, pieces, ranges = None, [], []
    for _ in range(30):
        result = reader.project_files(tmp_path, action="read", relative_name="docs/read.md", scope={}, cursor=cursor)
        pieces.append(result["text"])
        ranges.append(result["body_range"])
        assert "HIDDEN-CONTENT" not in result["text"]
        assert result["source"]["content_hash"] == digest
        cursor = result["next_cursor"]
        if not cursor:
            break
    assert "".join(pieces) == redact_text(original, preserve_newlines=True)
    assert "".join(pieces).count("\n") == original.count("\n")
    assert ranges[0]["start_line"] == 1 and ranges[1]["start_char"] > 0
    assert ranges[-1]["end_line"] == 5
    assert file.read_text() == original


def test_cursor_scope_source_and_tampering(tmp_path):
    file = _file(tmp_path, "docs/read.md", "a" * 10000)
    first = reader.project_files(tmp_path, action="read", relative_name="docs/read.md", scope={"account": "lx"})
    cursor = first["next_cursor"]
    for modified in ({"scope": {"account": "sy"}}, {"cursor": cursor[:-5] + "abcde"}):
        args = {"action": "read", "relative_name": "docs/read.md", "scope": {"account": "lx"}, "cursor": cursor, **modified}
        with pytest.raises(reader.ProjectReaderError, match="cursor_invalidated"):
            reader.project_files(tmp_path, **args)
    continued = reader.project_files(
        tmp_path, action="read", relative_name="docs/read.md", scope={"account": "lx"},
        cursor=cursor, start_line=999, max_lines=3,
    )
    assert continued["body_range"]["start_char"] > 0
    file.write_text("b" * 10000)
    with pytest.raises(reader.ProjectReaderError, match="source_changed"):
        reader.project_files(tmp_path, action="read", relative_name="docs/read.md", scope={"account": "lx"}, cursor=cursor)


def test_directory_cursor_rejects_changed_namespace(tmp_path):
    for n in range(50):
        _file(tmp_path, f"docs/{n}.md")
    first = reader.project_files(tmp_path, relative_name="docs", scope={})
    _file(tmp_path, "docs/new.md")
    with pytest.raises(reader.ProjectReaderError, match="source_changed"):
        reader.project_files(tmp_path, relative_name="docs", scope={}, cursor=first["next_cursor"])


def test_size_cancel_deadline_and_missing_key(tmp_path, monkeypatch):
    _file(tmp_path, "docs/huge.md", "a" * (reader.MAX_FILE_BYTES + 1))
    with pytest.raises(reader.ProjectReaderError, match="file_too_large"):
        reader.read_bytes(tmp_path, "docs/huge.md")
    with pytest.raises(reader.ProjectReaderError, match="cancelled"):
        reader.read_bytes(tmp_path, "docs/huge.md", cancelled=lambda: True)
    with pytest.raises(reader.ProjectReaderError, match="time_deadline"):
        reader.list_names(tmp_path, deadline_monotonic=time.monotonic() - 1)
    monkeypatch.setattr(reader, "_key", lambda: (_ for _ in ()).throw(reader.ProjectReaderError("continuation_unavailable")))
    result = reader.page_text(b"a" * 10000, relative_name="docs/read.md", scope={})
    assert result["continuation_status"] == "continuation_unavailable"
    assert result["next_cursor"] is None and not result["body_complete"]
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["has_more"] and not result["coverage"]["complete"]


def test_inplace_change_detected_on_same_fd(tmp_path, monkeypatch):
    file = _file(tmp_path, "docs/read.md", "a" * 100)
    original = os.read
    def changing(fd, amount):
        data = original(fd, amount)
        if data:
            file.write_text("b" * 100)
        return data
    monkeypatch.setattr(os, "read", changing)
    with pytest.raises(reader.ProjectReaderError, match="source_changed"):
        reader.read_bytes(tmp_path, "docs/read.md")


def test_default_redaction_unchanged_and_reader_preserves_newlines():
    text = "-----BEGIN " + "PRIVATE KEY-----\none\ntwo\n-----END PRIVATE KEY-----"
    assert redact_text(text) == "***REDACTED_PEM***"
    safe = redact_text(text, preserve_newlines=True)
    assert safe.count("\n") == 3 and not safe.endswith("\n")
    assert redact_text(safe) == safe


def test_sensitive_names_are_not_exposed_in_cursor(tmp_path):
    _file(tmp_path, "docs/token=PRIVATE-VALUE/read.md")
    for n in range(50):
        _file(tmp_path, f"docs/{n}.md")
    result = reader.project_files(tmp_path, relative_name="docs", scope={})
    assert "PRIVATE-VALUE" not in str(result)
    with pytest.raises(reader.ProjectReaderError, match="permission_denied"):
        reader.project_files(tmp_path, action="read", relative_name="docs/token=PRIVATE-VALUE/read.md", scope={})


def test_project_root_fd_remains_pinned_during_replacement(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    _file(root, "docs/read.md", "original")
    other = tmp_path / "other"
    other.mkdir()
    _file(other, "docs/read.md", "replacement")
    original = reader.read_bytes
    def swapped(fd, name, **kwargs):
        root.rename(tmp_path / "old")
        other.rename(root)
        return original(fd, name, **kwargs)
    monkeypatch.setattr(reader, "read_bytes", swapped)
    result = reader.project_files(root, action="read", relative_name="docs/read.md", scope={})
    assert result["text"] == "original"


def test_transient_retry_shared_across_syscalls(tmp_path, monkeypatch):
    import errno
    _file(tmp_path, "docs/read.md", "a" * 70000)
    original = os.read
    calls = 0
    def interrupted(fd, amount):
        nonlocal calls
        calls += 1
        if calls in {1, 3}:
            raise OSError(errno.EAGAIN, "transient")
        return original(fd, amount)
    monkeypatch.setattr(os, "read", interrupted)
    with pytest.raises(reader.ProjectReaderError, match="io_error"):
        reader.read_bytes(tmp_path, "docs/read.md")
    assert calls == 3


def test_filters_and_scope_are_explicit(tmp_path):
    _file(tmp_path, "docs/read.md")
    with pytest.raises(reader.ProjectReaderError, match="INPUT_ERROR"):
        reader.project_files(tmp_path, action="read", relative_name="docs/read.md", query="ignored", scope={})
    with pytest.raises(reader.ProjectReaderError, match="line_controls_require_read"):
        reader.project_files(tmp_path, action="list", max_lines=1, scope={})
    result = reader.project_files(tmp_path, action="read", relative_name="docs/read.md", scope={"config_key": "us", "config_path": "/private/secret"})
    assert result["scope"]["config_key"] == "us" and "config_path" not in result["scope"]
    assert result["scope"]["body_range"] == result["body_range"]


def test_list_is_one_layer_and_root_implementation_directories_lead(tmp_path, monkeypatch):
    for directory in ("src", "domain", "agent-runtime", "docs"):
        _file(tmp_path, f"{directory}/nested/file.py", "needle")
    _file(tmp_path, "configs/examples/config.yaml.example")
    _file(tmp_path, "AGENTS.md")
    for n in range(80):
        _file(tmp_path, f"docs/{n}.md")
    original = reader.list_names
    visited = []
    def list_names(root, name="", **kwargs):
        visited.append(name)
        return original(root, name, **kwargs)
    monkeypatch.setattr(reader, "list_names", list_names)
    root = reader.project_files(tmp_path, scope={})
    assert visited == [""]
    assert [row["relative_name"] for row in root["entries"]] == ["src", "domain", "agent-runtime", "docs", "configs", "AGENTS.md"]
    assert [row["kind"] for row in root["entries"]] == ["directory"] * 5 + ["file"]
    assert all("size_bytes" not in row for row in root["entries"][:-1])
    assert root["coverage"]["complete"] and root["next_cursor"] is None
    child = reader.project_files(tmp_path, relative_name="src", scope={})
    assert child["entries"] == [{"relative_name": "src/nested", "kind": "directory"}]
    with pytest.raises(reader.ProjectReaderError):
        reader.project_files(tmp_path, action="read", relative_name="src/nested", scope={})


def test_search_priority_preserves_all_pages_and_explicit_scope(tmp_path):
    expected = []
    for directory in ("src", "domain", "agent-runtime", "docs"):
        for n in range(25):
            expected.append(f"{directory}/{n:02}.py")
            _file(tmp_path, expected[-1], "needle")
    _file(tmp_path, "configs/examples/config.yaml.example", "needle")
    _file(tmp_path, "README.md", "needle")
    expected += ["configs/examples/config.yaml.example", "README.md"]
    entries, cursor = [], None
    for _ in range(100):
        page = reader.project_files(tmp_path, action="search", query="needle", scope={}, cursor=cursor)
        entries += [row["relative_name"] for row in page["entries"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert entries == expected
    for directory in ("docs", "src"):
        page = reader.project_files(tmp_path, action="search", relative_name=directory, query="needle", scope={})
        assert page["entries"]
        assert all(row["relative_name"].startswith(directory + "/") for row in page["entries"])


def test_docs_only_match_after_full_nonmatching_source_page(tmp_path):
    for n in range(201):
        _file(tmp_path, f"src/{n:04}.py", "absent")
    _file(tmp_path, "docs/answer.md", "unique-target")
    first = reader.project_files(tmp_path, action="search", query="unique-target", scope={})
    assert first["entries"] == [] and first["scanned"] == 200
    assert first["coverage"]["has_more"] and not first["coverage"]["complete"]
    second = reader.project_files(tmp_path, action="search", query="unique-target", scope={}, cursor=first["next_cursor"])
    direct = reader.project_files(tmp_path, action="search", query="unique-target", relative_name="docs", scope={})
    assert [row["relative_name"] for row in second["entries"]] == ["docs/answer.md"]
    assert second["entries"] == direct["entries"]


@pytest.mark.parametrize("action", ["list", "search"])
def test_navigation_cursor_rejects_old_traversal_and_other_queries(tmp_path, action):
    for n in range(50):
        _file(tmp_path, f"docs/{n:02}.md", "needle")
    args = {"action": action, "relative_name": "docs", "query": "needle" if action == "search" else "", "scope": {}}
    first = reader.project_files(tmp_path, **args)
    cursor = first["next_cursor"]
    assert cursor
    for change in ({"action": "search" if action == "list" else "list", "query": "needle"}, {"query": "other"}, {"relative_name": ""}, {"scope": {"account": "sy"}}):
        with pytest.raises(reader.ProjectReaderError, match="cursor_invalidated"):
            reader.project_files(tmp_path, **{**args, **change, "cursor": cursor})
    info = tmp_path.stat()
    old = reader.decode_evidence_cursor(cursor, reader._key())
    for previous in ({}, {"traversal_version": "project-navigation-v2"}):
        old["binding"] = reader.digest({"scope": {"root": [info.st_dev, info.st_ino]}, "action": action, "resource": "project", "name": "docs", "query": args["query"], **previous})
        with pytest.raises(reader.ProjectReaderError, match="cursor_invalidated"):
            reader.project_files(tmp_path, **args, cursor=reader.encode_evidence_cursor(old, reader._key()))


def test_shallow_list_ignores_child_changes_and_hides_unsafe_entries(tmp_path):
    _file(tmp_path, "docs/child/file.md")
    for n in range(50):
        _file(tmp_path, f"docs/{n:02}.md")
    (tmp_path / "docs/link").symlink_to(tmp_path / "docs/child", target_is_directory=True)
    os.mkfifo(tmp_path / "docs/pipe.md")
    _file(tmp_path, "docs/secret-directory/file.md")
    first = reader.project_files(tmp_path, relative_name="docs", scope={})
    _file(tmp_path, "docs/child/new.md")
    second = reader.project_files(tmp_path, relative_name="docs", scope={}, cursor=first["next_cursor"])
    names = [row["relative_name"] for row in first["entries"] + second["entries"]]
    assert "docs/child" in names
    assert not any("new.md" in name or "link" in name or "pipe" in name or "secret" in name for name in names)


@pytest.mark.parametrize("action", ["list", "search"])
def test_project_continuation_missing_key_and_cancel_remain_bounded(tmp_path, monkeypatch, action):
    for n in range(50):
        _file(tmp_path, f"docs/{n:02}.md", "needle")
    args = {"action": action, "relative_name": "docs", "query": "needle" if action == "search" else "", "scope": {}}
    for extra, reason in (({"cancelled": lambda: True}, "cancelled"), ({"deadline_monotonic": time.monotonic() - 1}, "time_deadline")):
        with pytest.raises(reader.ProjectReaderError, match=reason):
            reader.project_files(tmp_path, **args, **extra)
    monkeypatch.setattr(reader, "_key", lambda: (_ for _ in ()).throw(reader.ProjectReaderError("continuation_unavailable")))
    page = reader.project_files(tmp_path, **args)
    assert page["continuation_status"] == "continuation_unavailable"
    assert page["next_cursor"] is None and page["coverage"]["has_more"]
    assert page["coverage"]["status"] == "partial"


@pytest.mark.parametrize("name", ["README.md", "src/target.py", "configs/examples/config.yaml.example"])
def test_single_file_search_reads_only_target_and_needs_no_signer(tmp_path, monkeypatch, name):
    target = _file(tmp_path, name, "needle first\nneedle second\n")
    _file(tmp_path, "src/sibling.py", "needle sibling must not be read")
    original = reader.read_bytes
    reads = []
    def read_target(root, relative_name, **kwargs):
        reads.append(relative_name)
        assert relative_name == name
        return original(root, relative_name, **kwargs)
    def forbidden(*_args, **_kwargs):
        pytest.fail("a single-file search neither lists siblings nor signs a continuation")
    monkeypatch.setattr(reader, "read_bytes", read_target)
    monkeypatch.setattr(reader, "list_names", forbidden)
    monkeypatch.setattr(reader, "_key", forbidden)
    for query, count in (("needle", 1), ("NEEDLE", 0), ("absent", 0)):
        page = reader.project_files(tmp_path, action="search", relative_name=name, query=query, scope={})
        assert len(page["entries"]) == count
        assert page["scanned"] == 1 and page["next_cursor"] is None
        assert page["coverage"]["complete"] and not page["coverage"]["has_more"]
        if count:
            assert page["entries"][0]["line"] == 1
            assert page["entries"][0]["content_hash"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert reads == [name] * 3


@pytest.mark.parametrize("relative_name", ["src/context.py", "src"])
def test_search_returns_eight_context_lines_and_first_literal_match(tmp_path, relative_name):
    query = "literal (alpha) + [beta]"
    lines = [f"line {index:02}\n" for index in range(1, 33)]
    lines[15] = query + "\n"
    lines[28] = query + " again\n"
    _file(tmp_path, "src/context.py", "".join(lines))
    page = reader.project_files(tmp_path, action="search", relative_name=relative_name, query=query, scope={})
    assert len(page["entries"]) == 1
    entry = page["entries"][0]
    assert entry["line"] == 16
    assert (entry["context_start_line"], entry["context_end_line"]) == (8, 24)
    assert entry["text"].rstrip("\n") == "".join(lines[7:24]).rstrip("\n")
    assert len(entry["text"]) <= 1800


@pytest.mark.parametrize("char", ["x", "é", "规"])
def test_search_long_line_keeps_full_literal_and_accurate_redacted_context(tmp_path, char):
    query = "[literal]+(" + "needle" * 70 + ")"
    original = ("before\n-----BEGIN " + "PRIVATE KEY-----\nHIDDEN-CONTENT\n-----END PRIVATE KEY-----\n"
                + char * 5000 + query + char * 5000 + "\ntail\n")
    _file(tmp_path, "src/long.py", original)
    page = reader.project_files(tmp_path, action="search", relative_name="src/long.py", query=query, scope={})
    entry = page["entries"][0]
    safe = redact_text(original, preserve_newlines=True)
    assert query in entry["text"] and len(entry["text"]) <= 1800
    assert "HIDDEN-CONTENT" not in str(page)
    start = safe.index(entry["text"])
    end = start + len(entry["text"])
    assert entry["line"] == 5
    assert entry["context_start_line"] == safe.count("\n", 0, start) + 1
    assert entry["context_end_line"] == safe.count("\n", 0, end - 1) + 1
    assert 1 <= entry["context_start_line"] <= entry["line"] <= entry["context_end_line"] <= 6


@pytest.mark.parametrize("resource", ["run", "daily_decision_brief_read", "option_performance_report"])
def test_non_project_body_keeps_existing_character_ceiling(resource):
    page = reader.page_text(b"x" * 10000, relative_name="body", scope={}, resource=resource)
    assert page["text"] == "x" * 4000
    assert page["body_range"]["end_char"] == 4000
    assert page["next_cursor"] and not page["body_complete"]


def test_project_read_no_longer_stops_at_4000_ascii_characters(tmp_path):
    _file(tmp_path, "README.md", "x" * 5000)
    page = reader.project_files(tmp_path, action="read", relative_name="README.md", scope={})
    assert len(page["text"]) > 4000


@pytest.mark.parametrize("quote", ['"', "'", ""])
@pytest.mark.parametrize("key", ["api_key", "password", "client-secret", "refresh_token"])
def test_sensitive_key_quotes_preserve_source_lines(quote, key):
    original = f"{quote}{key}{quote}: \"SYNTHETIC_PRIVATE_A\nSYNTHETIC_PRIVATE_B\"\npublic = 'visible'"
    safe = redact_text(original, preserve_newlines=True)
    assert "SYNTHETIC_PRIVATE" not in safe
    assert f"{quote}{key}{quote}: ***REDACTED***" in safe
    assert safe.count("\n") == original.count("\n")
    assert safe.endswith("public = 'visible'")
    assert redact_text(safe) == safe

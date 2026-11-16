"""No providers/ledger: builtin trees and isolated Unix sockets only."""
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import tempfile
import weakref

import pytest

from src.application.trades import owner_diagnostic as d


@pytest.fixture
def tmp_path():
    # AF_UNIX paths on macOS are shorter than pytest's descriptive default paths.
    with tempfile.TemporaryDirectory(prefix="om-od-test-", dir=Path("/tmp").resolve()) as folder:
        yield Path(folder)


def roots(value=None):
    return dict(zip(d.ROOT_NAMES, [{"entry": value}, {}, {}]))


def generous(**kw):
    return d.Limits(**({"seconds": 5, "nodes": 100000, "metadata_bytes": 32*1024*1024} | kw))


def test_shared_per_root_union_cycles_and_no_values():
    shared = ["SECRET-BUSINESS-VALUE", 4567.25]
    graph = {"brief_cache": {"x": shared}, "inbox_cache": {"y": shared}, "lifecycle_cache": {}}
    result = d.aggregate(graph, limits=generous())
    assert result["complete"]
    sizes = {r["type"]: r for r in result["types"]}
    assert sizes["float"]["count"] == 1
    assert [next(t["count"] for t in r["types"] if t["type"] == "float") for r in result["roots"][:2]] == [1, 1]
    assert "SECRET-BUSINESS-VALUE" not in json.dumps(result)
    cycle = []; cycle.append(cycle)
    assert d.aggregate(roots(cycle), limits=generous())["complete"]


def test_exact_small_graph_bytes_and_aliases():
    leaf = b"synthetic-only"
    shared = [leaf]
    first = {"first": shared, "alias": shared}
    second = {"second": shared}
    third = {}
    graph = dict(zip(d.ROOT_NAMES, [first, second, third]))

    # An explicit finite object inventory, not a second traversal implementation.
    def expected(objects):
        result = {}
        for value in {id(value): value for value in objects}.values():
            name = type(value).__name__
            row = result.setdefault(name, {"type": name, "count": 0, "bytes": 0})
            row["count"] += 1
            row["bytes"] += sys.getsizeof(value)
        return result

    objects = [[first, *first.keys(), shared, leaf],
               [second, *second.keys(), shared, leaf], [third]]
    result = d.aggregate(graph, limits=generous())
    assert result["complete"]
    for row, inventory in zip(result["roots"], objects):
        assert {t["type"]: t for t in row["types"]} == expected(inventory)
    assert {t["type"]: t for t in result["types"]} == expected(sum(objects, []))


def test_mutation_keeps_current_partial_root():
    graph = roots([1])
    calls = 0

    def mutate():
        nonlocal calls
        calls += 1
        if calls == 3:
            graph["brief_cache"]["added"] = "synthetic"
        return False

    report = d.aggregate(graph, limits=generous(), cancelled=mutate)
    assert report["complete"] is False and report["stop_reason"] == "mutation"
    assert len(report["roots"]) == 1
    row = report["roots"][0]
    assert row["name"] == "brief_cache" and row["complete"] is False
    assert any(t["type"] == "dict" for t in row["types"])
    assert d._safe_response({"schema": 1, "pid": 1, "start_ticks": 1,
                             "request_id": "a" * 32, "report": report})


def test_default_limits_and_depth_boundary():
    assert d.Limits() == d.Limits(nodes=50000, depth=64, seconds=.1,
                                  metadata_bytes=8 * 1024 * 1024)
    for lists, complete in [(63, True), (64, False)]:
        value = None
        for _ in range(lists):
            value = [value]
        report = d.aggregate(roots(value), limits=generous())
        assert report["complete"] is complete
        assert report["stop_reason"] == (None if complete else "depth")


@pytest.mark.parametrize("limit,reason", [({"nodes": 1}, "nodes"), ({"depth": 0}, "depth"),
                                        ({"seconds": 0}, "time"), ({"metadata_bytes": 1}, "metadata")])
def test_limits_are_explicit_partial(limit, reason):
    result = d.aggregate(roots([[1]]), limits=generous(**limit))
    assert not result["complete"] and result["stop_reason"] == reason


def test_cancelled_and_expired_shared_deadline():
    assert d.aggregate(roots(), cancelled=lambda: True)["stop_reason"] == "cancelled"
    assert d.aggregate(roots(), deadline=time.monotonic()-1)["stop_reason"] == "time"


def test_no_user_size_or_metaclass_methods():
    class Trap(type):
        def __eq__(self, other):
            raise AssertionError("metaclass comparison")
        def __hash__(self):
            raise AssertionError("metaclass hash")
    class Unknown(metaclass=Trap):
        def __sizeof__(self):
            raise AssertionError("custom sizeof")
    assert d.aggregate(roots(Unknown()))["stop_reason"] == "unsupported"
    class Mapping(dict):
        pass
    with pytest.raises(ValueError, match="unstable_root"):
        d.aggregate(dict(zip(d.ROOT_NAMES, [Mapping(), {}, {}])))


def test_default_off_has_no_socket_or_path_read(monkeypatch):
    monkeypatch.delenv("OM_OWNER_DIAGNOSTIC_SOCKET", raising=False)
    monkeypatch.setattr(d.socket, "socket", lambda *a, **kw: pytest.fail("socket while off"))
    monkeypatch.setattr(Path, "lstat", lambda *a: pytest.fail("filesystem while off"))
    assert d.OwnerDiagnostic.from_environment() is None


def test_waiting_for_receiver_cancel_and_no_retry(tmp_path):
    p = d.OwnerDiagnostic(tmp_path/"om-owner-top-missing"/"owner.sock")
    p.poll(lambda: pytest.fail("missing receiver traversed"), cancelled=lambda: False)
    assert not p.done
    p.poll(lambda: pytest.fail("cancelled probe traversed"), cancelled=lambda: True)
    assert p.done
    p.poll(lambda: pytest.fail("cancelled probe retried"), cancelled=lambda: False)


@pytest.fixture
def local_protocol(monkeypatch):
    # Real AF_UNIX data/timeout/cleanup; Linux peer/proc syscalls shimmed on macOS.
    monkeypatch.setattr(d, "_peer", lambda s: (os.getpid(), os.getuid()))
    monkeypatch.setattr(d, "_start_ticks", lambda pid: 12345)


def wait_socket(path):
    end = time.monotonic()+2
    while not path.exists() and time.monotonic() < end:
        time.sleep(.001)
    assert path.exists()


def test_receiver_can_arrive_long_after_first_heartbeat(tmp_path, local_protocol, monkeypatch):
    path = tmp_path / "om-owner-top-late" / "owner.sock"
    probe = d.OwnerDiagnostic(path)
    clock = time.monotonic
    offset = [0]
    monkeypatch.setattr(d.time, "monotonic", lambda: clock() + offset[0])
    probe.poll(lambda: pytest.fail("no receiver"), cancelled=lambda: False)
    offset[0] = 3600  # no real sleep, and no alteration of the traversal budget
    responses, errors = [], []
    def receive():
        try:
            responses.append(d.receive(path, pid=os.getpid(), start_ticks=12345, seconds=.3))
        except Exception as error:
            errors.append(type(error).__name__)
    thread = threading.Thread(target=receive)
    thread.start(); wait_socket(path)
    probe.poll(lambda: roots([1, 2]), cancelled=lambda: False)
    thread.join(1)
    assert not thread.is_alive() and not errors and len(responses) == 1
    assert responses[0]["report"]["complete"] and probe.done
    probe.poll(lambda: pytest.fail("successful request retried"), cancelled=lambda: False)
    assert not path.exists() and not path.parent.exists()


def test_unclaimed_receiver_timeout_does_not_consume_client(tmp_path, local_protocol):
    path = tmp_path / "om-owner-top-unclaimed" / "owner.sock"
    probe = d.OwnerDiagnostic(path)
    with pytest.raises(TimeoutError):
        d.receive(path, pid=os.getpid(), start_ticks=12345, seconds=.01)
    assert not path.exists() and not path.parent.exists() and not probe.done
    probe.poll(lambda: pytest.fail("expired receiver traversed"), cancelled=lambda: False)
    assert not probe.done


def test_stale_endpoint_consumes_only_attempt(tmp_path, local_protocol):
    path = tmp_path / "om-owner-top-stale" / "owner.sock"
    path.parent.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path)); path.chmod(0o600); stale.close()
    probe = d.OwnerDiagnostic(path)
    probe.poll(lambda: pytest.fail("stale endpoint traversed"), cancelled=lambda: False)
    assert probe.done
    path.unlink(); path.parent.rmdir()
    # Even a later valid receiver cannot reset the claimed opportunity.
    errors = []
    def receive():
        try:
            d.receive(path, pid=os.getpid(), start_ticks=12345, seconds=.05)
        except TimeoutError:
            errors.append("timeout")
    thread = threading.Thread(target=receive)
    thread.start(); wait_socket(path)
    probe.poll(lambda: pytest.fail("stale request retried"), cancelled=lambda: False)
    thread.join(1)
    assert not thread.is_alive() and errors == ["timeout"]
    assert not path.exists() and not path.parent.exists()


def test_real_protocol_single_request_cleanup_and_release(tmp_path, local_protocol):
    path = tmp_path/"om-owner-top-success"/"owner.sock"
    responses, errors, refs = [], [], []
    def receiver():
        try:
            responses.append(d.receive(path, pid=os.getpid(), start_ticks=12345, seconds=2))
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=receiver)
    thread.start(); wait_socket(path)
    probe = d.OwnerDiagnostic(path)
    class Leaf:
        pass
    def factory():
        leaf = Leaf(); refs.append(weakref.ref(leaf))
        return roots(leaf)
    probe.poll(factory, cancelled=lambda: False)
    probe.poll(lambda: pytest.fail("request retried"), cancelled=lambda: False)
    thread.join(3)
    assert not thread.is_alive() and not errors
    assert responses[0]["report"]["stop_reason"] == "unsupported"
    assert probe.done and refs[0]() is None
    assert not path.exists() and not path.parent.exists()


@pytest.mark.parametrize("mode", ["slow", "wrong_pid", "disconnect", "oversize", "bad_json"])
def test_transport_failures_never_call_business_or_retry(tmp_path, local_protocol, mode):
    path = tmp_path/("om-owner-top-"+mode)/"owner.sock"
    path.parent.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path)); path.chmod(0o600); server.listen(1); server.settimeout(1)
        def peer():
            with server.accept()[0] as c:
                if mode == "slow":
                    time.sleep(.1); return
                if mode == "disconnect":
                    return
                payload = {"schema":1,"pid":os.getpid()+1,"start_ticks":12345,"request_id":"a"*32}
                c.send(b"x"*1025 if mode == "oversize" else b"{" if mode == "bad_json" else json.dumps(payload).encode())
                c.shutdown(socket.SHUT_WR)
        thread = threading.Thread(target=peer); thread.start()
        probe = d.OwnerDiagnostic(path)
        probe.poll(lambda: pytest.fail("invalid request traversed roots"), cancelled=lambda: False)
        thread.join(2)
        assert not thread.is_alive() and probe.done
        probe.poll(lambda: pytest.fail("failed request retried"), cancelled=lambda: False)


def test_permissions_and_symlinks_rejected(tmp_path, local_protocol):
    path = tmp_path/"om-owner-top-perms"/"owner.sock"; path.parent.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.bind(str(path)); path.chmod(0o666)
        probe = d.OwnerDiagnostic(path)
        probe.poll(lambda: pytest.fail("unsafe endpoint traversed"), cancelled=lambda: False)
        assert probe.done
    alias = tmp_path/"om-owner-top-alias"; alias.symlink_to(path.parent, target_is_directory=True)
    assert not d._endpoint(alias/"owner.sock")


def test_snapshot_replacement_is_stable_and_original_untouched():
    from src.application import daily_decision_brief_repository as h
    previous = h._VALIDATED_BRIEF_CACHE
    try:
        h._VALIDATED_BRIEF_CACHE = {("synthetic",): ({"immutable": [3,4]}, ())}
        snap = h._diagnostic_validated_brief_cache_snapshot()
        thread = threading.Thread(target=h._VALIDATED_BRIEF_CACHE.clear)
        thread.start(); thread.join()
        result = d.aggregate({"brief_cache":snap, "inbox_cache":{}, "lifecycle_cache":{}}, limits=generous())
        assert result["complete"] and len(snap) == 1 and h._VALIDATED_BRIEF_CACHE == {}
    finally:
        h._VALIDATED_BRIEF_CACHE = previous


def test_claim_is_shared_by_sources(tmp_path, local_protocol):
    path = tmp_path/"om-owner-top-claim"/"owner.sock"; path.parent.mkdir(mode=0o700)
    path.touch(mode=0o600)  # unsafe regular file consumes the one attempt
    probe = d.OwnerDiagnostic(path)
    threads = [threading.Thread(target=lambda: probe.poll(lambda: pytest.fail("unsafe path"), cancelled=lambda: False)) for _ in range(8)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(1)
    assert probe.done and all(not t.is_alive() for t in threads)


def test_receiver_refuses_existing_directory_and_identity(tmp_path, local_protocol):
    path = tmp_path/"om-owner-top-existing"/"owner.sock"; path.parent.mkdir(mode=0o700)
    marker = path.parent/"retain"; marker.write_text("synthetic")
    with pytest.raises(FileExistsError):
        d.receive(path, pid=os.getpid(), start_ticks=12345)
    assert marker.exists()
    with pytest.raises(ValueError, match="identity_changed"):
        d.receive(path, pid=os.getpid(), start_ticks=999)


def test_receiver_timeout_cleans_only_owned_paths(tmp_path, local_protocol):
    path = tmp_path/"om-owner-top-timeout"/"owner.sock"
    with pytest.raises(TimeoutError):
        d.receive(path, pid=os.getpid(), start_ticks=12345, seconds=.01)
    assert not path.exists() and not path.parent.exists()


def test_receiver_filters_unexpected_strings_and_huge_numbers():
    assert not d._safe_response({"secret":"do-not-output"})
    value = {"schema":1,"pid":os.getpid(),"start_ticks":12345,"request_id":"b"*32,
             "report":d.aggregate(roots())}
    assert d._safe_response(value)
    value["report"]["steps"] = 10**1000
    assert not d._safe_response(value)


@pytest.mark.parametrize("real_receiver", [False, True])
def test_real_listener_heartbeat_uses_diagnostic_only_after_status(
        tmp_path, monkeypatch, local_protocol, real_receiver):
    from types import SimpleNamespace
    from src.application.trades import auto_intake as a
    from test_trades_auto_intake_restart_policy import _source
    order = []
    stop = threading.Event()
    responses, errors = [], []
    path = tmp_path / "om-owner-top-heartbeat" / "owner.sock"
    consumer = None
    if real_receiver:
        def receive():
            try:
                responses.append(d.receive(path, pid=os.getpid(), start_ticks=12345, seconds=2))
            except Exception as error:
                errors.append(error)
        consumer = threading.Thread(target=receive)
        consumer.start()
        wait_socket(path)
    class Client:
        def __init__(self, **kw): pass
        def start(self, **kw): pass
        def check_health(self): pass
        def close(self): order.append("closed")
    class Probe:
        def poll(self, factory, *, cancelled):
            assert not cancelled() and order[-1] == "heartbeat"
            def checked_factory():
                snapshot = factory()
                assert set(snapshot) == set(d.ROOT_NAMES)
                assert snapshot["inbox_cache"]["entry"]["summary"] == {"counts":{}}
                assert snapshot["lifecycle_cache"]["entry"]["snapshot"] == {"pending_cases":[]}
                return snapshot
            if real_receiver:
                client = d.OwnerDiagnostic(path)
                client.poll(checked_factory, cancelled=cancelled)
                assert client.done
            else:
                checked_factory()
            order.append("probe"); stop.set()
    def inbox(path, *, cache):
        cache["entry"] = {"summary":{"counts":{}}}
        return {"counts":{}}
    def lifecycle(status, **kw):
        kw["snapshot_cache"]["entry"] = {"snapshot":{"pending_cases":[]}}
    monkeypatch.setattr(a, "OpenDTradePushListener", Client)
    monkeypatch.setattr(a, "OpenDHistoryDealClient", Client)
    monkeypatch.setattr(a, "resolve_futu_quote_route", lambda cfg: SimpleNamespace(ok=False,errors=(),status="unavailable"))
    monkeypatch.setattr(a, "_cached_trade_inbox_summary", inbox)
    monkeypatch.setattr(a, "_refresh_lifecycle_delivery_status", lifecycle)
    monkeypatch.setattr(a, "list_retryable_trade_payloads", lambda *x,**kw: [])
    monkeypatch.setattr(a, "_write_listener_status", lambda *x,**kw: order.append(kw["stage"]))
    code = a._run_listener_source_loop(source=_source(tmp_path), repo=object(), cfg={},
        cfg_path=tmp_path/"config.json", runtime_root=tmp_path, runtime_root_source="test",
        intake_cfg={"mode":"dry-run","enabled":True,"backfill":{"enabled":False}},
        apply_changes=False, receipt_callback=lambda c:{}, process_lock=threading.RLock(),
        stop_event=stop, owner_diagnostic=Probe())
    assert code == 0 and order.count("probe") == 1 and order[-1] == "stop_event"
    if consumer is not None:
        consumer.join(3)
        assert not consumer.is_alive() and not errors and len(responses) == 1
        report = responses[0]["report"]
        assert report["complete"] and report["stop_reason"] is None
        assert len(report["roots"]) == 3
        assert not path.exists() and not path.parent.exists()


def test_snapshot_refuses_dict_subclass_without_copy_hook():
    from src.application import daily_decision_brief_repository as h
    class Trap(dict):
        def copy(self): pytest.fail("custom copy")
    original = h._VALIDATED_BRIEF_CACHE
    try:
        h._VALIDATED_BRIEF_CACHE = Trap()
        with pytest.raises(ValueError): h._diagnostic_validated_brief_cache_snapshot()
    finally:
        h._VALIDATED_BRIEF_CACHE = original


def test_slow_consumer_does_not_make_client_wait_for_read(tmp_path, local_protocol):
    path = tmp_path/"om-owner-top-slow-consumer"/"owner.sock"; path.parent.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path)); path.chmod(0o600); server.listen(1)
        def peer():
            with server.accept()[0] as c:
                c.sendall(json.dumps({"schema":1,"pid":os.getpid(),"start_ticks":12345,"request_id":"c"*32}).encode())
                c.shutdown(socket.SHUT_WR)
                time.sleep(.15)  # no recv: small fixed response can be buffered
        t = threading.Thread(target=peer); t.start()
        probe = d.OwnerDiagnostic(path); before = time.monotonic()
        probe.poll(lambda:roots([1,2,3]), cancelled=lambda:False)
        elapsed = time.monotonic()-before
        t.join(1)
        assert probe.done and not t.is_alive() and elapsed < .12


def test_receiver_cancel_releases_own_endpoint(tmp_path, local_protocol, monkeypatch):
    path = tmp_path/"om-owner-top-cancel"/"owner.sock"
    original = socket.socket
    class CancelSocket:
        def __init__(self,*a,**kw): self.raw = original(*a,**kw)
        def __enter__(self): return self
        def __exit__(self,*a): self.raw.close()
        def __getattr__(self,name): return getattr(self.raw,name)
        def accept(self): raise KeyboardInterrupt
    monkeypatch.setattr(d.socket,"socket",CancelSocket)
    with pytest.raises(KeyboardInterrupt):
        d.receive(path,pid=os.getpid(),start_ticks=12345)
    assert not path.exists() and not path.parent.exists()


def test_native_linux_credentials_decoder(monkeypatch):
    import struct
    monkeypatch.setattr(d.socket,"SO_PEERCRED",17,raising=False)
    class Sock:
        def getsockopt(self,level,option,size):
            assert (level,option,size)==(socket.SOL_SOCKET,17,12)
            return struct.pack("3i",77,88,99)
    assert d._peer(Sock()) == (77,88)


@pytest.mark.parametrize("path", ["/run/../var/lib/om-owner-top-outside/owner.sock",
                                 "/run/mounted/om-owner-top-nested/owner.sock"])
def test_environment_rejects_noncanonical_or_nested_run_without_io(monkeypatch, path):
    monkeypatch.setattr(d.sys,"platform","linux")
    monkeypatch.setenv("OM_OWNER_DIAGNOSTIC_SOCKET",path)
    monkeypatch.setattr(Path,"lstat",lambda *a:pytest.fail("invalid path touched filesystem"))
    assert d.OwnerDiagnostic.from_environment() is None


def test_receiver_rejects_parent_traversal_before_proc_or_directory(monkeypatch):
    monkeypatch.setattr(d,"_start_ticks",lambda *a:pytest.fail("bad path read proc"))
    monkeypatch.setattr(Path,"mkdir",lambda *a,**kw:pytest.fail("bad path created directory"))
    with pytest.raises(ValueError,match="invalid_receiver_scope"):
        d.receive(Path("/run/../var/lib/om-owner-top-outside/owner.sock"),pid=1,start_ticks=1)


def test_service_layout_and_bootstrap_parent_validation_without_production_io(monkeypatch):
    from types import SimpleNamespace
    import stat
    path = Path(f"/run/om-owner-diagnostics-{os.getuid()}/om-owner-top-isolated/owner.sock")
    monkeypatch.setattr(d.sys,"platform","linux")
    monkeypatch.setenv("OM_OWNER_DIAGNOSTIC_SOCKET",str(path))
    monkeypatch.setattr(Path,"lstat",lambda p:SimpleNamespace(st_uid=os.getuid(),st_mode=stat.S_IFDIR|0o700))
    assert d.OwnerDiagnostic.from_environment() is not None and d._service_parent(path)
    monkeypatch.setattr(Path,"lstat",lambda p:SimpleNamespace(st_uid=os.getuid()+1,st_mode=stat.S_IFDIR|0o700))
    assert not d._service_parent(path)
    monkeypatch.setattr(Path,"lstat",lambda p:SimpleNamespace(st_uid=os.getuid(),st_mode=stat.S_IFDIR|0o755))
    assert not d._service_parent(path)
    monkeypatch.setattr(Path,"lstat",lambda p:SimpleNamespace(st_uid=os.getuid(),st_mode=stat.S_IFLNK|0o700))
    assert not d._service_parent(path)


def test_unsafe_service_base_rejected_before_mkdir(monkeypatch, local_protocol):
    path = Path(f"/run/om-owner-diagnostics-{os.getuid()}/om-owner-top-denied/owner.sock")
    monkeypatch.setattr(d,"_service_parent",lambda p:False)
    monkeypatch.setattr(Path,"mkdir",lambda *a,**kw:pytest.fail("unsafe base created child"))
    with pytest.raises(ValueError,match="unsafe_service_base"):
        d.receive(path,pid=os.getpid(),start_ticks=12345)


def test_receiver_does_not_remove_replaced_endpoint(tmp_path, local_protocol, monkeypatch):
    path = tmp_path/"om-owner-top-replaced"/"owner.sock"
    original = d._safe_response
    def replace(response):
        assert original(response)
        path.rename(path.with_name("retained-original.sock"))
        path.write_text("do not delete replacement")
        return True
    monkeypatch.setattr(d,"_safe_response",replace)
    result, errors = [], []
    def receiver():
        try: result.append(d.receive(path,pid=os.getpid(),start_ticks=12345,seconds=2))
        except Exception as error: errors.append(error)
    t = threading.Thread(target=receiver); t.start(); wait_socket(path)
    d.OwnerDiagnostic(path).poll(lambda:roots(),cancelled=lambda:False)
    t.join(3)
    assert not errors and result and not t.is_alive()
    assert path.read_text() == "do not delete replacement" and path.parent.exists()


def test_installed_module_receiver_entry_help_has_no_business_effect():
    import subprocess
    import sys
    result = subprocess.run([sys.executable,"-B","-m","src.application.trades.owner_diagnostic","--help"],
                            capture_output=True,text=True,timeout=10)
    assert result.returncode == 0 and "--start-ticks" in result.stdout and not result.stderr

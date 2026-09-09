import multiprocessing
from src.application.trades import inbox

from src.application.trades.inbox import begin_trade_receipt_attempt, enqueue_trade_payload, read_trade_payload


def _claim_receipt(path, inbox_id, ready, release, results):
    ready.put(True)
    if not release.wait(10):
        raise RuntimeError("receipt test barrier timed out")
    result = begin_trade_receipt_attempt(
        path, inbox_id=inbox_id, route={"provider": "offline", "channel": "test", "target": "fixture"},
        message="one ordinary receipt",
    )
    results.put(result)


def test_two_processes_can_claim_only_one_ordinary_receipt(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    inbox_id = enqueue_trade_payload(path, payload={"deal_id": "fill-1", "futu_account_id": "123"},
                                     source="push", broker_deal_key="futu:lx:123:fill-1")
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET receipt_recovery_allowed = 1 WHERE inbox_id = ?", (inbox_id,))
    claim = inbox.claim_trade_payload(path, inbox_id=inbox_id)
    result = inbox.save_trade_payload_result(path, claim=claim, result={"status": "applied"})
    inbox.settle_trade_payload_result(path, inbox_id=inbox_id, claim=claim, result=result)
    context = multiprocessing.get_context("spawn")
    ready, results, release = context.Queue(), context.Queue(), context.Event()
    workers = [context.Process(target=_claim_receipt, args=(str(path), inbox_id, ready, release, results))
               for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        for _ in workers:
            assert ready.get(timeout=10) is True
        release.set()
        claimed = [results.get(timeout=10) for _ in workers]
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
        assert sum(item["claimed"] for item in claimed) == 1
        assert len({item["attempt_id"] for item in claimed}) == 1
        assert {item["receipt_id"] for item in claimed} == {f"trade-receipt:{inbox_id}"}
        frozen = read_trade_payload(path, inbox_id=inbox_id)["receipt"]
        assert frozen["attempt_id"] == claimed[0]["attempt_id"]
        assert frozen["status"] == "unknown"
    finally:
        release.set()
        for worker in workers:
            worker.join(1)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)

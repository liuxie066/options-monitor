"""Minimal live admission probe; no model, market, account or query API access."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
from queue import Empty, Full, Queue
import tempfile
import threading
import time
from typing import Callable

from .config import PreflightSettings
from .feishu import EventRejected, normalize_sdk_event, redacted_sdk_sample, resolve_bot_identity
from .store import DeliveryStore, utc_text

FIXED_REPLY = "群查询bot 接入测试已收到"
LOG = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PreflightSession:
    def __init__(self, settings: PreflightSettings, bot_open_id: str, *, secret: str,
                 reply_fn: Callable | None = None, clock: Callable = _now):
        if not bot_open_id:
            raise ValueError("bot_identity_unavailable")
        self.settings, self.bot_open_id, self.secret = settings, bot_open_id, secret
        self.clock = clock
        self.store = DeliveryStore(settings.data_dir)
        self.store.cleanup(clock())
        self.created_at = clock()
        self.observations: list[dict] = []
        self.queue_rejections = 0
        self._evidence_lock = threading.Lock()
        self.stop_sending = threading.Event()
        self.evidence_path = settings.data_dir / "preflight-evidence.json"
        if reply_fn is None:
            from src.infrastructure.feishu_bot import reply_message
            reply_fn = reply_message
        self.reply_fn = reply_fn
        self.write_evidence()

    def _observe(self, reason: str, payload: object = None) -> None:
        with self._evidence_lock:
            now = self.clock()
            if now >= self.created_at + timedelta(hours=24):
                self.created_at, self.observations = now, []
            self.observations = [item for item in self.observations
                                 if item["expires_at_utc"] > utc_text(now)]
            self.observations.append({"status": reason, "created_at_utc": utc_text(now),
                                      "expires_at_utc": utc_text(now + timedelta(hours=24)),
                                      "sample": redacted_sdk_sample(payload)})
            self.observations = self.observations[-32:]
            self._write_evidence_locked(now)

    def _write_evidence_locked(self, now: datetime) -> None:
        # The batch TTL is fixed; rewriting a report does not renew its old observations.
        expiry = self.created_at + timedelta(hours=24)
        if now >= expiry:
            self.created_at, self.observations = now, []
            expiry = now + timedelta(hours=24)
        self.observations = [item for item in self.observations if item["expires_at_utc"] > utc_text(now)]
        payload = {
            "schema_version": "group_query_bot_preflight.v1",
            "status": "preflight_only",
            "live_g2_verified": False,
            "created_at_utc": utc_text(self.created_at),
            "expires_at_utc": utc_text(expiry),
            "config_sha256": self.settings.public_digest(),
            "bot_identity_sha256": hashlib.sha256(self.bot_open_id.encode()).hexdigest(),
            "bot_open_id_alias": "anon_" + hashlib.sha256(self.bot_open_id.encode()).hexdigest()[:24],
            "configured_app_id_alias": "anon_" + hashlib.sha256(self.settings.app_id.encode()).hexdigest()[:24],
            "queue_rejections": self.queue_rejections,
            "deliveries": self.store.counts(now),
            "observations": self.observations,
        }
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.settings.data_dir,
                                             prefix=".preflight-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            temporary.replace(self.evidence_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def write_evidence(self) -> None:
        with self._evidence_lock:
            now = self.clock()
            self.store.cleanup(now)
            self._write_evidence_locked(now)

    def handle_event(self, payload: object) -> dict:
        try:
            event = normalize_sdk_event(payload, self.settings.app_id, self.bot_open_id,
                                        self.settings.allowed_chat_ids)
        except EventRejected as exc:
            reason = str(exc)
            self._observe(reason, payload)
            return {"status": "rejected", "reason": reason}
        row, claimed = self.store.claim(event, self.settings.instance_id, self.clock())
        if not claimed:
            self._observe("duplicate", payload)
            return {"status": "duplicate", "delivery_state": row["state"]}
        started = False

        def before_send():
            nonlocal started
            if self.stop_sending.is_set():
                raise RuntimeError("preflight_stopping")
            self.store.mark_sending(row["message_key"])
            started = True

        try:
            response = self.reply_fn(app_id=self.settings.app_id, app_secret=self.secret,
                                     message_id=event.message_id, text=FIXED_REPLY,
                                     uuid=row["delivery_uuid"], reply_in_thread=bool(event.thread_id),
                                     before_send=before_send)
            if not started:
                state = "outcome_unknown"
            elif (isinstance(response, dict) and type(response.get("code")) is int
                  and response["code"] == 0 and isinstance(response.get("data"), dict)
                  and isinstance(response["data"].get("message_id"), str)
                  and response["data"]["message_id"].strip()):
                state = "acknowledged"
            elif isinstance(response, dict) and type(response.get("code")) is int and response["code"] != 0:
                state = "failed"
            else:
                state = "outcome_unknown"
        except Exception as exc:
            from src.infrastructure.feishu_bitable import (
                FeishuAuthError, FeishuPermanentError, FeishuPermissionError, FeishuRateLimitError,
            )
            # The transport owner distinguishes explicit rejection from uncertain delivery.
            explicit_failure = isinstance(exc, (FeishuAuthError, FeishuPermissionError, FeishuRateLimitError)) or (
                isinstance(exc, FeishuPermanentError) and (
                    exc.code is not None or
                    (isinstance(exc.response, dict) and isinstance(exc.response.get("http_status"), int)
                     and 400 <= exc.response["http_status"] < 500)
                )
            )
            state = "failed" if not started or explicit_failure else "outcome_unknown"
        self.store.finish(row["message_key"], state)
        self._observe(state, payload)
        return {"status": state, "scope_sha256": row["scope_sha256"]}


class PreflightWorker:
    """One fixed-reply worker; the authenticated SDK callback only enqueues."""
    def __init__(self, session: PreflightSession, *, queue_size: int = 16):
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.session = session
        self.queue: Queue = Queue(maxsize=queue_size)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="group-query-preflight", daemon=True)
        self.thread.start()

    def submit(self, payload: object) -> bool:
        if self.stopping.is_set():
            return False
        try:
            self.queue.put_nowait(payload)
            return True
        except Full:
            self.session.queue_rejections += 1
            LOG.warning("group_query_bot_preflight_queue_full")
            return False

    def _run(self):
        maintenance_at = time.monotonic() + 60
        while not self.stopping.is_set():
            if time.monotonic() >= maintenance_at:
                try:
                    self.session.write_evidence()
                except Exception:
                    LOG.error("group_query_bot_preflight_cleanup_failed")
                maintenance_at = time.monotonic() + 60
            try:
                payload = self.queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                self.session.handle_event(payload)
            except Exception:
                # No payload or provider exception text is safe to include in logs.
                LOG.error("group_query_bot_preflight_worker_failed")
            finally:
                self.queue.task_done()

    def close(self) -> bool:
        self.stopping.set()
        self.session.stop_sending.set()
        self.thread.join(timeout=1)
        return not self.thread.is_alive()


def run_live(settings: PreflightSettings, *, secret_provider=None,
             identity_transport: Callable | None = None, reply_fn: Callable | None = None,
             start_ws_fn: Callable | None = None, clock: Callable = _now) -> dict:
    from src.application.secret_store import resolve_secret
    secret = resolve_secret(settings.app_secret_ref, provider=secret_provider)
    if not secret:
        raise ValueError("dedicated_feishu_credential_missing")
    bot_id = resolve_bot_identity(settings, secret, identity_transport)
    session = PreflightSession(settings, bot_id, secret=secret, reply_fn=reply_fn, clock=clock)
    worker = PreflightWorker(session)
    if start_ws_fn is None:
        from src.infrastructure.feishu_ws_client import start_feishu_ws_client
        start_ws_fn = start_feishu_ws_client
    try:
        start_ws_fn(app_id=settings.app_id, app_secret=secret, on_event=worker.submit,
                    log_level="error")
    except KeyboardInterrupt:
        pass
    finally:
        completed = worker.close()
        session.write_evidence()
    return {"status": "stopped" if completed else "stopped_with_pending_io",
            "live_g2_verified": False, "evidence_path": str(session.evidence_path)}

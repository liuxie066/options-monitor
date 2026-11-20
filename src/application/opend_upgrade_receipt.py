"""OM receipt and delivery boundary for the installed market OpenD plugin."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import socket
from zoneinfo import ZoneInfo

from src.application.file_locks import single_instance_lock
from src.application.plugin_runtime import PluginUnavailableError, require_plugin
from src.application.reports.delivery import deliver_report
from src.application.reports.registry import DELIVERY_FEISHU_APP
from src.application.service_deploy import validate_opend_upgrade_units
from src.infrastructure.io_utils import atomic_write_json


def _message(receipt: dict) -> str:
    state = {
        "already_current": "运行版本不低于官方版本",
        "upgraded": "已升级，运行版本及登录验证通过",
        "upgrade_failed": "升级失败",
        "blocked": "检查或升级受阻",
    }[receipt["status"]]
    lines = [
        f"**OpenD 周度维护回执** · {receipt['host']}",
        f"{receipt['checked_at']} · {state}",
        f"富途官方版本 {receipt.get('official') or '未确认'}",
    ]
    for unit in receipt["units"]:
        installed = receipt.get("installed", {}).get(unit, "未确认")
        active = receipt["official"] if unit in receipt.get("verified", []) else installed
        verified = receipt.get("rollback_verified", {}).get(unit)
        if verified is not None:
            active = f"{installed}（回滚验证通过）" if verified else "回滚未验证"
        lines.append(f"- {unit} · 原版本 {installed} · 结果 {active if receipt['status'] != 'blocked' else '未确认'}")
    if receipt.get("detail"):
        lines.append(receipt["detail"])
    return "\n".join(lines)


def _execute(units: list[str], runtime_root: Path, confirm: bool) -> dict:
    try:
        upgrade = require_plugin("market-opend").run_opend_upgrade
    except (PluginUnavailableError, AttributeError) as exc:
        return {"units": units, "status": "blocked", "error": getattr(exc, "code", "OPEND_PLUGIN_TOO_OLD")}
    return upgrade(units=units, runtime_root=runtime_root, confirm=confirm)


def run_opend_upgrade(
    *, repo_root: Path, runtime_root: Path, units: list[str], confirm: bool, send_receipt: bool,
) -> dict:
    validate_opend_upgrade_units(units)
    if not confirm:
        return _execute(units, runtime_root, False)

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.weekday() != 6 or now.hour != 10 or now.minute >= 20:
        return {"units": units, "status": "blocked", "detail": "outside the Sunday OpenD maintenance window"}
    receipt_path = runtime_root / "opend_upgrade_status.json"
    with single_instance_lock(runtime_root / "locks" / "opend-upgrade-receipt.lock",
                              busy_message="another OpenD maintenance run is active"):
        try:
            previous = json.loads(receipt_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            previous = {}
        run_id = now.date().isoformat()
        if previous.get("run_id") == run_id:
            if set(previous["units"]) == set(units):
                return previous
            return {"units": units, "status": "blocked", "detail": "today's receipt covers a different unit set"}
        receipt = _execute(units, runtime_root, True)
        receipt.update(host=socket.gethostname(), run_id=run_id, checked_at=now.isoformat(),
                       delivery={"status": "unconfirmed" if send_receipt else "disabled"})
        atomic_write_json(receipt_path, receipt)
        if send_receipt:
            delivery = deliver_report(report_id="opend-weekly-upgrade", run_id=f"{receipt['host']}:{run_id}",
                                      channel=DELIVERY_FEISHU_APP, markdown=_message(receipt), base=repo_root)
            receipt["delivery"] = {
                key: delivery[key] for key in ("status", "error_code", "message_id", "ambiguous_send")
                if key in delivery
            }
            atomic_write_json(receipt_path, receipt)
        return receipt

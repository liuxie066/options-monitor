"""Consent-gated metadata selection for setup; no configuration writes."""
from __future__ import annotations

from dataclasses import dataclass, field
from getpass import GetPassWarning, getpass
from typing import Callable
import warnings

from src.application.futu_doctor import discover_account_candidates


@dataclass(frozen=True)
class AccountSelection:
    action: str
    account_id: str | None = field(default=None, repr=False)
    validation: str = "pending_validation"
    discovery_status: str | None = None


def select_account(
    *, host: str, port: int, market: str, trd_env: str,
    input_fn: Callable[[str], str] = input,
) -> AccountSelection:
    """Return an actual ID in memory, or change_endpoint/cancelled to the parent.

    metadata_matched means an explicit current-list market/environment match,
    never final connection/asset validation. Manual IDs remain pending_validation
    even after saving. The parent retains the final doctor check and handles
    EOF/Ctrl-C alongside its other unsaved fields. Each retry needs new consent.
    """
    status = None
    while True:
        action = input_fn(
            "检查连接并选择账户将连接指定 OpenD，只读取账户元数据（不查资金/持仓、不解锁）。"
            "输入 yes 同意查询；manual 离线填写；change 修改连接；exit 退出 [manual]: "
        ).strip().lower() or "manual"
        if action == "change":
            return AccountSelection("change_endpoint", discovery_status=status)
        if action == "exit":
            return AccountSelection("cancelled", discovery_status=status)
        if action == "manual":
            while True:
                prompt = "富途账户 ID（数字，离线填写，待验证；输入隐藏）: "
                if input_fn is input:
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", GetPassWarning)
                        try:
                            raw = getpass(prompt).strip()
                        except GetPassWarning:
                            raise ValueError("无法隐藏账户 ID 输入，请使用交互终端。") from None
                else:
                    raw = input_fn(prompt).strip()
                if raw.isdigit():
                    print("账户已手工填写，待验证；保存配置不代表连接验证通过。")
                    return AccountSelection("selected", raw, discovery_status=status)
                print("账户 ID 为必填数字，请重新填写。")
        if action not in {"yes", "y"}:
            print("请选择 yes、manual、change 或 exit。")
            continue

        result = discover_account_candidates(host=host, port=port, market=market, trd_env=trd_env)
        status = result["status"]
        if status != "ok":
            if status == "unavailable":
                message = {
                    "AUTH_EXPIRED": "OpenD 尚未登录或登录已失效，请在官方程序中登录。",
                    "UNREACHABLE": "无法连接指定 OpenD，请核对地址、端口和程序是否启动。",
                    "NEED_2FA": "OpenD 需要手机验证，请在官方程序中完成。",
                    "NEED_PIC_VERIFY": "OpenD 需要图形验证，请在官方程序中完成。",
                    "RATE_LIMIT": "OpenD 请求受限，请稍后主动重试。",
                    "TRANSIENT": "OpenD 暂未就绪或连接超时，请稍后主动重试。",
                }.get(result.get("error_code"), "账户元数据查询失败，尚不能判断账户情况。")
            else:
                message = {
                    "empty": "OpenD 返回的账户列表为空；尚未选择账户。",
                    "no_match": "账户列表中没有匹配所选市场和真实/模拟环境的账户。",
                    "permission_unavailable": "账户市场权限证据缺失，无法确认匹配账户。",
                    "identity_unavailable": "账户 ID 证据不可无损识别，无法选择。",
                }[status]
            print(message)
            print("可重新同意查询以重试、change 修改连接，或 manual 离线填写待验证。")
            continue

        candidates = result["candidates"]
        if result["truncated"]:
            print("候选较多，仅显示有界列表；未列出的账户可手工填写待验证。")
        if result["permission_unknown_count"]:
            print("另有账户缺少市场权限证据，未作为可选账户。")
        if result["identity_unknown_count"]:
            print("另有账户 ID 证据不可无损识别，未作为可选账户。")
        for index, candidate in enumerate(candidates, 1):
            print(
                f"{index}. {candidate['masked_account_id']} · {result['market']} · {result['trd_env']}"
                f" · 账户类别 {candidate['acc_type'] or '未知'}"
                f" · 模拟类别 {candidate['sim_acc_type'] or '未知'}"
            )
        if len({candidate["masked_account_id"] for candidate in candidates}) < len(candidates):
            print("存在同尾号账户，请结合本次序号和类别在富途核对；无法区分时不要确认。")
        if len(candidates) == 1:
            selected = 0
        else:
            while True:
                choice = input_fn("选择账户序号（back 返回重试/修改连接/离线填写）: ").strip()
                if choice == "back":
                    selected = None
                    break
                if choice.isascii() and choice.isdecimal() and len(choice) <= len(str(len(candidates))):
                    selected = int(choice) - 1
                    if 0 <= selected < len(candidates):
                        break
                print("请输入列表中的有效序号或 back。")
        if selected is None:
            continue
        confirmed = input_fn(f"确认使用本次序号 {selected + 1} 的账户？输入 yes 确认 [否]: ").strip().lower()
        if confirmed in {"yes", "y"}:
            print("已确认账户元数据匹配；仍须最终 doctor 验证，未验证资金或持仓。")
            return AccountSelection("selected", candidates[selected]["account_id"], "metadata_matched", status)

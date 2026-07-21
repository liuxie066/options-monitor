from __future__ import annotations

from tests.notification_format_assertions import assert_mobile_flat_markdown


def test_render_system_notice_flattens_fields_and_omits_empty_sections() -> None:
    from src.application.notification_shells import render_system_notice

    message = render_system_notice(
        component="OpenD\nwatchdog",
        status="❌ 不可用",
        fields=(
            ("原因", "rate limited\n  - retry exhausted"),
            ("次数", 0),
            ("空值", None),
        ),
        sections=(
            ("诊断", ["lx｜SEND_FAILED\nprovider timeout", ""]),
            ("空节", []),
        ),
    )

    assert message == (
        "# OM · 系统通知 · OpenD · watchdog\n\n"
        "状态｜❌ 不可用\n"
        "原因｜rate limited · - retry exhausted\n"
        "次数｜0\n"
        "空值｜-\n\n"
        "## 诊断\n"
        "lx｜SEND_FAILED · provider timeout"
    )
    assert "空节" not in message
    assert_mobile_flat_markdown(message)


def test_render_receipt_flattens_fields_and_omits_empty_sections() -> None:
    from src.application.notification_shells import render_receipt

    message = render_receipt(
        account="lx\nops",
        receipt_type="成交",
        status="⚠️ 待确认",
        fields=(("说明", "first line\n  - second line"), ("次数", 0)),
        sections=(
            ("可选批次", ["A｜FUTU\nlot_id=one", ""]),
            ("空节", []),
        ),
    )

    assert message == (
        "# OM · 回执 · lx · ops\n\n"
        "类型｜成交\n"
        "状态｜⚠️ 待确认\n"
        "说明｜first line · - second line\n"
        "次数｜0\n\n"
        "## 可选批次\n"
        "A｜FUTU · lot_id=one"
    )
    assert "空节" not in message
    assert_mobile_flat_markdown(message)

from __future__ import annotations

from collections.abc import Sequence


def _flat_text(value: object) -> str:
    text = "" if value is None else str(value)
    return " · ".join(part.strip() for part in text.splitlines() if part.strip())


def render_system_notice(
    *,
    component: str,
    status: str,
    fields: Sequence[tuple[str, object]] = (),
    sections: Sequence[tuple[str, Sequence[object]]] = (),
) -> str:
    component_text = _flat_text(component) or "System"
    lines = [f"# OM · 系统通知 · {component_text}", "", f"状态｜{_flat_text(status) or '-'}"]

    for label, value in fields:
        label_text = _flat_text(label)
        if label_text:
            lines.append(f"{label_text}｜{_flat_text(value) or '-'}")

    for title, rows in sections:
        title_text = _flat_text(title)
        flat_rows = [_flat_text(row) for row in rows]
        flat_rows = [row for row in flat_rows if row]
        if title_text and flat_rows:
            lines.extend(["", f"## {title_text}", *flat_rows])

    return "\n".join(lines).strip()


def render_receipt(
    *,
    account: str,
    receipt_type: str,
    status: str,
    fields: Sequence[tuple[str, object]] = (),
    sections: Sequence[tuple[str, Sequence[object]]] = (),
) -> str:
    account_text = _flat_text(account) or "-"
    lines = [
        f"# OM · 回执 · {account_text}",
        "",
        f"类型｜{_flat_text(receipt_type) or '-'}",
        f"状态｜{_flat_text(status) or '-'}",
    ]

    for label, value in fields:
        label_text = _flat_text(label)
        if label_text:
            lines.append(f"{label_text}｜{_flat_text(value) or '-'}")

    for title, rows in sections:
        title_text = _flat_text(title)
        flat_rows = [_flat_text(row) for row in rows]
        flat_rows = [row for row in flat_rows if row]
        if title_text and flat_rows:
            lines.extend(["", f"## {title_text}", *flat_rows])

    return "\n".join(lines).strip()


__all__ = ["render_receipt", "render_system_notice"]

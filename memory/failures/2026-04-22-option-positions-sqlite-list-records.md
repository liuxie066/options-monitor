# 2026-04-22 SQLite list_records row shape mismatch

- 初版 `SQLiteOptionPositionsRepository.list_records()` 只查询了 `record_id, fields_json` 两列。
- 但后续复用了 `_entry_from_row()`，该函数要求完整的备份状态和时间列，导致 bootstrap 场景下 `repo.list_records()` 触发 `IndexError: No item with that key`。
- 修复方式：`list_records()` 直接查询 `_entry_from_row()` 所需的完整列集合，避免“轻量查询 + 重解析器”契约不一致。
- 教训：SQLite 行解析如果复用同一个 row decoder，查询列必须和 decoder 契约保持一致，否则很容易在测试覆盖较弱的路径上炸掉。

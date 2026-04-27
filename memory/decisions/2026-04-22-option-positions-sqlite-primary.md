# 2026-04-22 option_positions SQLite primary (historical)

> Historical decision note: this document captured an earlier SQLite-primary plus Feishu-backup direction.
> It is retained for migration history only. After the 2026-04-27 source-plan cleanup, Feishu is no longer documented as an account-level backup source model.

- `option_positions` 读路径统一切到 SQLite，Feishu 不再作为 steady-state 读源。
- 历史方案曾采用 `SQLite primary + Feishu best-effort backup`，主流程不能因 Feishu 失败而失败。
- 首次启动如果 SQLite 为空且 Feishu 已配置，自动从 Feishu 全量 bootstrap 到 SQLite。
- bootstrap 导入的历史记录保留原 Feishu `record_id` 作为本地 `record_id`，便于历史迁移对齐。
- 历史方案中新建本地记录使用 `op_<uuid>`` 作为主键；Feishu 成功同步后仅写 `backup_record_id`，不回写本地主键。
- `fetch_option_positions_context.py` 改为从 repository 读，保持输出 JSON 结构不变，避免影响 pipeline context / sell put cash / sell call covered shares 逻辑。

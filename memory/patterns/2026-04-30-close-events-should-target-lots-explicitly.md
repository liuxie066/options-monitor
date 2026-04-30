# Context

bootstrap snapshot 导入的 option lots 会保留历史 `record_id`，而后续 `manual_close` / `auto_close` 事件如果只靠 `broker/account/symbol/strike/expiration` 启发式匹配，重投影时容易出现“事件已写入，但 lot 没被消费”的错账。

# Pattern

- close 类事件应优先携带显式 lot 目标身份
- 一等目标键使用 `close_target_source_event_id`
- 兼容历史事件时，允许回退到 `raw_payload.record_id`
- 投影时若显式目标存在但找不到合法 lot，不应回退到启发式匹配其他 lot

# Why It Works

- `source_event_id` 是 lot 从 canonical event flow 投影出来时最稳定的内部锚点
- `record_id` 适合兼容 bootstrap seed lots 和历史 `manual-close-*` 事件
- 显式目标优先能避免多 lot 同键场景下误平到错误 lot

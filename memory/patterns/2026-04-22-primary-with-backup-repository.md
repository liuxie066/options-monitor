# 2026-04-22 primary with backup repository pattern (historical)

> Historical note: this pattern documented an earlier primary+backup repository design.
> It is no longer the recommended model after the 2026-04-27 source-plan cleanup that removed account-level primary/backup source semantics.

- 当要降低对外部 SaaS 存储的依赖时，优先保持领域层和字段语义不变，只替换 repository 边界。
- 推荐拆成三层：
  - 远端仓储：保留原 API 行为。
  - 本地主仓储：最小 schema，业务字段整体 JSON 存储。
  - 组合仓储：对外暴露统一接口，读只走主库，写先落主库，再做 best-effort 备份。
- 迁移切换优先用“空主库自动 bootstrap + 后续单向同步”，避免在第一轮引入双向 merge。
- 对补偿同步，优先给 CLI 增一个显式子命令，而不是把自动重试逻辑塞进所有业务脚本。

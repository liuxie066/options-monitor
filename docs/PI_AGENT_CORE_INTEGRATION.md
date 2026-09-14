# Legacy Pi storage and rollback

当前 Bot 源码已切换为 [Python runtime / Scene v7](BOT_DESIGN.md)。Pi Agent Core、Node 问答进程及其 npm 依赖已从当前运行入口移除。不要为新版 Bot 安装 Node 或执行旧 Pi smoke 命令。

旧 `pi_sessions.sqlite3`、迁移回执、备份和相关旧发布目录仍是历史数据，不由新版 Bot 删除、重写或自动迁移。新版会话正文保存在原有 Host 数据库的新上下文表；既有个人记忆存储继续使用。

`pi_migration.py`、`pi_agent_process.py` 中的离线桥、`agent-runtime/pi_migration.mjs` 与 `pi_session_core.mjs` 仅保留用于历史 Pi 存储转换。它们不在正常问答路径中。执行历史迁移仍需明确目标、源/目标旧运行时、preview、确认和回执核验；所需 Node 与 Pi 包由所选旧运行时提供。

升级到 Python Bot 不打开 Pi 数据库；明确回滚到旧 Pi 发布时仍执行原有存储就绪检查。cleanup 继续保护迁移回执依赖的旧发布和备份。

离线转换验收位于 `tests/manual/pi_migration.py`，需预先准备 `OM_PI_LEGACY_RUNTIME`（0.84.2）和 `OM_PI_TARGET_RUNTIME`（0.85.1），再显式运行 pytest 指向该文件。测试不自动下载安装历史 SDK。正常 Python Bot CI 验证新版不访问旧数据库及既有回滚/清理保护。

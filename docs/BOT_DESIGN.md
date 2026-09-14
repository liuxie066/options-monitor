# Bot / Python runtime / Scene v7

本轮目标是解释 OM 策略、分析已记录的报错、说明标的被过滤的原因，并支持连续对话、个人记忆和只读工具调用。本文描述开发中的源码合同；是否已发布、是否部署以及真实模型的效果需要分别验证。

## 调用链

渠道鉴权与去重 → `channel_facade.py` → `local_harness.py` → `host.py` → `runtime.py`。

`runtime.py` 直接使用现有 Python Chat Completions / Responses 适配器。没有历史上下文的首轮使用原生 `tool_choice=required` 要求一次工具调用；已有上下文的追问由模型判断是否需要重读。Host 校验参数和固定范围，调用原有 OM 工具，返回脱敏结果；模型随后直接给出正文。没有 Pi 子进程、IPC、答案提交工具、claims、证据编号准入或目录激活步骤。

工具由现有 registry 与各领域 `TOOLS` 定义；Scene 直接列出 7 个工具：`project_context`、`project_files`、`candidate_filter_explain`、`runtime_runs`、`runtime_logs`、`runtime_status`、`receipt_read`。渠道场景按身份追加 `bot_memory`。不再按 toolset 展开工具，也不构造目录 catalog、snapshot 或 hash。工具仍拥有账户、配置、历史快照、分页和真实状态判断；Bot 不重新计算历史筛选结果。

## 上下文与记忆

同一渠道、用户、会话和配置范围的最近 10 轮用户/助手正文存入 Host SQLite，和回答及回复 outbox 在同一事务中保存。工具结果用于当前分析；历史回答只提供连续性，当前业务事实必须重读。上下文过长时压缩较旧完整消息组，保留当前问题和最近工具配对；删去的正文只留下标为不完整的短摘录。首次模型请求后优先使用 provider 返回的输入 token 数校准本地估算，再判断是否压缩；provider 不返回 usage 时才使用保守估算提前收束。

个人记忆复用既有 `BotMemoryStore`，按鉴权用户与配置范围隔离。支持明确请求后的保存、查看、搜索、纠正、删除，保留原始用户引用、修订校验、幂等与读取确认。当前不运行后台自动记忆整理。旧 Pi 会话数据库保留，但不作为新版上下文读取来源。

## 完成与失败

默认最多 16 次模型调用、12 次工具调用，总计 180 秒，预留 45 秒回答。工具额度耗尽或连续读取失败后关闭工具，要求模型说明已知结果和缺口。正文达到输出上限最多续写一次；取得工具结果后遇到模型异常，预算允许时尝试一次最终回答。

未知工具、非法参数和跨范围参数不能执行。工具失败保持失败，过大的结果要求缩小范围，不当作有效空结果。项目源码一次最多读取 300 行；读取 cursor 自带下一位置，后续请求无需重复 start_line。模型观察不包含配置或内容摘要 hash，避免把完整性元数据误当业务原因；Host 和工具 owner 仍保留这些校验。取消或超时后的模型/读取结果不能保存答案；已运行的读取线程可能持续到自身超时，但没有提交回答的权限。

运行诊断把 `ran_scan` / `ran_pipeline` 写入标志投影为 `usable_scan_result` / `pipeline_completed_successfully`，避免把失败结果误读成流程从未调用。`account_metrics` 与 `run_audit` 是独立记录；除非单条记录明确给出关系，运行 ID、顺序和时间接近均不构成因果链。

## 保留边界

交易、账本、配置、通知与服务操作仍由原有业务模块和 deterministic Control 管理。模型没有修改这些状态的工具。既有渠道权限、去重、取消、Host 租约、outbox 和真实数据保持独立。

普通安装与新版升级仅需要 Python。回滚到旧发布仍要核验旧 Pi 存储和旧运行时；历史迁移说明见 [Pi legacy storage](PI_AGENT_CORE_INTEGRATION.md)。

## 验证

`tests/test_bot_python_runtime.py` 覆盖模型工具循环、两个 HTTP 协议、当前轮压缩、上下文事务、个人记忆隔离、取消、真实源码读取、真实过滤快照和报错写入记录。渠道 HTTP、取消、业务工具与安装升级测试分别验证相邻边界。脚本化模型结果不等同于真实模型语义验收；真实模型必须回答同一组问题后再评估效果。

同一 `deepseek-flash` 下进行两轮成对复验：完整证据直答与 Bot 工具路径都覆盖策略主要边界；报错回答都保留两条独立记录的证据边界，没有再把它们拼成因果链。结果保存在 `/private/tmp/om-python-paired-yz2ru4ib`。这是本轮目标题的回归证据，不代表生产状态或长期可靠性统计。

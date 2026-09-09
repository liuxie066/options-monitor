# Bot v2 Architecture / Scene v5 Contract

The product architecture remains v2. The general `om_chat` runtime Scene and
prompt contract are versioned independently and are currently `v5`.

The generic Agent runtime has been replaced by Pi Agent Core. This document
continues to own the Bot product boundary and Scene v5 contract; the
current model/tool loop, session, admission and rollback implementation is
specified in [PI_AGENT_CORE_INTEGRATION.md](PI_AGENT_CORE_INTEGRATION.md).


## Bot 第一版实现设计（实现验证中）

本节是 [BOT_PRD.md](BOT_PRD.md) 的唯一实现设计，产品合同和批准依据以 PRD 第 1、9、10 节为准。
其余章节描述已发布基线；本节只在明确列出的第一版变更上替代旧合同，不宣称能力已上线。
本文件已整体更名为 `BOT_DESIGN.md`，活跃引用同步更新；冻结快照及验证结果由 handoff 引用。
流程、批准与评审进度只在 [handoff](../codex/bot-devflow-handoff.md) 维护。

### B1. 目标、非目标与当前事实

第一版四个可验收结果：项目自有 Copilot 全面改名 Bot/bot；用户问后可查成交、监控、定时任务回执内容；180 秒总预算内有依据地完成回答；长期记忆跨会话有效且可纠正/删除。
原有普通问答与 Control 能力不得退化。不引入主动诊断推送、自动修复、源码修改工具、服务器管理平台、独立移动端或无限续跑。

2026-09-09 已 fetch origin；设计源码基线为 `16ee6e830a3a9b89232feb70c02d4bdee6d4bdd9`。
设计时原工作树 HEAD 是 `0c5ece8c5f124261a0eec47fb2edeb7d28e697ea`，落后 62 提交并有无关文档修改。
实施使用独立 options-monitor-bot worktree，HEAD 为冻结基线。设计时上游差异以 `git show origin/main:<path>` 核查，设计阶段未切分支；实施不使用原脏 main。
- Pi 三个依赖及锁文件均为 0.84.2。压缩后 75% 准入修复已在上游 6e0591de，50% 仍为压缩目标。
- 成交 inbox 已持久化不同结果与回执消息；部分成交/生命周期回执交给 lifecycle outbox，不能只读 inbox。
- Daily Brief 已有结构化报告读取和发送消息存储；notification perception 只记录决策/发送摘要，不能替代回执原文。
- channel_facade 采用含 sender 的 opaque Pi session；旧 Host session_memory 并非当前问答主路径。
- Scene 已为 180 秒、16 轮、12 次调用、45 秒收尾 reserve；“容量治理”不能再次仅提高这些值。
- 历史失败样本及其时点见 PRD 第 8 节；这些不是当前版本失败率或已确认仍存在的缺陷。

### B2. Pi SDK 兼容性决策

第一版保持三包 0.84.2；升级评估已完成，不把“沿用 Pi”误写成无条件升级。
npm 官方三个包的 latest 均为 0.85.1，Node 要求均仍是 >=22.19.0。
已下载两个版本的公开声明与包元数据做静态比较，发现 OM 正在调用的接口有破坏性变化：

| OM 当前使用 | 0.85.1 声明变化 | 对 OM 的影响 |
| --- | --- | --- |
| SqliteSessionRepository({env, sqlite, databasePath, writerLease}) | SqliteSessionRepo({directory, databaseFactory, databasePath?}) | 构造器、生命周期及显式 writer lease 接口需适配 |
| session.findEntriesOnBranch / moveLane / appendMessage / appendEntry | Branch、Context、SessionMutation 等新接口 | 已提交分支恢复、写入与 commit marker 不能照搬 |
| compact(..., signal, thinkingLevel, retry, callbacks) | Context 参数与参数顺序改变 | 取消、压缩和计费回调需重新验证 |
| SQLite loadMigrations/applyMigrations | applyInitialSchema | 声明无法证明旧库可原地升级，必须用副本验证迁移与回滚 |

证据：npm tarball 中 `dist/harness/session/session.d.ts`、`dist/harness/compaction/compaction.d.ts`、`dist/sqlite/repo.d.ts`、`dist/sqlite/migrations.d.ts`；
当前调用为 agent-runtime/main.ts:1304、1332、1520、1769 附近。
本次未安装替换依赖、未执行新版 runtime、未打开生产 SQLite。静态不兼容已足以排除直接改版本号，但不能据此推断数据一定不兼容。
官方 [0.85.1 发布说明](https://github.com/earendil-works/pi/releases/tag/v0.85.1) 与
[Agent 包](https://www.npmjs.com/package/@earendil-works/pi-agent-core) 仅证明发布内容，不证明 OM 回归通过。

升级后续 owner 为 Bot runtime 维护者；触发条件是新版能力/修复有本项目明确收益，或单独确认 SDK 迁移。
届时三包一起精确锁定、更新真实 runtime_version，并验证五类 provider、SQLite 旧数据副本、writer fencing、取消/commit 竞争及回滚。
不通过引入新 AgentHarness、远端 Pi server 或临时旧 SDK runtime 双轨来完成本版。

### B3. 主链路与 ownership

```text
可信 Channel / CLI 身份与范围
 -> Bot Service 准备请求
 -> Host：统一 deadline、会话 lease、有限记忆/进度、当前 Control 快照
 -> Pi Agent：选择工具、读取回执索引/详情与当前证据、提交答案
 -> Host：校验 scope / coverage / freshness / answer，提交结果
 -> 既有 reply outbox -> 渠道呈现
 -> 有界后台记忆整理（仅由用户对话触发）
```

| 职责 | 现有 owner / 计划变化 |
| --- | --- |
| Bot 入口与身份 | assistant/inbound_service.py、channels、inbound/feishu_ws.py、cli/bot_ops.py 的入口及调用；不按业务意图分流 |
| 请求治理 | application/bot/；Host 保留准入、lease、取消与最终胜者 |
| 通用模型循环 | agent-runtime/main.ts；infrastructure/pi_agent_process.py 保留唯一 JSONL bridge |
| 业务内容 | trades、daily_decision_brief_repository、positions/maintenance_receipt、multi_tick 审计及 runtime run/log owners |
| 回执查询 | canonical agent_tools 增加一个纯读 receipt_read；复用源 owner 的查询函数，不读取任意路径/SQL，不持久化第二份业务事实 |
| 长期记忆 | BotHostStore 在实际 Host DB 内维护 bot_memory 与 bot_memory_jobs；不挪到 Pi 或业务账本 |
| 业务操作 | deterministic Control 原 owner；记忆内容不授予 preview/apply 权限 |

receipt_read 是三类“用户收到的回执”的固定查询合同，不承担业务计算或新的路由服务。
已有 daily_decision_brief_read、runtime_runs、runtime_logs、notification_perception_read 和 positions 读工具继续承担自身领域的当前状态和细节查询；必要的 Scene toolset 投影从 registry 派生。
Host 只给一般工具描述和可信范围，不为案例硬编码查证脚本。

### B4. 完整更名与一次性迁移

更名范围：
- src/application/copilot -> bot，Copilot* 类型、copilot_* 标识、CLI copilot -> bot、Host 表名与项目自有 schema 标识；
- assistant.copilot -> assistant.bot，配置校验/生成/示例、环境变量中的旧词、API payload/decision/tool metadata 中项目自有旧名；
- 测试文件/fixtures/scripts、错误文案、活跃文档/链接和发布打包清单的相关引用。
- 保留项目名称 options-monitor、OM_RUNTIME_ROOT、om_chat、om-pi-ipc.v1 和 om-pi-session-v1 等不含旧词的现有身份；“无 OM 前缀”针对产品名称 Bot，不随意改变项目协议身份。

应用启动不接受旧配置别名、不隐式回退、不创建空旧库；发现 assistant.copilot 等旧键报明确迁移提示。
旧 CLI/API 不转发到新入口。只有离线迁移器和历史审计识别可以包含 Copilot 字样。
无 blanket 字符串替换：第三方 GitHub Copilot 名称、不可变审计原文不改写。
以 `rg -ni copilot` 的实际残留清单逐项核验，并用入口行为测试证明旧名不可用。
评估链同时更名 scripts/copilot_p1_eval.py、对应 tests 和 release preflight 引用；项目自有报告 schema `om.copilot.p1_eval.v4` 改为 `om.bot.p1_eval.v4`，新消费者只接受新值，不加 fallback。历史评估报告保持原文且不充当新版本验收证据；只在确有迁移读取需要时由离线迁移路径转换。

迁移入口为 `./om bot migrate --host-db <path> --dry-run` / 显式 `--apply --writers-stopped`，路径来自操作者参数与既有配置解析，不接受模型提供路径。
迁移由运维在维护窗口单独执行；部署/生产 apply 不属于开发授权。
窗口必须覆盖所有共享 Host audit_db/config 的写者：渠道 inbound/Control audit、Bot run、回复 outbox sender、记忆 worker、CLI/其他宿主；停止接收新请求与 claim，排空有效 run/lease，确认旧二进制退出。已有 delivering/未决外部发送不能重置为未发送；保留 delivery_key、claim 与尝试事实，先依据原 owner 的发送确认/恢复规则解决在途状态，未解决则 apply 拒绝。

启动检查由 BotHostStore 的 schema owner 负责，在任何 _ensure_schema、自动建库和读写方法之前只读检测目标 DB 与配置。迁移清单由同 owner 的离线迁移器保存为 Host DB 旁的 bot-migration.json，原子替换、包含迁移版本、规范化 DB/config 路径、备份身份/校验值、逐项状态与验证摘要。SQLite 内 Bot 自有迁移完成标记与更名同事务写入；不用共享 DB 的全局 PRAGMA user_version 代替。manifest 是切换记录，不是业务数据真源。
fresh install 仅在已解析清单的所有位置均无旧配置/旧表、无不完整 manifest 且无新旧冲突时成立；共享 DB 已有其他 owner 表不等于旧 Bot 数据。发现 legacy 数据但无完整记录、已迁移 DB 与清单身份不符或部分完成时返回 not_ready，不能先创建 bot 空表。已有文件的只读检测使用 mode=ro，缺失路径由显式 fresh-install 初始化处理；干运行不创建文件。
Host 表迁移清单固定为 copilot_sessions、copilot_session_runs、copilot_runs、copilot_reply_outbox、copilot_lane_leases 及其索引/约束，逐项更名 bot_*，保留主键和引用；旧 session 的 messages/turns/memory_json 保留为历史数据，不自动提升为跨用户长期记忆。Pi DB 只校验身份/可读性，不更改 SDK schema。配置清单包括 assistant.copilot 及项目自有旧环境变量、生成器/示例/校验；来源以可信 runtime 配置解析确定，不把模型提供的路径作为清单。

1. 枚举真正使用的 Host DB、Pi DB、配置、outbox/运行记录及 schema 版本；路径去重，缺失不建空库。
2. dry-run 输出脱敏清单、冲突、schema 和数量；业务记录只验证可读，不重新发送。
3. SQLite 使用一致性 backup（含 WAL 已提交内容）后，对 Host 表/索引及可变协议字段事务迁移；配置通过验证后原子替换。
4. 用迁移记录识别已完成项；跨数据库/配置没有伪原子性，全部清单项完成前新 Bot 保持 not_ready。中断后从记录继续，不能绕过已完成验证。
5. 数据冲突或新旧对象并存且不等价时停止，不覆盖新数据；重复执行结果确定。
6. Pi 会话 ID 和库格式保持不变。历史 Host 会话/运行/回执保留；缺少可信 sender 的旧会话保留供授权查看，不自动挂到任意新用户。
7. 历史事件文本和完成的审计 envelope 保持原样；新记录只写新协议。待发送 reply 的可变结构在 migration 中转换，delivery_key/claim/发送幂等身份保留。
8. 回滚使用备份与匹配旧发行版，禁止旧二进制直接读已迁移新 schema。切换后的新数据不能通过覆盖备份丢弃，需维护模式处理差异。

### B5. 回执读取合同

receipt_read 输入为 type=trade|monitor|scheduled（可省略）、account、market、起止时间、deal_id/run_id/event_id、symbol、cursor、limit。
时间规范使用 UTC 存储、显式时区展示；用户“下午”由 Agent 结合固定 operating_date 解释，Host 不新增自然语言解析器。
所有参数为封闭 schema；默认 page=10、上限 50，详情必须仍属于同一认证范围。
Host 注入 channel/sender/authority_scope/可访问账户；model 参数只能收窄，不能把不同账户、scope 或任意路径注入工具。
可信 scope 的具体来源是 assistant/policy 的渠道 sender allowlist，以及 channel_facade 解析的 config_key/规范化 config_path，再通过现有 accounts_from_config(cfg, fallback=()) 得到该配置允许账户集合。此集合不是新的 sender→account ACL；当前配置授权范围内沿用现有行为。配置缺失/解析失败/账户不在集合内则拒绝，不回退全账户。每次详情/后续页重新核对身份与集合。
om-agent 的 receipt_read 遵循本地 Tool Gateway 现有可信配置作用域，由 operator 参数经现有配置 resolver 解析；不伪造渠道 sender，不开放个人记忆。仅经过渠道 policy 的真实 sender 可访问其个人记忆；本地无认证身份不因传入任意 sender_id 获得该权限。

输出按现有 evidence contract 投影：
- event_ref=(source, source_event_id)，account/market、业务对象、发生/记录时间、来源 revision；
- business_result、diagnostic_code/脱敏诊断、receipt_body、related_run、source_ref；
- delivery_state 独立给出 prepared/attempted/confirmed/failed/unknown，缺失不是未送达；
- coverage 复用现有 canonical 字段 status、complete_for、included_count、total_count/omitted_count、has_more、scope；附 freshness/as_of，missing_sources 作为独立诊断字段；
- 完整详情只对当前 event/revision 的已读字段 complete_for；列表页仅对本页覆盖范围有效，有 next_cursor 不能声称全部事件已读。所有应查源成功且结果为零才是 valid empty；任一源失败为 partial/unavailable，不可伪装完整零结果。未知总数保持未知，不捏造 total_count；具体 status 值与 result_admission 的 canonical 枚举一致；
- 查询状态区分 empty、partial、unavailable、outside_retention（仅源能证明时），不凭无结果断言已过期。


来源覆盖与读取方式（基于 origin/main）：

| Source | 回执内容真源 | 读取及补齐 |
| --- | --- | --- |
| scheduled / monitor 的 Daily Brief fixed 与 candidate_alert | daily_decision_brief_repository 的账户 delivery state 与 run delivery plan，冻结 rendered_message/hash/delivery_key | 复用 read_daily_decision_brief_delivery_state，补按时间/key 的只读查询；将已有 daily_brief toolset 纳入 Scene |
| trade intake | trades/inbox 各语义结果 receipt_id/business_result/payload，发送时冻结 message/route | 复用 read_trade_payload(read_only=True)，新增只读列表；不调用会 ensure_schema 的 recovery list |
| trade lifecycle | trades/lifecycle_outbox 与 ledger lifecycle notification outbox/batch | 经 ledger/api.py 读取 frozen payload；历史重渲染标 reconstructed，未来在原发送冻结边界存正文/hash |
| scheduled run 与其他 monitor 状态回执 | 既有运行/审计及 positions/maintenance_receipt 等各任务 owner | 通过 run_id 关联真实保留的结果，未保存正文时只给源已存业务内容并标识 reconstructed/unavailable，不伪造一条“已发送回执” |

receipt_read 每行带 body_provenance=frozen|reconstructed|unavailable。查询“收到的原文”时，重建内容不能当 frozen。
Daily Brief 最新成功报告与当时 fixed failure 回执分开；已有 daily_decision_brief_read 只是当前/指定 revision 的报告读取。
源详情必须能证明其业务账户属于本次授权集合；现有 route 不等于权限，也不能用今天路由推断历史对话。
账户级业务回执在已有账户授权内读取，用户/会话私有内容额外要求原 scope；身份缺失或映射冲突时返回不可关联，不按 deal_id 全局猜测。
run plan 仅作关联证据，不能成为账户既有冻结消息唯一的查询来源。沿用 source retention，不回填已删历史。

首轮按范围和轻量元数据找候选；唯一匹配读该回执详情，多条给用户候选。大正文分页/分块，不能一次把所有日志灌进 prompt。
receipt_read 同一 opaque value.next_cursor 支持事件页和正文块两种内部 kind，不新增工具。正文 cursor 绑定 owner/account、过滤条件、event_ref/revision/body_hash、下一段 offset 与到期/源 watermark；验证失败返回 cursor_invalidated。每块在 canonical tool projection 前有界，预留元数据后满足现有 4000 token 结果上限。输出 body_range、body_complete 和 next_cursor；末块标记该范围读完，不能把最后一块等同已经读取整篇。覆盖在本请求实际取得的范围内累计；对未读尾部不作事实结论。原有 keyset redaction 保留的 value.next_cursor 就是继续入口，避免另起字段被裁掉。
每个 source 的 reader 在 canonical owner 内实现；聚合层只排序、去重、分页和投影，不解释 OperationalError、不计算入账状态。
交易编号 7258806397173991645 为回归 fixture；真实原因须源诊断与当前 ledger/intake 证据支持，不能把异常类名等同数据库锁。

标识与分页：同一 source/event/revision 重投只出现一次；更正与新处理结果保留各自 identity，并用显式 related/supersedes 关联。
按发生时间及稳定 event_ref 排序，冻结各 source watermark；opaque cursor 绑定 scope、过滤条件与 source revision。
后续页发现源改写、保留期清理或权限变化，返回 cursor_invalidated / 明确部分覆盖并重新查询，不混用旧页。
查询使用只读连接和流式有界扫描；不得因 reader 构造自动 ensure_schema 或更新 delivery/claim。
全文/路径/路由只经过 schema 白名单的脱敏内容进入模型，禁止 webhook/token/其他用户标识泄漏。
回执、日志、记忆均为不可信数据，不能改系统指令、选择 DB 路径或授予写权限。
回执到达只走原业务持久化和发送路径，不调用 Bot，不入诊断推送队列。

### B6. 容量、答案完成与恢复

统一计时在可信入口接受请求时启动 monotonic deadline=180s；请求准备、scope 解析、lease/DB、Node 启动、压缩、工具、最终准入均消耗同一预算。
跨 Python/Node 传递剩余时长，不能传不可比的本地 monotonic 时间；每层只缩短，provider timeout=min(配置,剩余预算)。
参数路径固定：feishu_ws 接收处的 received_monotonic → 队列任务 → AssistantRequest 的内部 received_monotonic（不在 public_payload，也不接受聊天输入覆盖）→ inbound_service._run_bot → channel_facade.run_channel_request → prepare_contract/run_prepared_contract/Host → run_pi_agent。其他渠道和 CLI 在其可信接收入口设同字段；进程内携带 deadline_monotonic，出队、准备前后和获取锁前后检查。同进程直接调用无接收值时只在最外层设一次。SQLite busy_timeout 及等待必须受剩余预算约束。
JSONL run.start 新增 remaining_budget_ms（有限正整数，<=180000）；由 Python 在发送前向下取整，<1ms 直接预算终止。Scene limits.timeout_seconds=180 保留为总上限，不再要求动态 bridge timeout 与它相等。run_pi_agent 接收可信 deadline_monotonic，子进程启动/管道写入前后仍检查同一截止；Node 用收到的毫秒数创建本地 performance.now() 截止，Python 仍是全程终止兜底，IPC/启动耗时不能使 Host 获得额外时间。模型配置 timeout 保留原校验，调用时用剩余毫秒截断；支持不足 1 秒，不向上取整成新的秒预算。双端 strict schema 同步更新并测试缺失/非法/超上限字段，不依赖可跳变的 wall clock。
渠道接收后排队也消耗这 180 秒；过期请求直接结束，不等出队后重置预算。以最终回答持久化并进入 reply outbox 为生成完成点，外部渠道投递另计且必须报告真实送达状态；渠道 ACK 不视为回答成功。
由渠道适配层在执行前提供可信 reply route/delivery_key/request_id；Host 不做渠道地址推断。BotHostStore.finish_run 对有渠道回复的成功路径在同一 SQLite 事务内 CAS 最终 winner、存 response、最小 progress 和现有 bot_reply_outbox 行（含 run_id/request_id/原 delivery_key/结构化回答及 route）。渠道呈现由现有适配器完成，后续 enqueue 只按同 key 幂等读回，不能重发或产生第二个 key；同步 CLI 以相同结果事务持久化为完成点，不创建虚假网络 outbox。
Pi commit 只是对话分支提交，不等于 Host answered 或送达；Host 事务失败即不算 answered，不能因 Pi 已提交而伪造完成。outbox sender 仅消费已提交行，重启按既有 claim/retry 恢复。成功事务与渠道回调之间退出不丢回答；外部发送结果不确定继续按现有 delivery 语义处理，不声称恰好送达一次。
现有 45 秒收尾 reserve 纳入总预算。业务工具达到上限时仍保留 submit_answer 的一次正常收尾机会，协议修复不重置全局时钟或无限刷新计数。
实现前以固定 fixture 检验 source 已有的输入绑定/答案修复，已修复历史问题只留回归，不再复制逻辑。

上下文：
- 保留 70% 触发、50% 目标、75% 硬准入语义；固定 prompt、tool schema、记忆、当次问题与 output reserve 一起核算。
- 在每次模型请求前统计实际 provider input；运行中先用现有 observation 投影/分页收窄，再对历史完整 turn groups 调用 Pi 压缩原语。
- 保留当前请求、已确认范围、Control 引用、关键 evidence id/来源时间、未完成进度；不截断 tool-call/result 配对，不把摘要当当前 evidence。
- 当前轮所需完整证据不能安全容纳时，收窄调查或留下可继续进度，不能提高硬上限/清空历史。
- 同一工具、有效参数、错误码未改变时禁止无意义重复；canonical 参数提示在源工具修复，不建 Host 意图路由。
- 目录加载和 eager 均先测真实输入/往返；不因“最佳实践”一律切换，也不新增第二套目录。
- 准入仍要求本请求有 scope/coverage/freshness 合法的 observation。旧 evidence id 只能定位，不能作为本轮已读事实。

状态：
- 复用现有 running/waiting_model/waiting_tool -> answered/control_requested/failed/cancelled/interrupted；不新建另一个业务状态机。
- 预算终止保存当前目标、事件引用、已读证据/时间、缺口和下一步；即使只有部分结论也明确“本次未完成”。
- progress 由 Host 从 durable contract/events 确定性生成，随终态写入已有 run 记录；不依赖 Pi 当前轮提交、submit_answer 或后台经验提炼。每次已准入 observation 的导航引用随原事件保留；stale-run 恢复从这些事实补齐 interrupted 进度，不再跑模型。未保存到事件的观察不能宣称已读。
- 跨会话继续通过可信 owner/account 查询已有 run 的 progress；未完成条目默认可找，cancelled 仅在用户明确继续/指定原事件时列出候选。多匹配给用户选择，不按最近时间猜一条。新 run 写 resumed_from，原 run 保持终态与原取消资格，当前证据重新取得；没有新建第二份模型生成进度真源。
- run 的执行终态与 progress 的调查状态分开：原 run.status 保持 failed/cancelled/interrupted，已有 run 记录的 progress 元数据保存 revision、resolved_by、resolved_at。继续请求绑定原 progress_ref/revision；默认只检索尚无有效 resolution 的进度，完成的历史 run 仍可显式查看。
- submit_answer 的封闭内部提交合同增加可选 progress_resolution（progress_ref、expected_revision、covered_goal、claim_indexes）。它仅用于本次明确承接且同 owner/account 的事项，claim_indexes 只能指向本次 claims 数组中已通过原有答案准入的项，并覆盖原目标；未声明、澄清、多候选、预算终止或仅回答旁支问题都不关闭原事项。Host 在 B6 的终态/outbox 同一事务中 CAS 标记原 progress.resolved_by=当前 run_id，取消的旧 run 不恢复执行资格。不以任意 linked run 的 answered 自动推断原调查已完成。
- 重复完成幂等读回；revision 冲突保留当前回答但不关闭发生变化的进度，并如实提示进度更新未完成。数据库/事务失败不产生虚假 closure；新会话按 progress 元数据而非原 run.status 单独判断未完成。
- 用户继续建立新的 linked run，重新冻结 180s 与权限；保留新提问，所有“现在”的易变事实重读。
- 已取消不自动续跑；用户显式继续可建立新请求引用进度，不恢复原 run 的写入资格。
- Control preview/apply、发送和记忆写操作不能通过 observation replay 再执行；仅允许同一操作幂等键读回已提交结果。
- late callback 不得在取消/提交竞争后改最终状态；保持 Host 单一 durable winner。
- 进度复用现有事件/渠道能力，可取消；不把全量 trace 推给用户。

新增原因分类在现有事件/指标中记录：prepare、compaction、context_capacity、time_deadline、tool_call_limit、tool_failure_limit、answer_admission；保留底层安全错误码。
统一输出 elapsed、各阶段耗时、context before/after、模型/工具/重试数量、token 和答案提交结果。
不得将 unavailable 伪装为 valid empty，或把“有错误说明”全部计入有效完成。

### B7. 长期记忆合同与存储

复用 private SQLite / BotHostStore 连接，在实际 Host DB 建两张必要表，不使用向量库、外部 Agent 服务或旧 session_memory 双写。
- bot_memory：id、owner_scope、account_scope、kind、content、原始 source_turn/run/evidence_refs、source_time、revision、state、updated_at；tombstone 保留最小原始来源抑制信息。owner 级 epoch 作为同表的内部元数据记录（不参与 list/search/模型内容），所有该 owner 记忆事务统一读取/递增。
- bot_memory_jobs：由 run_id 唯一标识的待整理已完成对话，含输入 source refs、memory epoch、lease、attempt、状态；不存重复全部回执/原始日志。
owner_scope 由可信 channel+sender+authority_scope 派生，支持同一用户新会话复用；账户具体经验必须 account_scope，当前无权限时不召回。
会话 Pi key 保持原算法且仍含 conversation。个人记忆身份只来自已通过渠道 policy 的可信 sender；未认证 CLI/local session 不读写个人记忆，不能通过自行指定身份解锁。账户经验的 account_scope 从本次已准入业务证据与可信允许集合取交集；模型声明无权扩张。一般偏好可为 owner-only（无业务账户），不视为账户通配符。progress 真源是 B6 的 Host run，bot_memory 的 progress 类型仅可引用该 run，不复制动态状态。

内容政策：
- preference：用户明确稳定偏好；experience：已通过源证据核实的结果及适用条件；progress：未完成调查的目标/关联/下一步。
- 不把模型猜测升级经验，不储存凭据或动作授权，不复制回执全文为常驻记忆。
- 每条内容上限 2000 字符；每次召回最多 8 条且总计不超过 2000 token，先按 owner/account/kind 过滤，再按引用和词匹配相关度、更新时间排序。
- 采用 SQLite 有界查询/索引与标准库匹配；如果中文召回验收失败再采用仓库可用索引，不提前引入 embedding 服务。
- 长期保存与每次注入容量分开；低相关历史留库可查询。存储不足时如实报错，不删除显式偏好来腾容量。

用户入口仍是当前聊天，支持“记住/查看/纠正/删除”和明确继续。
Agent 通过封闭 Host 私有 bot_memory 协议动作读取/管理本人的记忆，不暴露通用 business mutation 或修改源代码工具。
动作只允许 list/search/remember/correct/forget，target id 必须同 owner scope，纠正/删除多匹配先澄清。
所有写动作带当前 user turn 的 provenance、expected revision 和 idempotency key；无真实用户来源或来自工具内容的命令不得获得写权限。
普通业务写入仍需 Control；用户已授权的个人记忆维护是此处明确限定的本地写入边界，不能扩张为账本/配置写入。
显式操作持久化完成后返回结构化操作回执，答案依据回执才能说“已记住/已删除”；失败明确说明。
私有写动作不能标为 pure_read 或借 OM_AGENT_ENABLE_WRITE_TOOLS 开放业务写入。提交后执行同 owner 的封闭 readback，将 action/id/revision/result/idempotency_key 登记为本请求的记忆操作证据；submit_answer 只据此承认对应维护动作，不据此支持交易/余额等业务事实。删除读回 tombstone 不携带已删除正文。提交成功但 readback 失败只报告确认暂不可用，同幂等键可读回既有结果，不能再次写。
所有显式记忆写入和幂等回执同事务提交；获得 SQLite 写锁后、写前检查当前 run 非终态/未取消、deadline 未到、run lease/fencing、owner、epoch、expected_revision，提交前再检查本地 monotonic。取消/终态 CAS 与写入由同一 DB writer 顺序决定：取消/截止先赢则无新写；写先提交则允许真实 readback，但取消后不得再改 run 终态。进程退出后旧 worker 的 lease 无效。数据库锁等待有界，不能只丢弃 Python late callback 来宣称阻止副作用。
当前会话立即尊重纠正，但跨会话只有提交成功才生效。

自动整理：
- 原业务回答先完成准入并可交付；Host 以 run_id 入队，不等待提炼模型。宿主启动/空闲时按稳定 run 游标有界扫描已完成、具有真实用户来源且缺少 job 的 eligible runs，幂等补建 bot_memory_jobs（run_id 唯一）。因此回答事务后、job 入队前崩溃也可恢复；失败/取消的进度走 B6，不混入经验任务。扫描游标只有对应批次已成功入队或已证明无需提炼时推进；不因一次 enqueue 失败永久跳过。
- 在现有渠道宿主中使用一个有界后台执行槽；不新增 systemd 服务或通用调度平台。CLI 短进程只保证持久待办，后续宿主恢复扫描。
- 仅消费已提交用户对话与可验证 source refs，每次最多一个任务、最多一次模型提炼、总时限 30s；失败至多一次重试，重新启动可恢复 lease 过期待办。
- 执行 owner 为 Bot 应用内 memory worker，由现有渠道宿主启动单个有界槽；模型调用复用 infrastructure/pi_agent_process 与同一 Node/Pi SDK 的受限整理模式，使用已验证 assistant 模型配置，独立短生命周期且不提交到用户 Pi 对话。输入仅允许经来源校验/脱敏的候选，tools=[]，结构化候选输出，不运行业务工具/Control，不新增模型配置项或第二个 runtime。后台成本记录到 job，不算前台回答 token。
- foreground 活跃时不 claim；新前台进入时设置当前整理取消信号、撤销其提交 lease，前台不等待模型退出。整理最多 30 秒从 claim 起计算（含准备/进程/调用/校验/写入），通过同一 deadline 机制取消。前台让行回到待办且不算模型故障重试；实际模型/校验错误仍至多一次重试。旧响应必须凭 job lease/epoch/CAS 才能提交；晚回不能绕过前台让行。任务失败只记记忆状态，不回写业务 answered 为 failed，不额外发送诊断消息。
- 偏好显式写入同步执行；自动提炼经验最终以来源校验和乐观 revision/epoch 提交，不能覆盖更新的用户纠正。

纠正/删除：
- 更新 expected_revision 做 CAS；竞争失败重读，不覆盖用户新版本。correct 与 forget 都在同事务递增 owner epoch，保留被替代版本的最小原始 source suppression（不保留旧正文）；correct 的 active 来源切到本次真实用户纠正，原始来源进入条目的 superseded_source_refs。候选写入前按 owner 核对所有适用 suppression，不能仅查 target id：新 epoch/新 job/新候选 id 仍不得用旧来源另建旧内容。无法细分同一旧 source 的内容时保守禁止从该 source 自动重建，既有其他有效条目不被删除；后续真实用户新声明可成为新来源。
- forget 将条目 tombstone 并增加该 owner 的 memory epoch；删除内容不再作为可召回正文，保留最小 id/source suppression 信息。
- 旧 epoch 正在运行的整理不能提交。写入只能以原始用户声明或原业务证据作为来源，摘要/助手复述只能导航，不能作为独立 source。无法还原原始来源的候选不写入；原始来源命中 tombstone suppression 时，即使新摘要/新任务已拿到新 epoch 也不能重建。source refs 由 Host 校验，不能信任模型编造 provenance；不引入内容 fingerprint 或语义去重来冒充此保证。
- 用户后来明确要求重新记住可以在新 user turn 下建立新版本；原始业务记录与审计不删除。
- 对话摘要可能含旧偏好，注入时附当前记忆版本/纠正事实并以其为准；测试必须证明没有从旧摘要恢复已删记忆或宣称仍有效。
- progress 状态读取 B6 原 run 上的 progress 元数据与 resolution，不能只看原 run 执行终态；完成后不自动召回，取消后仅用户显式继续时可导航到原 run。仅 unfinished 默认参与继续，不让旧未完成条目永久覆盖当前任务。

### 实现补充与验收限制

- 个人记忆采用用户原文或已持久化权威 observation 的可验证摘录；不把模型自由总结直接写成经验。
- 跨会话继续发送“继续 run_…”明确绑定原事项；多匹配不自动选择。closure 需本请求重新取得原业务 tool/query 锚点、足够 coverage 和提交时账户权限；只有记忆维护或旁支回答不能关闭原事项。没有原始业务锚点时保留进度供后续查阅。
- 每个回执 SQL 源最多扫描 500 条，JSON 来源上限 2 MiB；超过时返回部分覆盖并要求收窄。正文和事件续页共用有界 cursor。
- 迁移命令必须指定 --host-db，可重复 --config / --pi-db 枚举所有实际来源；先 --dry-run。显式执行还需 --apply --writers-stopped，生产维护窗口不属于本次开发授权。
- 离线 fixture 及模拟 Pi 测试验证存储、权限、预算和协议链路。真实 provider 耗时、自然语言诊断质量、历史 Pi 摘要影响和生产渠道送达仍需目标环境验收。
- SQLite reply outbox 以原 delivery_key 持久化并读回；渠道回调消费已提交载荷，生成成功不代表外部已送达。
- 预算失败的用户答复与进度同事务保存：列出已读取证据数量、覆盖缺口、实际停止原因、未完成问题和继续编号，不把未提交模型文字作为结论。目录激活工具后压缩按当前活动 schema 估算，仍遵守 70% 触发、50% 目标、75% 准入。
- 合法长记忆返回有界预览及 id/revision，明确 partial；原文仍完整保存。记忆预加载故障不阻断普通业务问答，本轮禁用记忆维护并跳过后台整理；取消胜者恢复为 cancelled，默认不召回。
- 同幂等键记忆写入在 MEMORY_UNAVAILABLE 后最多允许一次相同参数重试，用于读回已提交结果；总时钟、工具次数和失败预算不重置。普通业务重复失败仍被抑制。
- 回执市场始终由可信范围绑定；混合账户 batch 以一个物理事件表示，并校验正文全部账户及成员市场。带迁移标记的数据库必须同时有完整匹配的迁移清单。
- 回执投影在正文分页前对完整文本脱敏；正文范围/hash描述脱敏投影，source watermark仍绑定原始保留内容，原文变化即使脱敏后相同也使cursor失效。
- 新生命周期回执从规范化execution/case保留市场；维护回执保存实际market_filter且不改变receipt_key；定时无通知结果从本次run的明确单一市场保留关联。历史缺项和混合/冲突市场仍不可关联，不从当前配置猜测。新intent的既有payload hash自然包含市场，既存事件和发送记录不重写，经济裁决及幂等算法不变。
- 显式记忆搜索先在完整授权历史中按查询过滤再限制候选；自动召回仍可带稳定偏好。答案修复每次启动新SDK循环时同步该循环实际工具集合，避免沿用上轮schema计量。
- 排队已超180秒的已授权消息通过既有Inbound错误envelope和reply outbox说明未完成，原deadline不重置、无模型/业务解析/Control执行，也不伪造Bot run进度；未授权/关闭回复保持静默，重复消息沿用幂等回复。



### B8. 行为切片与验收

| Slice | 独立可验证的行为 | 主要 owner | PRD |
| --- | --- | --- | --- |
| S1 | Bot 新入口可用、旧名不可用，迁移后原会话/回执/待发送记录不丢失 | bot 模块、CLI/assistant/channels、配置、Host migration、docs/packaging | A1、A9 |
| S2 | 用户不粘贴回执即可查询三类内容、分析和追问当前状态 | 源 owners、receipt_read、registry/Scene、evidence contracts | A2、A3、A4、A10 |
| S3 | 长对话在共同预算下收尾/取消/继续，跨会话记忆可用且可纠正删除 | Host/Pi bridge、memory store/worker、渠道交互和 eval | A5、A6、A7、A8、A9 |

S2 在保留旧功能的 S1 上验证；S3 复用 S2 真实回执作为连续调查测试，容量与记忆共同验证，避免独立看似通过却合并后溢出。
切片内部用最小失败用例推进，全部完成才做完整 Deepreview；不按文件分十余个机械阶段。

验收场景固定如下；已执行结果和未验收边界见 handoff，不以场景清单代替通过证据：
1. S1：临时 SQLite/配置夹具演练 dry-run、apply、重复迁移、中断恢复、冲突、WAL 与待发送幂等；新旧入口及配置拒绝；身份和 lease 不漂移。
2. S2：真实脱敏成交错误 fixture + 定时/监控独立场景；原文与报告重渲染区别；多用户/账户/配置、重复/乱序、缺失/损坏、分页失效/超保留、路径与注入边界；仅读取，无发送/重跑/ledger 写入。
3. S3：历史失败种子在当前基线复现/已修复确认；长历史压缩、巨大工具结果、错误修复、最后一次 submit、慢准备/慢模型/慢工具、cancel/late callback、重启/继续，计时从入口到提交。
4. 记忆：跨新会话召回、账户隔离、显式 CAS、删除时后台整理在途、旧摘要/旧任务防复活、写入失败、队列恢复及不阻塞回答；进度不重放任何副作用。
5. 现有 tests/test_bot_phase1.py、test_bot_conversation_memory.py、test_bot_s8_host_admission.py、test_pi_agent_process.py 及 receipt/notification/inbound 测试；新增测试集中在相关 owner，而不是复制每层同一套断言。
6. 混合 Python/Node import/protocol smoke、项目 lint/guardrails、必要依赖图与配置 dry-run；按风险执行完整 pytest。更改 import/tests 后生成 docs/DEPENDENCY_GRAPH.md。
7. 固定数据/模型/预算、包含未参与调优案例；关键 E2E 连续 3 次通过、每次 <=180 秒；记录完成率/时延/token/调用/失败归因和答案证据。真实外部模型验收在授权环境进行，当前设计阶段不发起。
8. 三次通过不等于统计成功率。取消、读失败或预算不足不得被计为 answered；合法的证据缺口回答按 PRD 判定，而非文字匹配。
9. 集中在公共入口验证必要反例：单事件正文超过 4000 token 且依据在末尾；valid empty/partial 的 evidence admission；排队/准备超时和亚秒剩余；Host 终态/outbox 间崩溃；取消/截止与 SQLite 锁竞争的两种赢家；失败/取消/进程退出后跨新会话继续；删除后生成新摘要/新任务不复活；已交付但未创建 job 时重启；前台到来让行；legacy 启动先于 schema ensure、在途发送拒绝迁移；未认证 CLI 个人记忆拒绝。每个用例验证 durable 或用户可见行为，不为每层复制测试。
10. 新会话继续完成原调查后再开会话不重复召回旧事项，旁支/澄清回答不误关闭，CAS 竞争不丢新进度；记忆纠正后让新 epoch/new id 的后台任务重新读原始来源，旧内容仍不能另建，新的真实用户声明仍可记住。

### B9. 风险、替代方案与开放项

- SDK 0.85.1 升级需要会话 API/数据验证，第一版暂不强耦合；评价材料已完成，未来迁移未完成。
- 无旧名兼容需要一次性运维切换窗口；源码交付不授权生产迁移。现存未知外部调用不能假设不存在。
- 三类 source 的可查询范围受现有持久化和 retention 限制，新增字段只覆盖未来事件，历史缺项如实给出。
- 后台记忆需防止和前台争用模型/SQLite；有界槽、短事务、来源检查与显式失败不可省略。
- 仅凭类型声明无法证明 Pi 库升级/回滚，不能在无副本演练时改变 SDK。
- 设计评审需重点挑战 receipt 原文关联、权限、总计时边界、删除防复活和 migration 中断，不以新增功能扩大第一版。
- 未选 Hermes/新 runtime：不消除业务回执、Host 治理与数据授权工作；未选向量库：现有有限记忆尚无检索证据要求；未选仅抬预算：已有失败远早于180秒。
- 产品合同无未决项；source 覆盖与迁移对象以 B4/B5 为实现边界，实际环境中的路径/数量由 dry-run 枚举，不在设计阶段臆测。未知源或历史缺项按 partial/unavailable 呈现；实现不能把未验证的 source 标记完整。必要验收 fixture 以 B8 固定，后续发现的缺口按四项合同裁决。

## Purpose

Bot is the general conversational Agent for options-monitor. It must
answer free-form operational and options-monitor questions with canonical data,
maintain useful multi-turn context, survive runtime failures, and hand requested
state changes to deterministic Control.

Monthly income, option-operation review, exposure analysis, candidate diagnosis,
and notification diagnosis are evaluation cases. None is a dedicated runtime
capability, router branch, Scene, or answer template.

## Runtime Shape

```text
Channel / CLI UI
-> Bot Service
-> Bot Host
-> generic Agent / Engine
-> canonical pure-read tools
   -> OM local read models
   -> portfolio_query / portfolio_pnl_bridge / portfolio_cash_bridge
      -> portfolio-management loopback HTTP API

Agent
-> request_control_preview
-> deterministic Control preview
-> explicit confirm / cancel
-> deterministic apply and readback
```

The stable layers are `UI -> Service -> Host -> Agent`. Contract preparation,
Scene preparation, structured-memory injection, event storage, and tool projection are
mechanisms inside those layers, not additional architecture layers.

`./om-agent` is a structured Tool Gateway for external agents. It is a UI entry,
not OM's autonomous Agent. Both `./om-agent` and Bot derive tool schemas and
descriptions from `agent_tool_registry.py` and `agent_tools/`.

## Invariants

- There is one general Scene: `om_chat`.
- The `om_chat` Scene is `v5` and compiles one ordered five-fragment prompt
  pack. Repository operator instructions are not runtime prompt input.
- Service does not classify free text into OM business tasks.
- Service does not parse month, symbol, account, or intent from free text.
- Host owns execution governance; Agent owns generic model/tool iteration.
- After the Agent selects an option-performance read tool, Host may bind only
  the unique current-message period attestation defined below. This is an
  input-authority fence, not Service routing or financial interpretation.
- Agent and Engine contain no OM task routing or strategy-specific branches.
- Bot receives canonical pure-read tools only. The `portfolio` toolset is an
  optional read boundary, disabled by default and projected only when
  `assistant.enabled`, `assistant.bot.enabled`, and
  `assistant.bot.toolsets.portfolio` are all true. It is not a second
  Bot, Scene, router, or Agent runtime.
- The model may request a validated Control preview but cannot confirm, cancel,
  apply, or call a direct mutation tool.
- Explicit commands and pending-operation replies remain deterministic Control.
- There is no old planner, perception, reasoning, evidence, verifier, or answer
  renderer fallback for free-form chat.
- Missing data is explicit. A tool failure is not converted into an invented
  financial conclusion.
- Trace records execution facts and failures, never private chain-of-thought.
## Typed-Tool-Only Product Boundary

Bot exposes canonical typed read tools rather than an embedded SQL/BI
workspace. Pi Agent Core remains the only model/tool loop; Host enforces scope,
allowlists, budgets, cancellation, audit, and answer admission. Business
calculation stays with each canonical tool owner.

The retired `analysis_catalog` and `analysis_query` names are absent from both
Bot and `./om-agent`. There is no compatibility alias, replacement DSL,
generic aggregation tool, session adapter, or second Scene. The current
`om_chat` identifier remains `v5`.

### Exit Inventory And Result

The breaking exit was accepted by product/operations owner `liuxie`. The
inventory covered source, tests, scripts, living docs, configs, service
templates, and repository-root entry points at
`origin/main@68b370f6` on 2026-09-02T20:00:18+08:00. No service or runtime-config
caller was found. Repository-independent Tool Gateway telemetry was not
available, so absence of an unknown external caller was never assumed.

| Eager Scene business projection | Business tool count | Serialized business schema | Business catalog hash |
|---|---:|---:|---|
| Before removal | 20 | 24,869 UTF-8 bytes | `sha256:6927c37523411c6104c0ae910078546c77a4f190e1327a2a4a4fabcf57a12d46` |
| Current | 18 | 21,256 UTF-8 bytes | `sha256:be4977e39e2173d8b25a95677f878ad7113d4a2e7dc8ca347f29956d17094b62` |

These measurements cover the business tools selected by the Scene before Host
adds protocol tools. The current local eager provider projection is 19 tools
and 22,626 UTF-8 bytes after adding `submit_answer`; channel runs may also add
Control preview. Runtime rollout verification uses
`scene_prepared.tool_count` and `scene_prepared.tool_schema_sha256`, not the
business catalog hash in this table.

The business schema payload is 3,613 bytes smaller. This is a payload
measurement, not a claim about wall-clock latency. The retired database was in
memory, so the change does not reduce persistent storage.

Against the pinned base, production source and scripts are 4,131 lines smaller
net; tests are 1,409 lines smaller net, for a combined 5,540-line reduction.

### Supported And Removed Capabilities

| Need | Current owner and boundary |
|---|---|
| Period option income and cash components | `option_performance_report`; aggregate evidence only for Bot. |
| Position facts | `option_positions_read action=list`; only declared bounded rows. |
| Option event history | `option_positions_read action=events`; canonical pagination applies. |
| Symbol configuration | `symbol_config_read`; one symbol per read. |
| Close advice | `close_advice_read`; strict owner. |
| Runtime and delivery diagnosis | Runtime and notification-perception tools; declared facts only. |
| Operation evidence | `operation_timeline`; raw operation facts rather than a derived upgrade summary. |

In the Bot model projection, symbol performance attribution, bulk
configuration comparison, expiration buckets, grouped lifecycles, replay
views, derived upgrade summaries, arbitrary SQL, cross-view joins, and exact
aggregates over incomplete position coverage are intentionally unavailable. A
typed tool is not expanded merely to preserve an old view. A recurring missing
need requires a separate requirement at the canonical business owner.

### Failure And Compatibility Behavior

- Calling a retired name returns the existing unknown-tool or allowlist failure,
  with no implicit substitute and no side effect.
- Partial, stale, missing, paginated, or insufficiently projected evidence must
  produce a narrowed or incomplete answer, never an inferred exact aggregate.
- Persisted conversation history is preserved but unsupported as current
  evidence. Old observations cannot satisfy current-request admission; if a
  provider rejects old tool history, the operator starts a new conversation.
- A rollout must not mix workers with different catalogs. Deployment and
  per-instance hash verification remain separately authorized operations.

Pi Agent Core, deterministic Control, account/config isolation, write authority,
persistent stores, Tick, ledger, and canonical calculation contracts are
unchanged.

## UI Boundary

UI adapters own transport concerns:

- message extraction and channel identity;
- sender and conversation identity;
- configuration and model-profile selection;
- rendering the returned response;
- delivery receipts and channel-specific idempotency.

UI selects the default `om_chat` entry surface. It does not choose business
tools, task kinds, evidence plans, or prompt variants.

## Service Boundary

`src/application/bot/service.py` is a thin contract-preparation service. It:

1. validates non-empty user text;
2. normalizes explicit UI scope only;
3. appends the current user message to supplied conversation context;
4. selects the default `om_chat` Scene;
5. emits a read-first execution contract.

Service must not import Host, Agent, Engine, tool implementations, or model
providers. It cannot decide which OM tool should answer a question.

## Host Boundary

Host responsibilities:

- validate the execution contract and Scene;
- project Scene-approved canonical tools;
- prepare prompt, context, structured memory, and current Control snapshot;
- own session and run lifecycle;
- enforce per-session exclusion and concurrency lanes;
- enforce timeout, turn, tool-call, retry, and context budgets;
- propagate cancellation;
- persist events, run state, metrics, and final result;
- record the exact prompt and provider-visible tool projection fingerprints
  before Engine execution without persisting prompt text;
- recover interrupted pure-read runs;
- maintain the reply outbox;
- expose coarse progress events.

Host must not classify business intent, choose evidence recipes, interpret
financial data, or rewrite the model's answer into a second answer system.

## Deterministic Option-Performance Input Binding

### Goal, Non-Goals, And Success Signals

The goal is to prevent a correct natural-period request from failing because
the model chose the right read tool but supplied a conflicting period payload.
For example, `期权8月收益` must execute as the attested natural month rather
than fail because the model proposed `mtd`.

Success requires all of the following:

- an unambiguous current-message month or year request reaches
  `option_performance_report` with the matching `period` plus `month` or `year`;
- a case-insensitive bare `MTD` or `YTD` token in an option-performance phrase
  remains valid when it directly touches CJK text or punctuation, while an
  ASCII identifier that merely contains `mtd` or `ytd` remains invalid;
- the sanitized model-proposed input and the effective input are both
  auditable, including when preparation rejects the call before execution;
- ambiguous, malformed, or future selectors still fail before any business
  read, while unrelated text retains generic MTD/YTD behavior;
- successful observations returned to the model expose the effective bound
  period scope rather than the conflicting proposal;
- direct Tool Gateway behavior and canonical performance calculations remain
  unchanged;
- model-visible tool descriptions state the valid parameter combinations for
  the performance read and answer submission.

This work does not add a general intent router, a second answer renderer, a new
tool or event store, or a provider-specific strict-tool runtime. Host does not
calculate income, translate a natural month into MTD, or choose a performance
tool for the model.

### Current Facts And Constraints

- The current-message fence parses closed MTD/YTD cutoffs and natural month/year
  selectors for `option_performance_report`.
- The generic MTD/YTD token guard is lexical, not a calendar-period owner. It
  currently uses Python's Unicode word boundary, which incorrectly rejects a
  bare token adjacent to CJK text; this slice changes only that boundary.
- It currently compares the model proposal with that attestation and rejects a
  mismatch, although the attested values are already known deterministically.
- A pre-execution rejection records the failure observation but not the model's
  sanitized business-tool arguments, so the exact bad proposal cannot later be
  reconstructed from the run.
- The canonical period owner accepts `mtd`, `ytd`, `month`, and `year`; it owns
  calendar-window semantics and remains the final validator.
- Current contracts freeze `operating_date` from one `report_now_ms` in
  Asia/Shanghai; Host must not substitute an ambient machine-local date when
  that authority is missing or malformed.
- Result admission requires an empty claim list in `conceptual` mode and at
  least one evidence claim in `evidence` mode. The validator enforces this, but
  the projected `submit_answer` description must make the coupling explicit.

### Chosen Design And Data Flow

The existing closed selector parser remains the sole attestation source. Once
the Agent calls `option_performance_report`, Host performs:

```text
model-selected read tool + model arguments
-> bounded model-input audit projection
-> current-message option-period attestation
-> reconcile with trusted fixed scope
-> bind attested period fields into a copy of model arguments
-> ordinary payload preparation, normalization, and fixed-scope merge
-> canonical tool validation and execution
-> evidence admission
```

Binding is limited to `period`, `as_of_date`, `month`, and `year`. Host replaces
those fields with the attested values and removes incompatible sibling selector
fields. Account, broker, config, and every other model or fixed-scope field are
preserved.

Authority precedence is:

```text
trusted fixed scope > current-message attestation > model proposal/default
```

For this option-performance tool, a trusted fixed `month` denotes
the complete scope `period=month, month=<fixed>`. If the current message has no
selector, that fixed scope is bound. If the message attests the same month,
execution continues; a different month, a year, or an MTD/YTD cutoff conflicts
and returns `INPUT_ERROR` before the business read. Invalid, ambiguous, or
future current-message selectors also reject instead of falling back to the
fixed month. Binding never overwrites trusted fixed scope.

After the authoritative scope is selected, Host copies the model arguments,
replaces the four period fields, removes incompatible siblings, and only then
runs ordinary tool preparation. A conflicting or empty model-proposed period
sibling therefore cannot reject a selector that Host already knows exactly.

Attestation has four closed states:

- `none`: no option-performance selector or selector-like text is present;
- `unique_valid`: exactly one supported selector is present and may be bound;
- `invalid_or_ambiguous`: selector-like option-performance text is malformed,
  incomplete, conflicting, or contains multiple natural selectors;
- `future`: the selector resolves after the frozen Asia/Shanghai
  `operating_date`.

`invalid_or_ambiguous` and `future` reject before the business read. The
malformed boundary is exact: it must match one of the two existing
option-performance phrase orderings (`期权 <selector> 收益` or
`<selector> 期权收益`) while the isolated selector token fails the closed valid
selector grammar. Text that does not match either performance phrase ordering
is `none`; Host does not add a general Chinese date or intent parser. Explicit
MTD/YTD cutoffs must not be after `operating_date`.

Period attestation requires a valid contract `operating_date`, or a valid
frozen `report_now_ms` from which Host derives the same Asia/Shanghai date. If
neither is available, Host returns `INPUT_ERROR` before binding or reading; it
never falls back to the ambient process date. This intentionally makes a legacy
or damaged persisted run non-resumable for this read instead of changing its
calendar scope.

The accepted selector states are:

| Current-message selector | Effective performance input |
|---|---|
| explicit MTD cutoff | `period=mtd`, matching `as_of_date` |
| explicit YTD cutoff | `period=ytd`, matching `as_of_date` |
| one natural month | `period=month`, canonical `month=YYYY-MM` |
| one natural year | `period=year`, canonical integer `year` |
| bare `MTD` or `YTD` in a performance phrase | existing generic MTD/YTD behavior; surrounding whitespace is optional |
| no attested natural selector | existing generic MTD/YTD behavior; unauthorized `as_of_date` is removed |

Bare MTD/YTD detection is case-insensitive and treats only ASCII letters,
digits, and underscore as identifier continuations. `期权ytd收益` and
`mtd期权收益` are therefore valid, while `期权mytd收益` and `期权ytdx收益`
remain invalid. Host does not rewrite the current message or add a second
natural-language parser.

Multiple natural selectors, invalid dates, future periods, and selector-like
text outside the closed grammar remain rejected before execution. The parser is
not broadened to infer arbitrary phrasing. Direct `./om-agent` calls remain
governed by the tool schema and canonical period owner without this
conversation-only binding.

For business read calls, trace extends the existing audit sanitizer with one
bounded input projection used identically for `model_input` and `tool_input`:

- recognized schema fields other than free-form query text retain their values
  after the existing secret/path redaction and cursor hashing;
- `sql` and `query` retain only `{type, length, sha256}`;
- unsupported field names remain visible, while their values retain only type,
  length where applicable, and SHA-256;
- the complete projection uses the existing 4,000-token observation ceiling;
  an oversized field or projection collapses to the same metadata shape.

The event records:

- `model_input`: the bounded projection of arguments as the model proposed them;
- `model_input_hash`: a stable hash of the complete model proposal;
- `tool_input`: the effective bound payload only after ordinary tool preparation
  succeeds, through the same bounded projection;
- the existing error code and message when preparation or attestation rejects.

A rejected call keeps these diagnostics in its existing `tool_result` event;
no synthetic successful `tool_call` is created. Secrets and configured paths
remain redacted, cursor-like values remain hashed, input projection depth and
size remain bounded, and raw SQL/free text, answer text, prompts, messages, and
private reasoning remain absent. `submit_answer` continues to log only mode,
status, references, admission outcome, and approved-answer hash.

The two business-read audit states are:

1. rejected before execution, including attestation, fixed-scope reconciliation,
   or ordinary preparation: sanitized `model_input` and `model_input_hash`, with
   no `tool_input`;
2. execution attempted: sanitized `model_input`, `model_input_hash`, and the
   effective bound `tool_input` sent to the canonical tool, whether the returned
   observation succeeds or fails.

The projected direct-report schema states that `month` is valid only with
`period=month`, `year` only with `period=year`, and `as_of_date` only with an
explicit MTD/YTD cutoff. The projected `submit_answer` description
states that `conceptual` requires `claims=[]`, while `evidence` requires at
least one claim referencing successful current-request observations. Existing
prompt rules remain the general behavioral owner; no question-specific prompt
is added.

### Failure Behavior And State Transitions

Binding does not introduce a new run state. A call remains either rejected
before execution, executed with a failed observation, or executed with a
successful evidence observation. Only the last state can support factual
claims.

If a natural-period payload lacks a matching attestation, or attestation is
invalid, ambiguous, future, or conflicts with trusted scope, Host returns the
existing recoverable `INPUT_ERROR` observation with the sanitized model input
attached to the trace. `none` still permits generic MTD/YTD without a cutoff and
removes an unauthorized model-proposed `as_of_date`. The model may repair the
call within existing budgets. If no admissible evidence exists, the model may
submit a conceptual missing-evidence explanation with empty claims; Host does
not manufacture an answer on its behalf.

### Implementation Slices

1. Replace comparison-only option-period fencing with narrow pre-normalization
   binding at the existing Host ownership point, enforce trusted-scope
   precedence, and preserve fail-closed cases.
2. Extend the existing audit sanitizer with the bounded input projection and add
   `model_input` alongside effective `tool_input` in existing business read
   events, including pre-execution rejection, without persisting SQL or unknown
   field values.
3. Align the projected performance and `submit_answer` descriptions with their
   validators, and align the linked Pi integration owner with this binding
   contract.
4. Add focused Host, trace-redaction, schema-description, and result-admission
   regressions; then run the existing Bot and Agent contract checks.
5. Change the existing Host `_OPTION_PERFORMANCE_PERIOD_TOKEN` guard to use
   `re.ASCII | re.IGNORECASE`, and extend the existing public `run_contract`
   parameter tables rather than adding a normalizer, parser, or test harness.

### Validation Plan

- Prove `期权8月收益` executes with the canonical natural-month input even when
  the model proposes MTD, and prove the same behavior for natural year and
  explicit MTD/YTD cutoffs.
- Cover `option_performance_report`.
- Prove the returned observation exposes the bound period scope, followed by a
  source-declared evidence claim that `submit_answer` accepts and a terminal
  `answered` result without `ANSWER_ADMISSION_FAILED`.
- Prove ambiguous, future, and malformed selectors, including `期权13月收益`,
  perform no business read; unrelated text preserves generic MTD/YTD behavior.
- Through the public `run_contract` path, prove `期权yTd收益` and `MtD期权收益`
  work without spaces, while ASCII-prefix, suffix, digit, and underscore
  continuations reject with no business read.
- Prove fixed month scope alone binds `period=month`, equal message attestation
  succeeds, and a different month/year or MTD/YTD message scope rejects before
  the read.
- Prove bare-month resolution across January and the Asia/Shanghai day boundary,
  and prove future MTD/YTD cutoffs reject before the read.
- Prove missing or malformed `operating_date` falls back only to a valid frozen
  `report_now_ms`; when both are unusable, the call rejects without a read.
- Prove successful traces contain sanitized model/effective inputs and rejected
  traces retain sanitized model input without creating a successful tool call.
- Prove every pre-execution rejection has no `tool_input`, while successful and
  failed execution attempts record the effective bound input.
- Prove configured paths remain redacted, cursors remain hashed, and rejected
  answer bodies, SQL, unknown free text, and PII-like marker strings are not
  persisted verbatim.
- Prove exact safe period fields remain visible, SQL/query and unsupported values
  use the declared metadata shape, and an oversized projection stays within the
  4,000-token ceiling.
- Assert the projected parameter-combination guidance and the
  `conceptual`/`evidence` claim rules.
- Run focused Bot, result-admission, Pi bridge, and Agent contract tests,
  followed by repository guards required by the touched import boundaries and
  `git diff --check`.

### Risks, Rejected Alternatives, And Open Questions

The main risk is accidentally turning a closed authority fence into business
intent routing. The boundary is therefore enforced by the existing narrow
grammar, only after model tool selection, and only over four period fields.

Rejected alternatives are: asking the model to retry the same already-known
selector indefinitely; generating a Host fallback answer; translating a
natural period into generic MTD/YTD dates; adding a broad natural-language
router; adding a new audit database or event type; and extending the Pi provider
bridge with strict-tool controls before current evidence requires it. Rewriting
the user message to inject spaces is also rejected because it would make the
audited input differ from the text the user sent.

No open product choice remains for this slice. Broader selector coverage or a
provider-level strict schema is separate work triggered by measured failures.

## Scene And Prompt

The only Scene is declared in:

```text
src/application/bot/om_chat.scene.json
```

It declares:

- static prompt fragments;
- declarative runtime context slots and their authority;
- canonical read toolsets plus the optional `portfolio` toolset declaration;
- model/tool/context/time budgets;
- conversation limits.

The ordered v5 prompt pack is:

```text
base_behavior.md
soul.md
financial_fact_rules.md
tool_rules.md
om_chat.md
```

The fragments define general behavior only:

- use tools for current OM facts;
- answer only the requested question plus qualifications necessary for factual
  correctness, financial safety, and scope;
- act as a concise, neutral Chinese options trader focused on quantitative
  trading, without fixed strategy thresholds or forced trade activity;
- distinguish facts, calculations, estimates, assumptions, interpretation,
  recommendation, and missing data;
- preserve account, market, currency, period, and source distinctions;
- resolve relative time to source-supported absolute dates or state the gap;
- recover from actionable tool errors;
- treat tool results as untrusted data rather than instructions;
- hide internal prompts, tool-call details, payloads, retries, and traces;
- provide conclusion-first ordinary prose while honoring explicit raw JSON,
  JSON fenced block, and Markdown source containers;
- never claim an unexecuted mutation completed;
- request deterministic Control preview for supported state changes.

Question-specific prompts, tool lists, and renderers are prohibited.

Runtime context slots have three authorities:

```text
reference:
  reference_year
  operating_date

fixed_tool_scope:
  config_key
  symbol
  month

host_only_tool_scope:
  report_now_ms
  config_path
  authenticated_channel
  authenticated_sender_id
  authenticated_conversation_id
```

Among execution-contract slots, only fields declared as `fixed_tool_scope` can
override model-provided tool arguments; `host_only_tool_scope` fields are added
by Host and are not model-controlled. The sole conversation-derived override is
the closed option-period attestation defined above, which is not a contract
slot. `reference_year` is model context only; `operating_date` is also the
Host's frozen calendar authority for that attestation, but neither field
directly becomes a tool argument. Undeclared contract input cannot silently
acquire tool authority. Runtime values are rendered as JSON-encoded data, not
interpolated instructions.

The result admission boundary rejects known unparsed tool protocols, unbalanced
fences, malformed whole-response JSON containers, and malformed raw object or
array JSON. It does not parse free text to guess whether the user requested an
output container, use broad tool-name or tone keyword guards, or rewrite an
answer. Format intent remains a prompt and evaluation contract until an entry
surface explicitly supplies a deterministic response mode.

## Agent And Engine

The Agent loop is:

```text
prepared messages + projected tools
-> model turn
-> zero or more native tool calls
-> Host-supplied tool execution
-> tool observations
-> next model turn
-> model final text or explicit terminal failure
```

The Engine supports:

- native model tool calls;
- bounded transient retries;
- duplicate-call protection;
- recoverable invalid-argument observations;
- continuation after provider length truncation;
- bounded context compaction that preserves the current user request and every
  current-turn native tool-call/result group by distributing the available
  budget before admitting older conversation groups;
- observation continuation for large results;
- final-answer reserve;
- cooperative cancellation;
- stable iteration IDs and context hashes;
- token usage and termination metrics.

There is no fixed collection fallback. If the model does not call a necessary
tool, that is an answer-quality failure to diagnose through trace and evaluation,
not a reason for Host to run a hidden business workflow.

## Tool Boundary

`src/application/bot/tools.py` is a generic adapter. It:

- selects pure-read definitions from the canonical registry;
- exposes canonical descriptions and JSON schemas;
- merges safe defaults, model arguments, and only the Scene-declared fixed tool
  scope;
- executes only Host-allowed pure-read tools;
- converts canonical results into flat Agent-friendly observations;
- exposes `portfolio_query`, `portfolio_pnl_bridge`, and
  `portfolio_cash_bridge` through the `portfolio` toolset using GET-only stdlib
  HTTP against `PORTFOLIO_SERVICE_URL` (default
  `http://127.0.0.1:8765`); the two bridges keep total-asset PnL and cash
  movement separate, use PM's actual period-end facts, and return structured
  steps plus Markdown fallback text without image rendering;
- provides compact previews and continuation metadata.

Tool descriptions, defaults, validation, error hints, and output contracts should
be fixed at the owning tool definition. Bot must not maintain a second tool
catalog or question-specific evidence recipe. `portfolio_query` accepts only
view and query scope; it rejects model-provided endpoints and non-loopback service
URLs, exposes no portfolio write endpoint, and preserves source/scope/freshness.
Disabling the optional toolset removes both its model-visible description and
its Host allowlist entry before Agent execution. Engine allowlist enforcement
still rejects a model-emitted call that was not projected. Resume rebuilds the
Scene from current assistant config, so a later disable revokes resumed access.

## Deterministic Control

The model-visible `request_control_preview` surface is generated from the
deterministic Control capability catalog. A valid request creates a Control
preview and pending operation. It does not apply the operation.

```text
model preview request
-> schema and capability validation
-> deterministic preview
-> pending operation
-> explicit contextual confirmation or cancellation
-> deterministic apply
-> readback receipt
```

Current pending operations are injected into every channel turn from the
operation store. This snapshot is newer and more authoritative than conversation
history or structured memory.

`取消分析` targets an active Bot run. `取消执行` targets a pending Control
operation. These are distinct state machines.

## Conversation Memory

Current conversation transcripts and compaction are owned by Pi Session, as
specified in [PI_AGENT_CORE_INTEGRATION.md](PI_AGENT_CORE_INTEGRATION.md#6-memory-and-context).
The active channel path does not inject or dual-write the retired Host
session_memory store. Old Host rows remain historical data and do not prove a
trusted sender identity. Current Control state is refreshed from its owner.

Cross-session long-term memory is a planned Bot change defined in B7 above;
it is not a currently released capability. Memory never replaces current
financial or runtime evidence, and cannot grant Control authority.

## Durable Runs, Resume, And Cancellation

Host persists:

- execution contract;
- session key;
- run state and events;
- cancellation request;
- resumed-from identity and attempt count;
- termination reason and aggregate metrics;
- final response.

Active states are `running`, `waiting_model`, and `waiting_tool`. Terminal states
include `answered`, `control_requested`, `failed`, `cancelled`, and `interrupted`.
Stale active runs are marked interrupted after process failure.

Resume rules:

- only failed or interrupted read-first contracts are eligible;
- attempts are bounded;
- resume creates a new run linked by `resumed_from`;
- only successful pure-read observations are recovered;
- identical recovered reads are not repeated;
- Control previews, confirmations, cancellations, applies, and writes are never
  replayed automatically.

Cancellation is checked before and after provider calls, during retry backoff,
and before and after tool execution. The current synchronous provider transport
cannot forcibly abort an already-blocked socket read; cancellation still prevents
the next model or tool step and is observed immediately after the call returns.

## Trace And Progress

Every model iteration records:

- `iteration_id`;
- sanitized context hash and size;
- force-finish state and tool count;
- finish reason and attempt count;
- input/output/total token usage where available;
- categorized provider failure;
- partial malformed tool-call arguments where available.

Before the first model iteration, `scene_prepared` records:

- Scene name and version;
- ordered fragment paths, lengths, and SHA-256 hashes;
- compiled prompt SHA-256;
- selected toolsets;
- provider-visible tool count and schema SHA-256.

The tool fingerprint covers exactly `name`, `description`, and `input_schema`,
including the projected Control preview tool. It changes when optional toolsets
change. Prompt text, user messages, tool results, and secrets are never included.
The static Scene fingerprint is separate from the per-turn dynamic
`context_hash`. A resumed run rebuilds the current Scene and records its own
fingerprint.

Run records aggregate model turns, tool calls, retries, token usage, status, and
termination reason. Business read events retain sanitized model-proposed input
and effective input when available, including the proposal attached to a
pre-execution rejection. Trace payloads are sanitized execution facts, not
reasoning.

Public progress is derived from stable events and exposes only labels such as:

- `正在分析`;
- `正在读取数据`;
- `正在继续分析`;
- `正在整理结论`;
- `等待确认`;
- `已取消`;
- `执行完成`.

## Reply Outbox

Channel replies use a SQLite outbox:

```text
pending -> delivering -> delivered
                    -> retryable_failed -> delivering
                    -> terminal_failed
```

`delivery_key` is unique. Enqueue is idempotent, successful delivery is recorded,
and retryable channel failures are retried by the channel worker after process or
transport recovery. Existing channel-level provider receipts remain an additional
idempotency layer.

## Concurrency

OM uses lightweight Host leases rather than a general multi-tenant governor:

```text
chat_read: 2
control: independent
```

The same conversation permits one active Agent run. Expired leases are removed
so process failure cannot permanently block a session. Control remains outside
the read lane and must not wait behind a long model run.

`heavy_analysis` is not introduced until measured production contention proves a
separate lane is necessary; adding it now would require business classification
that the Service is explicitly forbidden to perform.

## Failure Behavior

| Failure | Required result |
|---|---|
| Empty question | `needs_clarification` before Host |
| Model not configured | `not_ready`, no tool call |
| Contract or Scene invalid | explicit failure |
| Tool arguments invalid | bind a unique attested option-period scope; otherwise return a recoverable observation with repair hint |
| Tool unavailable or data missing | explicit gap preserved |
| Repeated identical call | duplicate call rejected |
| Provider timeout/error | categorized event and bounded failure |
| Run budget exhausted | bounded final answer or explicit failure |
| Cancellation | partial events preserved; run `cancelled` |
| Process failure | stale active run becomes `interrupted` |
| Concurrent same-session run | second run `not_ready` |
| Channel delivery failure | outbox `retryable_failed` and later retry |

There is no fallback to old Assistant planning or unevidenced generic chat.

## Evaluation

Deterministic CI uses fixture observations and explicit model turns. Real-model
acceptance is executed by the trusted production environment with actual
read-only OM data.

The fixed set covers:

- income and attribution follow-up;
- exposure concentration;
- option-operation review;
- account-scope follow-up;
- candidate diagnosis;
- close-advice notification diagnosis;
- missing-data honesty;
- write safety;
- no unsolicited expansion;
- evidence-based challenge to a high-yield/add-position premise;
- no-trade and wait conclusions;
- raw JSON, one JSON fenced block, and one Markdown source block;
- conclusion follow-up.

Each case captures all events, run identity, elapsed time, termination reason,
failure owner, selected tools, actual provider/model/runtime version, tool-call
and continuation metrics, output contract checks, Scene/tool fingerprints,
evidence-health checks, final answer, and six human-review dimensions:

- intent fulfillment;
- factual accuracy;
- scope and currency;
- missing-data honesty;
- actionability;
- conversation continuity.

No benchmark may become runtime routing, a dedicated Scene, or an answer template.

Production evaluation must receive the runtime root explicitly instead of
depending on a shell-specific inherited environment:

```bash
python3 scripts/bot_p1_eval.py \
  --assistant-config /var/lib/options-monitor/resolved/config.assistant.json \
  --config-key us \
  --runtime-root /var/lib/options-monitor \
  --output /tmp/om-bot-p1.json
```

The output contract is `om.bot.p1_eval.v4`. Structural and evidence checks
are mandatory CLI exit gates. Human answer-quality is also mandatory after
review, while an otherwise valid unreviewed report remains available for
offline scoring. Human review applies to the exact saved report without
rerunning the model:

```bash
python3 scripts/bot_p1_eval.py \
  --review-report /tmp/om-bot-p1.json \
  --review-input /tmp/om-bot-p1-review.json \
  --output /tmp/om-bot-p1-reviewed.json
```

The review input must contain every report case and all six 0..2 dimensions;
a reviewed case passes at 10/12 or higher. The report records the model actually
configured at runtime and must not assume a provider.

## Delivery Phases

| Phase | Deliverable | Exit gate |
|---|---|---|
| P0 | Stable rebuild baseline | Focused tests, guards, dependency graph, and diff checks pass. |
| P1 | Production answer-quality baseline | The configured production model produces sanitized eval-v4 traces and human scores. |
| P2 | Structured memory | Existing pinned state and episodes remain injectable without request-path model calls or memory writes. |
| P3 | Durable run control | Interrupted reads resume safely and cancellation stops further work. |
| P4 | Trace/model protocol | Iteration identity, usage, termination, and failure categories are persisted. |
| P5 | Progress/outbox | Coarse progress is pollable and final replies are idempotent and retryable. |
| P6 | Lightweight concurrency | Session and lane leases enforce limits and recover after expiry. |
| P7 | Tool remediation | Only production-trace-proven canonical tool gaps are changed. |
| P8 | Prompt remediation | Only failures with correct model-visible data justify prompt changes. |
| P9 | Cleanup/docs | One free-form path, one Scene, one registry, one Control owner remain. |
| P10 | Release/acceptance | Full checks and production behavioral acceptance pass. |

P1 is the gate for P7 and P8. Engineering work on generic Host reliability may
continue while production evaluation is scheduled, but tool- and prompt-specific
changes require captured evidence.

## Completion Criteria

The rebuild is complete only when:

- one general Scene exists and Service remains business-neutral;
- Host owns governance and Agent owns generic model/tool iteration;
- Bot exposes canonical pure-read tools plus validated Control preview only;
- free-form chat has no old planner/evidence/verifier/renderer fallback;
- structured memory, durable runs, resume, cancellation, trace, progress,
  outbox, and concurrency leases have regression coverage;
- explicit operations use one deterministic audited Control contract;
- deterministic Bot, Control, channel, config, and architecture tests pass;
- production real-model questions produce useful, factual conclusions;
- three independent real-model acceptance runs use the expected stable Scene v5
  prompt/tool fingerprints and pass every format and safety hard gate;
- quantitative persona cases use relevant supported evidence, avoid false
  precision and emotional language, and permit wait/no-trade conclusions;
- channel follow-ups preserve scope and current Control context;
- reply failure is retryable and idempotent;
- no free-form request can directly mutate OM state;
- docs and public commands describe the implementation that actually runs.

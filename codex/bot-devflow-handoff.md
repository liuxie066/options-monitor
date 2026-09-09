# Bot devflow 交接

## 输入与文档所有权

产品合同：[Bot 第一版 PRD](../docs/BOT_PRD.md)，批准依据只引用其第 10 节，不另存批准副本。
目标、范围、非目标、成功信号和验收分别以 PRD 第 1、9 节为准。当前用户授权保存 PRD 并启动 devflow。
本文件保存实现方向讨论和流程进度；不是实现设计真源。design_doc 为 ../docs/BOT_DESIGN.md，设计已通过第 2 轮 Planreview 并冻结；Implementation 已获用户本次“确认”批准。

## Brainstorm：推荐方案

沿用现有 Pi + Host + deterministic Control，在现有模块职责内完成第一版。
Pi 负责模型/工具循环，Host 负责身份、会话、预算与治理；已有受控操作继续交由 Control。
不引入 Hermes 服务、第二套 Agent runtime、通用 SQL/shell 工具或新的任务路由层。
收益是保留已有会话与权限、证据校验；代价是需要在当前工具和上下文链路中定位真实失败，不能靠更换引擎宣称解决。

| 产品合同 | 推荐实现方向 | 主要取舍与待查证事项 |
| --- | --- | --- |
| F1 完整更名 | 清点项目自有名称与调用方，统一 Bot/bot；数据通过一次性迁移保留 | 无旧名兼容会破坏外部旧配置；需明确拒绝、迁移冲突和重复执行规则，历史审计原文保留 |
| F2 回执上下文 | 沿用成交 inbox、通知/监控与任务现有数据 owner，补足有权限边界的业务内容检索和证据关联 | 摘要只用于定位；覆盖矩阵须识别三类回执哪些已有持久化、哪些需补最小字段，不能另建业务事实账本 |
| F3 容量 | 从当前基线复现历史失败，治理压缩、工具参数/重复错误、运行中增长和答案收尾，180 秒统一计时 | 不削弱准入校验；保留进度但用户手动继续；已有修复直接复用，不仅抬高预算 |
| F4 长期记忆 | 复用现有持久化设施，增加按身份/账户隔离的有限记忆记录和按需召回 | 与 Pi 对话记录及业务事实分工；明确写入成功后确认，异步整理不阻塞回答；删除抑制旧摘要恢复 |

实施设计需明确：三类回执读取源/标识/时间/权限矩阵、更名与迁移清单、总预算时钟与取消传播、失败原因和答案提交链、记忆来源/纠正/删除与并发更新规则。
这些属于实现设计，不能重新扩大已确认的产品范围。
可按“名称与迁移 → 回执问答闭环 → 长对话与记忆闭环”组织交付；实际 slice 依赖和验收映射留待设计评审后冻结。

## 设计阶段的基线及代码依据（历史记录）

- workspace：/Volumes/<workspace>/workspace/options-monitor
- 检出 main，HEAD：0c5ece8c5f124261a0eec47fb2edeb7d28e697ea
- 本地 origin/main：16ee6e830a3a9b89232feb70c02d4bdee6d4bdd9，当前检出落后 62 个提交。2026-09-09 本轮已 fetch，引用仍为上述值；不代表未来不会漂移。
- origin/main 已包含 6e0591de 的压缩准入修复：50% 是压缩目标，压缩后准入按 75% 硬预算。不得重复当作未修复缺陷。
- origin/main 已包含 PR #262 的成交处理结果与回执恢复；src/application/trades/{inbox,receipt}.py 已有持久化消息、结果关联和发送状态，应复用。
- agent-runtime/main.ts：Pi 会话、压缩、上下文准入；后续需以刷新后的基线重验运行中增长和中止路径。
- src/application/copilot/channel_facade.py、host.py、host_store.py：可信身份、会话串行、持久化与 Host 治理。
- 现有 Pi 会话使用独立持久化；host_store 中旧 session_memory 不是当前对话主路径，不恢复为第二份对话真源。
- src/application/copilot/tools.py、om_chat.scene.json 与 src/application/agent_tools：现有业务读取工具及结构化证据；保留工具参数和证据校验。
- src/application/notification_perception_read.py、agent_tools/notification_perception.py：当前通知观察能力不足以代替三类业务回执检索。
- docs/BOT_DESIGN.md、docs/PI_AGENT_CORE_INTEGRATION.md 是现状文档 owner。Save Design 应按 owner-first 确定设计归属与更名，不把待实现能力写成现状。
- 历史远端运行样本、时点和局限见 PRD 第 8 节。本阶段未执行新的生产调用或迁移。

保存前工作树已有 11 个无关 tracked 文档修改和未跟踪 codex/ 内容，全部保留。
本任务已新增 docs/BOT_PRD.md 和 codex/bot-devflow-handoff.md；本轮在已有 design owner 增加明确标记的待实现设计，并纠正文档中已过时的 Host session_memory 说明。没有修改业务源码或运行配置。
未来在冻结 review_base 和 Implementation 前刷新基线、明确 worktree 归属并隔离实施；不在当前脏 main 直接实施。

## 设计阶段能力检查与验证（历史记录）

- ponytail_status=used：已处于 full 模式，继续复用优先，不重复初始化。
- planreview_status=used：第 1 轮 usable/fail（2中），最小修订后第 2 轮 usable/pass-with-risks；首轮阻塞在设计层关闭。实现验证尚未执行。
- deepreview_status=used：工作流选用已安装 /Users/<user>/.agents/skills/deepreview/SKILL.md；评审 execution=pending，尚未调用，不代表已通过。
- om_doc_hygiene_status=fallback:unavailable：未发现可用技能，按仓库 owner-first 规则执行并披露。
- Panel 已取得同一快照的四份可用只读评审；dsh_crew_status=used，backend=DSH Crew hub，model=deepseek-v4-pro（最终工具元数据），independence=verified。三个原生 reviewer 的 model=unknown、independence=unverified。
- 已完成需求 walkthrough、源码/测试契约阅读、本地提交差异核查。文档保存后检查内容、引用和无关文件保留。
- Panel 与 Planreview 已完成；尚无实现测试或 Deepreview 结果。后续按 PRD 验收，不把文档检查当作功能验收。


## 实现方向批准与本轮判断

在呈现 Pi + Host + Control 方向、随后建议评估 Pi 0.84.2 → 0.85.1 兼容性后，用户回复“确认”。
该回复用于继续保存实现设计并进行 Panel，涵盖升级评估；不当作无条件修改 SDK 或执行生产迁移的授权。
产品批准原文仍只在 PRD 第 10 节，本处仅记录实现方向推进。

本轮已从 npm 获取三包声明与元数据：0.85.1 仍要求 Node >=22.19.0，但 SQLite Session API/compact 参数有破坏性变更。
推荐第一版继续 0.84.2；升级迁移作为评估结论列明后续条件，不阻断已批准四项功能。
只读来源调查发现 Daily Brief 工具已存在但未入 Scene，成交需同时覆盖 inbox 与 lifecycle outbox。

## Panel 修订批准

呈现四方报告的 P1–P14 拟议修订和保留 Pi 0.84.2 的判断后，用户回复“确认”。批准将这些修订合入同一实现设计并自动进入 Planreview；不包含 Implementation 授权。产品批准仍只引用 PRD 第 10 节。

## Workspace Isolation Check（只读）

fetch 后 origin/main 仍为 16ee6e830a3a9b89232feb70c02d4bdee6d4bdd9；6e0591de 压缩修复为其祖先，成交 inbox/receipt owner 存在。当前 main 落后 62 提交且保留 11 个无关 tracked 文档修改，不能直接实施。已读取 AGENTS/worktree 约定、status 和 worktree porcelain；现有 worktree 均无本次 Bot 范围的可复用专用环境，旧 release/其他任务不复用、不清理。

拟议 implementation_workspace：/Volumes/<workspace>/workspace/options-monitor-bot
拟议 branch：codex/bot-v1；检查时路径及分支均不存在。
review_base 固定为上述已核实提交；用户批准 Implementation 后再复核占用/基线条件并创建，当前没有创建 worktree、切分支或 stash。

仅迁入核验属于本任务的 PRD、canonical design 变更、handoff 及必要评审 artifact。design owner 在旧 HEAD 与 origin/main 之间没有上游差异；PI_AGENT_CORE_INTEGRATION.md 上游已有变化，沿用新基线，不复制旧文件覆盖。不迁入 11 项无关文档及其他 codex 内容；忽略的 review artifact 不强制入 git。

## Implementation 批准

用户在看到第2轮 Planreview pass-with-risks 和专用工作区建议后回复“确认”。授权按冻结设计执行 S1→S2→S3、validation 和自动 Deepreview；不包含提交推送、发布或生产迁移。

## Devflow Progress

- current_node: Closeout
- status: completed
- next_action: 用户已确认完成并授权提交、推送本次Bot改动；执行源码交付，不合并、不发布、不升级运行环境。
- approved_product_contract: ../docs/BOT_PRD.md 第 1、9、10 节
- panel_revision_approval: 用户“确认”，上下文见本文件“Panel 修订批准”；P1–P14 已合入
- design_doc: docs/BOT_DESIGN.md
- control_doc: codex/bot-devflow-handoff.md
- workspace: /Volumes/<workspace>/workspace/options-monitor-bot；branch=codex/bot-v1；已从冻结基线创建，只迁入本任务文件。implementation_baseline=/private/tmp/bot-implementation-baseline.json（含staged/unstaged/untracked hash和size）
- review_base: 16ee6e830a3a9b89232feb70c02d4bdee6d4bdd9（已核实、冻结的实施候选基线）
- content_version: frozen design_sha256=cdb12457eaffdd260f57ffc1e69cdba8632020d68a423a8ef9b63f39507c935f；1140 行；快照 /private/tmp/bot-design-planreview-r2.md
- planreview_rounds_used: 2
- deepreview_rounds_used: 3
- in_flight_jobs: none；三方报告及root综合裁决完成
- panel_artifact: docs/reviews/bot-design-panel-2026-09-09.md（4/4 usable，历史设计快照）
- planreview_artifact: 第1轮 docs/reviews/plan-review-20260909-202559.md（usable/fail，2中，已修订）；第2轮 docs/reviews/plan-review-20260909-203222.md（usable/pass-with-risks，两项设计层关闭，无新 material findings）
- validation: 最终6404 passed、2 skipped、1既有warning；14项门禁全exit0，/private/tmp/bot-r3-validation.json。原PRD及原工作区15项保留文件hash不变。
- open_findings: none；R1及额外活动schema、R2六项均fixed，R3完整复审无新confirmed；残余风险见最终artifact。
- blockers: none
- scope_changes: none；Pi 三包固定 0.84.2，无旧名兼容、无主动分析，四项产品合同保持

## Implementation checkpoint

- 工作区 options-monitor-bot，分支 codex/bot-v1；review_base/HEAD 为 16ee6e830a3a9b89232feb70c02d4bdee6d4bdd9。原脏 main 未改变。
- S1：完整自有名称更名，配置/启动拒绝旧名；离线迁移保留 Host/Pi 历史、备份身份、配置哈希、outbox 幂等及恢复。SDK 保留 0.84.2。
- S2：receipt_read 接入 registry/Scene 和原 owner；不主动分析。正文来源、账户权限、coverage 分开。
- S3：180 秒贯穿入口/锁/压缩/工具/提交；回答/outbox/进度 closure 同事务，异常留进度。记忆有作用域、来源、后台让行、幂等回执及删除/更正抑制。
- 首轮全仓 6302 passed、9 failed、2 skipped，日志 /private/tmp/bot-full-pytest.log。失败已分类针对性修复（包含真实循环），不能把首轮写为全绿。
- 聚焦：S2+迁移167 passed、Host/F4 203 passed、首轮失败和主链路183 passed；receipt真实读取+Host模拟Pi连续3次通过，进度原子事务连续3次通过。均无真实模型/生产/外部通知。
- Scope Guard：仅本任务文档及冻结基线迁入；测试用 .venv 和 agent-runtime/node_modules 软链不属于交付，忽略的 docs/reviews 不强制入git。
- 自动审核曾把纯源码/模拟测试识别为真实外发，使用纯源码与mock证据重新提交后通过，没有剩余审批阻塞或真实外发。
- 未执行 commit/push/merge/release/deploy/生产迁移。最终验证及Deepreview结果将追加到本owner。

## Deepreview round 1

- artifact: docs/reviews/code-review-20260909-214720.md; usable/fail，1高8中，全部accepted，按原owner修复后完整复审。
- validation: 全仓6333 passed、2 skipped、1既有warning；/private/tmp/bot-final-validation.json 保存精确命令。
- frozen_inventory: /private/tmp/bot-review-inventory.json；sha256=18f3b5975e34c74495c9001a5520299c6cc2ba64c74f1b5b20176cc1cd901e33。
- 仅修复既有四项产品合同中的缺口，无新增能力或生产授权。

## 第1轮修复及补充证据

- 四项合同内的 required-correctness/safety 修复：市场强制绑定、混合账户 batch 原文可达且不越权、迁移标记与清单一致；默认 audit 路径消费已持久 outbox；长记忆可管理预览、取消恢复和可选记忆故障隔离；持久失败进度返回已完成/未完成；同键记忆回执的一次安全重试。
- 原开放线索已确认：目录激活后压缩仍按旧工具schema估算。相同真实Node本地fixture，旧8629/11488超过75%而失败，同步schema后7415/11488并answered；阈值没有改变。证据 /private/tmp/bot-capacity-probe-tail.log，属于F3容量验收的同owner修复。
- 聚焦验证：回执/迁移191 passed，渠道75 passed，记忆/进度43 passed，Host/进度59 passed；均为临时数据库、mock或loopback，无真实provider/通知。

## Deepreview round 2

- status: completed / review-usable / fail（历史轮次，6项于R3关闭）；冻结清单 /private/tmp/bot-review-inventory-r2.json。
- 修复后精确验证命令及结果：/private/tmp/bot-r2-validation.json；全部退出0。无源码写入任务。

- round2_artifact: docs/reviews/code-review-20260909-222000.md；review-usable/fail，1高5中，6项accepted，均属四项范围内必要修复。

### Round2 remediation evidence

- 6项accepted按原owner修复：完整脱敏先于分页、三类producer市场关联、历史显式搜索、渠道超期终止回复、新SDK loop schema计量、评测旧schema拒绝。未改变四项产品/权限边界、经济裁决或发送幂等算法；新intent的payload hash包含市场，既存记录不重写。
- Root评测CLI20 passed；Pi完整196 passed（79.05s，/private/tmp/bot-pi-r2-validation.log）；memory/channel/inbound/runtime232 passed（5.43s）；新增实际SDK失败→一行同步后成功对照与源producer公共链路测试。
- 后续统一analyze/test及完整第3轮评审均已完成；结果见下节。本地证据不代表真实外部验收通过。

## Deepreview round 3

- status: completed / review-usable / pass-with-risks；相同workspace/review_base，完整当前变更，非仅修复复核。
- Round2修复后回执270 focused passed（8.65s），最终producer10 passed；全量分析/测试精确命令与结果 /private/tmp/bot-r3-validation.json，14项全exit0，pytest完整日志 /private/tmp/bot-r3-pytest.log。
- 本轮冻结inventory /private/tmp/bot-review-inventory-r3.json；原PRD未改，全部新增文件纳入，symlinks不作为交付。
- 无真实provider、外部发送、生产迁移、staging/commit/push/merge/release/deploy。

- final_artifact: docs/reviews/code-review-20260909-224450.md；三方完整报告 /private/tmp/review-host-r3.md、/private/tmp/review-receipts-r3.md、/private/tmp/review-pi-r3.md；无未关闭blocking finding。
- frozen_content_sha256: 2ac349968587a21e64418c6eac07967f27d6c4475f8073c1871e95f65d2ef787；181路径/146present/35deleted。Closeout后只修改本handoff状态，源码保持冻结版本。

## Closeout

- design_doc: docs/BOT_DESIGN.md；Planreview第2轮usable/pass-with-risks。原始PRD不为实现改写。
- implemented: F1 Bot代码/入口/配置全更名及显式离线迁移；F2提问后查询成交/定时/监控回执；F3统一180秒、动态schema容量、有限收尾/重试和持久进度；F4隔离长期记忆、异步整理、纠正/删除与写后回读。Pi保持0.84.2。
- changed owners: src/application/bot、agent_tools/receipts与原receipt/ledger/run owners、assistant/Feishu/WeChat、CLI、Pi bridge/runtime、相关tests/config/docs。完整逐路径hash清单见R3 inventory。
- validation: 6404 passed、2 skipped；ruff/graph/guardrails/Node/diff/smoke/spec/US-HK dry-run通过；本地关键场景连续3次通过。三方复审独立Host69/receipt51/Pi33项通过。
- residual owners: runtime负责真实模型质量/时延验证；channel负责实际送达；operator负责生产资源盘点/迁移；delivery负责获授权后的Git/CI/发布。详细限制与下一步见最终review artifact。
- boundaries: 未stage/commit/push/merge/release/deploy、真实模型/通知或生产迁移。dirty且未合并的专用worktree保留；本地依赖软链不交付。
- skill_status: 延续既有ponytail/planreview/deepreview；om-doc-hygiene不可用时按仓库owner-first规则fallback。
- completion: completed；用户在Closeout后回复“完成，提交并推送，不发布”。本地研发流程已完成，真实模型/生产验收限制保留。

## Source delivery authorization

用户原话：“完成，提交并推送，不发布”。授权本次已审查Bot改动的commit和push；无merge、版本号变更、tag、Release、部署或生产迁移授权。提交前已再次核对R3冻结源码hash；仅本handoff更新完成状态。依赖软链与gitignored过程评审不纳入提交。

提交门禁要求个人绝对路径使用通用占位符；交付前仅替换PRD与本handoff中的机器路径。原PRD批准段与验收段逐字不变，原始快照保留在本机临时证据中；R3原hash仍指向审阅时内容，不能视为路径脱敏后的文件hash。

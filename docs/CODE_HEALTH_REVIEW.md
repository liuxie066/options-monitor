# 合并后增量巡检

同一个每日任务依次处理合并后增量巡检、issue 价值分诊、开放 PR 的逐行审查与实际验证、本地修复，以及评论和发布交付。问题与使用反馈仍统一在 GitHub issue 跟踪，复用现有 PR、测试、过程报告和 [开发经验](../LESSONS.md)。开放 PR 阶段补充合并前检查，不新增 GitHub 强制门禁。下文描述仓库代码；本机脚本、配置及日程是否生效，以另行部署的安装回执为准，本次代码交付不修改已安装配置或运行真实队列。

## 目标与权限

成功标准：明确审过哪些提交、确认了什么、原验证为什么漏检，以及下一次如何检查。零发现也是有效结果，不以问题数量评价巡检。

巡检阶段对业务代码和外部系统保持只读；可在隔离临时目录运行无外部副作用的定向测试。用户于 2026-11-03 进一步授权：确认缺陷后，在同次调度接上独立本地修复、回归验证与独立审核。检查前阅读 [Agent Manual](../AGENTS.md)、本次相关合同和经验条目；保留无关工作区的改动。

手动报告保存在 gitignored 的 `docs/reviews/`，定时报告、修复任务和交付回执位于独立状态目录。用户于 2026-11-09 明确授权本 loop：评论实际处理过的 issue/PR；对逐行审查、实际验证与 CI 均通过的精确 PR 修订执行 squash 合并；修复合入 main 后按既有流程准备发布 PR，并在发布证据齐全后回复版本。开关和默认值见下文。新建 issue、修改标题/标签、人工关闭 issue/PR、改权限、封禁贡献者和操作生产仍需单独授权；已合并修复的来源 issue 沿用既有自动关闭路径；2026-11-14 新增授权仅允许 `not_worthwhile` 在分诊评论和关闭说明确认投递后自动关闭，具体重试与回读规则见下文。疑似敏感或恶意内容仅保留必要脱敏证据，按 [Security](../SECURITY.md) 处理。

## 每轮范围与续跑

1. 冻结本轮目标 `main` 的完整 SHA，记录源码、依赖环境和起始时间。审查期间 `main` 的新提交留到下一轮。
2. 首轮沿目标 SHA 的 first-parent 主线选择最近三个已合并 PR，按主线从旧到新审查，以最早一个 PR 的主线父提交为本轮起点。不得按 PR 编号或列表默认排序推定合并顺序。首轮起点之前的历史明确不在覆盖范围内。
3. 后续轮次先读取上一份报告，核对“已完成至”是本轮目标的祖先；续查其后的改动，每轮 first-parent 主线单元数上限由配置 `max_units` 决定（默认 10）。PR 合并和直接提交各计一个主线单元，都必须列出并检查。没有新提交时记录无增量；续跑记录缺失或主线关系不成立时，明确报告断点，不能猜测已完成位置。
4. 每个 PR 比较其合并提交与主线父提交，检查全部变更文件，并追踪相关入口及上下游一跳。再确认候选问题在本轮冻结目标是否仍然存在，避免将后续已修复的问题重新报成新缺陷。
5. 每轮按独立的提交审查和 issue 额度执行，及时记录进展；新配置建议每日总上限 15000 秒，具体公式和配置见下文。预算耗尽时保存未完成项和下一步，不能把未检查的提交记为完成；`max_units` 是数量上限，不保证每轮完成。

可用 `git log --first-parent <目标SHA>` 确认主线顺序，再用 `gh pr view <编号>` 核对 PR、变更和检查记录。PR 编号、完整 SHA、测试修订和证据路径都写入报告，不能只记录“最新 main”或“CI 全绿”。

“已完成至”表示从本轮起点连续完成了四项检查，不表示不存在缺陷或已经修复。确认问题可以转入后续跟踪；关键证据不足以完成某项检查时保留待审，不跨过该提交推进位置。

## 四项检查

| 检查项 | 要回答的问题 | 验证线索 |
|---|---|---|
| 实现与所有权 | 是否重复已有能力，或在下游补偿上游错误？ | 对照 [API Index](API_INDEX.md)，追踪真实入口、canonical owner 和相邻调用者。 |
| 测试判别力 | 是否删除或弱化断言、扩大 mock、增加 skip？测试能否暴露旧错误？ | 检查测试 diff；适配器、进程、持久化和时限问题保留相应真实边界，必要时做最小前后对照。 |
| 业务失败路径 | 缺失、异常、超时是否被当成成功？隔离和幂等约束是否保留？ | 按相关合同验证成功与失败，明确账户、市场、数据完整性和写入边界。 |
| 权限与执行能力 | workflow、依赖、安装、插件和版本变更是否新增外部执行或发布能力？ | 检查实际 diff 和当前生效证据；修改 `VERSION` 的 PR 另按 [Release Process](RELEASE_PROCESS.md) 核对发布边界。 |

结构变化和静态扫描只能提供线索；不得仅凭重复代码、函数长度或扫描告警断言缺陷或恶意行为。CI 完整执行当前测试集合，也不证明断言强度和业务边界已被覆盖。

## 候选问题与经验

先查关联 issue、修复 PR 和相关经验，区分新问题、重复问题及已修复的历史原因。先沿真实入口验证触发条件，检查下游是否已阻断；未实证的线索保留为待验证，不写成确认缺陷。

确认问题至少记录触发条件、预期和实际结果、源码位置、实际复现日志、稳定去重 key，以及明确的修复文件范围。没有实证日志的线索保留为验证缺口；例如首轮 V001 不自动变成当前业务故障。结构化确认记录先写入 `repairs/<问题标识>/task.json` 和本地 issue 草稿，再推进审查游标；同 key 复用任务，不重复开工。

同次调度默认最多推进 1 个现有待办，由 `max_repairs` 覆盖，每个任务另受 `repair_unit_timeout_seconds`（默认 600 秒）限制。同一任务每轮最多选择一次，先处理尚无尝试的任务，再按最近尝试时间从旧到新处理；有归属或文件冲突时保留并说明原因。`attempts=0` 的历史任务直接沿原 task.json 续跑，保留 ID、来源及尝试历史，不另建任务。每个合格任务沿以下步骤推进：创建冻结基础的独立本地分支/worktree；在未修改业务源码时先补回归并由 runner 记录真实断言失败；启动另一个上下文修改 owning boundary，保持该回归文件不变；由 runner 对同样用例重跑，要求失败/跳过/fixture error 为零；再启动全新只读审核上下文核对整个最终 diff、未跟踪测试、上下游、门禁和实测证据。

审核通过后生成本地 `pr-draft.md` 和 `approved-content.json`，状态为 `awaiting_delivery_authorization`；这是本地验证就绪标记，不代表远端 PR 已创建或修复已合并。runner 在同轮按交付配置继续发布；关闭发布时保留本地任务。清单绑定原 task ID、基线、分支、工作树、全部变更文件内容/模式及成功验证；验证前后和独立审核前后必须保持相同内容。遗留待交付任务缺少清单时，需要人工重新验证与审核，不能补造历史批准。审核要求修改时，下次日跑续接实现与复审；无法复现或关键证据不足时停为待裁决/blocked。`blocked`、`needs_triage` 保留失败原因和历史，不自动重置；`manual_todo` 列出所需人工动作、任务文件、证据与历次尝试。任务状态和审查游标分离，无新增 PR 时仍可继续待办；同次预算不足则保留阶段和脏分支，不删除或提交。

修复与验证拥有优先预留的 `repair_budget_seconds`（默认 1800 秒）；提交和 issue 审核不能使用这份额度，审核提前结束的时间可以留给修复。前置准备超时或失败时仍进入 repair 阶段，记录实际可推进任务或归属、输入、环境等阻塞原因。阶段被调用不等于任务已修复；没有合格任务时应明确报告 0 次尝试及原因。生产、服务、发布边界的改动以及已验证依赖输入发生变化时，保留记录并交用户单独裁决或刷新环境，不自动安装或放宽权限。原因查明后，按 `LESSONS.md` 的触发条件、错误原因、下次检查方式和证据更新原条目；相同原因合并，不复制任务流水账。被证伪的审查判断先保留反证，只有可复用且证据充分的原因才进入经验。

## 修复交付与合并授权

普通 `run` 在 `repair_enabled=true` 时，按布尔配置 `repair_delivery_enabled` 控制已验证修复的 PR 发布；缺省为 `true`，设为 `false` 时停留本地。兼容参数 `--deliver-repairs` 可以在本次调用覆盖关闭配置。发布路径核对内容清单、归属、基线与依赖环境；暂存批准文件；运行 staged guardrails（含公开面）、Ruff 与依赖图检查；创建单个修复提交；普通 push；创建或回读该分支唯一的 PR。这一级只推进到 PR，不赋予合并权限。日程原有的 `runner.py run --state-dir <state>` 参数已能读取该配置，无需为此修改 plist；部署仍由用户另行安排。

PR 正文从任务与验证记录生成，必须包含来源 issue 编号（提交巡检无来源 issue 时明确说明）、触发条件和预期/实际、实际改动文件、验证命令与结果，以及“本 PR 尚未合并；须经独立逐行审查、实际验证和宿主交付门禁”。新验证记录保存实际命令；旧任务缺少命令时使用已保存的同一 selectors 生成复查命令并明确标注，不能伪造历史命令。正文移除自动关单指令。版本与发布文件变更不走这条路径；代码不安装依赖、不重置旧 worktree、不强推、不代建 issue、不发布版本。

```bash
python scripts/code_health_review.py run \
  --state-dir "$HOME/Library/Application Support/options-monitor/code-health-review" \
  --deliver-repairs
```

已存在的待交付任务先获得至多 120 秒、且不超过 repair 剩余额度四分之一的交付时间；其后推进本地任务，再用剩余时间交付新通过的任务。同一任务本轮只交付一次；同一分支已有 PR 时复用，HEAD 不变时不重复创建 PR 或 push。CI 未完成时保存 PR 和 HEAD，立即让出本轮时间。CI 失败记录 `ci_failed`、具体检查结果和首次失败证据 `delivery.ci_failure`，计入 blocked；不会触发 CI 重跑，也不会把任务重新送进本地修复来覆盖红灯。后续运行可回读外部检查状态，首次失败证据保留。合并要求 `checks` 及当前 [Guardrails](../.github/workflows/guardrails.yml) 的八个 `regression (0)` 至 `regression (7)` 全部成功，其余已返回检查不得失败或仍运行；缺失、跳过的必需检查不算成功。

常规自动合并由下文“开放 PR 审查与实际验证”的独立阶段负责，`pull_request_merge_enabled=true` 才可在所有门禁通过后 squash 合并；本地修复者的 approve 不能替代该阶段。修复交付模块本身仍保留人工兼容入口：`--approve-repair-merge <16位任务ID>=<40位HEAD>`，须同时带 `--deliver-repairs`，仅用于当次指定 task/HEAD，使用原有 `--merge --match-head-commit` 并要求 `CLEAN`。关闭 PR 自动合并开关后，仍可由用户在 GitHub 人工合并或使用这个精确批准入口。任何路径都不使用 admin 或自动等待合并选项。`status`、`issues-backfill`、smoke、dry-run 均拒绝交付参数，也不运行新增评论/发布阶段。

合并命令成功仍不算完成。程序重新回读同仓库、同分支、base=`main`、同 HEAD 的 merged PR，再次核对上述必需 CI，记录合并 SHA 与时间，fetch 后证明修复 HEAD 属于合并提交、合并提交属于 `origin/main`，才记录任务为 `merged`。手工已合并 PR 和中断后的恢复路径同样必须满足 CI 证据，不能绕过后直接关单。只有这条有证据的修复路径可以 PATCH 关闭该任务已有的来源 issue，并再次 GET 确认；新归属出现时转人工处理。squash 后 HEAD 不再是合并提交的祖先时，须用 Git 证明合并树与已验证 HEAD 的内容完全一致；不能证明时保留人工核对。开放 PR 自动合并另要求 squash 的第一父提交等于实测 base，才把它交给发布阶段。

`delivery-commands.jsonl` 和 task 的 `delivery` 字段保存执行、PR、HEAD、合并证据及逐条关闭回读。创建命令成功返回 URL 时立即保存 `pr_created_receipt` 并计入本轮新建 PR，后续状态回读超时不抹掉创建事实。每次关闭先持久化 `close_requests`，再发送 PATCH；响应丢失时先 GET，已有请求首次确认为 closed 时计入确认关闭，已确认条目不重复计数或 PATCH。中途失败保留已完成步骤，续跑复用分支/PR。模型判断“已修复”“不能复现”的历史条目仍只输出关闭建议，不能进入这条自动关闭路径。

这些是代码入口和权限边界；开发或测试通过不表示本机已安装、日程已调整或远端交付已启用。日程使用的脚本、参数与安装回执须另行核对，本轮不修改本机配置实际值。smoke、dry-run、status 和 issues-backfill 不进入 PR 发布或合并路径，配置启用也不改变这一点。

## 开放 PR 审查与实际验证

`scripts/code_health_pull_requests.py` 使用 `gh pr list --state open` 获取完整队列，按创建时间、编号从旧到新处理。`max_pull_requests` 默认 3，`pull_request_unit_timeout_seconds` 默认 900 秒；超过本阶段预算或数量上限的 PR 写入待续清单，不消耗提交巡检或 issue 配额。loop 自己的发布 PR 交给发布专用门禁，避免通用审查绕开 release_delta 的完整范围要求。draft 和非 main 目标均保留待裁决。

每个 PR fetch 到独立 detached worktree，绑定完整 HEAD、当前 main/base 和 diff 摘要；模型须逐文件逐行读取全部 diff、原断言、owner 及上下游一跳，为每个文件和每个 hunk 返回对应摘要及逻辑分析。宿主拒绝漏文件、漏 hunk、摘要不符或空分析。明确检查无关改动、弱化断言、伪造或只覆盖 mock 的测试、只写注释却声称实现、复制错误逻辑和异常边界；按实际代码与证据判断，不以贡献者身份或工具使用情况推断质量。

模型提出受影响测试和相关回归的精确 selectors，宿主在现有无网络验证沙箱中实际执行两组 pytest，保存命令、JUnit、非空测试清单及退出码。失败、error、skip、测试前后源码变化都拒绝合并；仅 CI 绿色或模型自述通过不算验证。bug fix（模型分类、fix 标题或 bug 标签任一成立）须将相同回归测试复制到独立基础树，验证真实断言从红变绿，测试身份相同；运行时异常、依赖错误或旧环境不匹配不能冒充红灯。maintenance/nonbug 必须说明为何不适用失败基线，仍实际跑影响测试和相关回归。无法实跑则 blocked。

三种审查结论：

- `approve`：完整逐行检查和宿主实测通过、没有未解决 findings，才进入 `merge_eligible`。交付宿主重新绑定内容、HEAD/base、讨论、非 draft、可合并状态和必需 CI，然后 `gh pr merge --squash --match-head-commit <HEAD>`；回读 merged、main 祖先关系、squash 第一父和完整树后才记录 `merged`。
- `changes_required`：回复具体问题及证据，不修改对方代码，等对方更新或另行安排修复；更新 HEAD/有效讨论后重新审查。
- `decline`：回复无价值、超范围或明显有问题的原因并建议关闭。关闭始终由用户单独裁决；`pull_request_close_enabled` 默认 false，当前实现拒绝将其设为 true，避免一次开关扩大所有 PR 的关闭权限。

第三方中转/代理服务、外部 provider 站点接入或硬编码路由是硬边界：issue、修复独立审核、开放 PR 和发布全增量审核均须显式检查 `third_party_service`。命中就转产品决策；即使模型同时返回 approve、测试通过或声称低成本，宿主仍拒绝自行入队、合并或发布。通用协议能力是否值得做也需单独分析，不把某个中转站直接塞进产品。

`pull-request-state.json` 保存精确输入和结果；新 HEAD、base、有效讨论或验证环境变化会失效。相同失败输入复用报告，相同成功输入可复用实测证据，但每次合并仍重新回读身份和 CI。只有已有发送回执且作者匹配的 loop 评论才从输入中排除，用户追加的证据仍触发重审。每条报告、patch、清单、实测日志和评论草稿保存在 `pull-requests/items/<编号>/<尝试>/`；超时、半途停止不能记成已审完成。

## 修复后的发版与回复

`scripts/code_health_delivery.py` 仅对本地确实处理过的 issue/PR 评论，使用 `gh issue comment` / `gh pr comment --body-file`。默认开启 `comments_enabled`，实现用户要求的明确反馈；关闭时不写 GitHub。回复包括分诊理由或问题清单，完成时包括复现、改动、实际验证命令/结果、合并 SHA，以及已核实的版本与 Release 链接。正文不得把未执行的测试或尚未发布写成完成，也不自动公开内部日志。标题、标签、状态和权限不在评论授权内；PR 不通过不会自动关闭。

`comment-state.json` 在发送前持久化待发正文和标识。发生超时或响应丢失时，下一轮先按标识和实际发送账号 GET 回读，已存在则补收据，不重复发。评论必须绑定已复核记录或已有修复 PR/来源；仅凭远端编号不能获得评论权限。日常 issue 复核仍只取数、保存结论，由独立交付阶段发送；`issues-backfill`、status、smoke 和 dry-run 不发布评论。

`release_enabled` 默认 true，但只由已核实合入 main 的本 loop 修复/实现触发；没有合并不定时制造版本，一轮可将多个已合并修复放入同一发布任务。复用 [发布流程](RELEASE_PROCESS.md) 和现有 `.github/workflows/guardrails.yml` 的 VERSION 触发链路：

1. fetch main/tags，冻结发布目标，核实所有修复提交属于 main；完整检查上个正式 tag 到该目标的**全部提交**，包括中间其他合并。发布上下文逐个读完整 diff，为每条提交提供真实 release_notes/no_release_note 和 design_evidence，不能只按提交主题分类。
2. 默认只升 patch。保留已有 Unreleased 条目；新增功能、Breaking Changes、第三方服务接入、兼容性敏感改动或证据不足转待裁决。通过实际存在的 `version_update` 工具 preview/apply 及 freshness digest 更新 VERSION，不调用文档中尚无 CLI 实现的 `release.prepare` 等名字。
3. 准备 VERSION、带日期的 CHANGELOG 和 `scripts/release_delta.py` 生成的 coverage，逐提交填入覆盖证据；运行 canonical `release_check.py --require-current-taxonomy --require-delta-coverage`、独立发布审核、完整 `release_preflight.sh --full --allow-dirty`，只提交三个发布元数据文件，再跑 `--require-clean`。
4. 创建发布 PR，等待其必需 CI，重新确认发布 main 与已审目标一致后 squash。现有 Guardrails 在 VERSION 合入 main 后负责 tag、Release 及运行包；runner 不自建 tag，不直接 `gh release create`，不改 workflow、不部署生产。
5. 回读精确 release merge SHA 的最新 Guardrails 整轮成功、tag 剥离后的提交、正式 Release、完整非零资产和运行包 `ready` 标记。核心 Release 已有但运行包未 ready 仍未完成；跨轮保存发布 PR 和证据，不能抢先回复版本。

`releases/<批次>/task.json` 保存发布状态及来源。检查、授权、环境、预算或 CI 不足时明确回复“已合并未发版”并列待办；失败准备树保留，需人工核对后续跑，不自动 reset 或覆盖历史。已知发布 PR 可跨轮回读；发送响应丢失时保留证据交人工核对，不能猜测成功或重复制造发布 PR。评论失败单独记录，不抹掉已核实的合并/发布事实。

新增开关均需布尔值：`pull_requests_enabled=true`、`pull_request_merge_enabled=true`、`comments_enabled=true`、`release_enabled=true`，以执行本轮明确授权；`pull_request_close_enabled=false`，因为低价值或有问题的判断可能出错，关闭应逐 PR 确认。`repair_delivery_enabled` 延用默认 true。开启这些配置不代表本机已部署，更不授权生产服务、配置、交易、通知或账本写入。

## 每轮报告

每日收尾同时写 `daily-summary.json` 和人读 `report.md`：分别计数本地验证通过、新建 PR、核实合并修复、等待合并批准、CI 等待、CI 失败和 blocked。CI 状态与等待批准可重叠，CI 失败和交付 blocked 都计入阻塞。报告逐项列出新建或复用的 PR 编号、链接与 HEAD，以及 blocked 任务和具体原因；已发布 PR 不再计为本地待交付。预算不足或发布配置关闭时，仍将任务中保存的 PR、等待批准和 CI/阻塞状态计入清单，标明“本轮未刷新”与本地记录时间，不能冒充本轮 GitHub 回读。开 PR 和本地验证通过都不等于修复已合入 main。

建议文件名为 `docs/reviews/code-health-review-<YYYYMMDD-HHMMSS>.md`。报告至少包括：

- 范围：起始 SHA（不含）到目标 SHA（含）、有序 PR/直接提交、冻结时间、源码和测试环境。
- 检查记录：逐 PR 列出四项检查结论、所用证据，以及未运行或未覆盖的内容。
- 确认问题：触发、预期/实际、源码和复现证据；无发现时明确写零。
- 验证缺口：缺什么、影响什么结论、下一步如何补；待验证不冒充已确认。
- 漏检原因与经验：旧验证为什么没挡住，新证据覆盖了哪个边界，是否已有经验可复用。
- 后续动作：已有 issue/PR，或可直接评审的 issue 草稿；没有新问题时无需制造 issue。
- 续跑：连续“已完成至”SHA、待审项、下一轮起点；如果预算耗尽，记录实际停止处。卡点须逐条列出具体 commit SHA、连续失败次数、失败类型（超时 / 未完成 / 模型无输出）、是否已升级 `needs_human`，并给出 review / issues / repair 各阶段耗时、预算与剩余时间。

报告同时承担续跑记录。当前报告只在本地，跨设备或换工作区前需要显式交接报告；找不到时不得宣称已延续覆盖。

## 首版验收

首次手动执行后，检查以下条件：

1. 范围冻结、PR 顺序和检查记录可追溯，未完成项可续跑，原工作区未被覆盖。
2. 每条确认问题有证据，验证缺口与未运行项明确，没有以问题数量充当完成标准。
3. 用 `LESSONS.md` 的 L001 演练真实 gateway 与推进时钟的检查要求；已修复案例不重复报题。测试可在隔离环境复核，不访问真实供应商。
4. 文档入口、链接、格式和适用 guardrails 通过，报告未被强行跟踪。

用户于 2026-11-03 在首轮通过后授权每天运行一轮，后续两轮由持久调度试运行，再复核覆盖、误报与续跑。反复出现且可确定判断的规则优先进入现有测试或 guardrails；不增加 CI、贡献者评分或黑名单。

## Issue 与使用反馈处理

GitHub issue 是已有问题与使用反馈的统一持久待办。对话或其他渠道可以提供反馈；先查重，相同故障补充到原 issue，新问题才建 issue，不另建反馈队列。普通答疑可以直接解释；需要继续复核、产品裁决或研发处理的有效反馈应留下 issue。本 loop 对自己处理过的 issue/PR 的评论已获明确持续授权，按 `comments_enabled` 控制；新建 issue、修改元数据或人工关闭仍需另行授权。缺少相应权限时保留正文草稿和待办，本地处理可以继续。

反馈至少记录来源、使用版本或提交、触发步骤、预期与实际、已有证据和缺失信息。用户未提供的事实保持未知；故障以“用户报告，待复核”记录，issue 的存在不代表缺陷成立。体验建议记录希望改善的行为，策略偏好记录待决事项，不强行包装成 bug。诊断附件只采集必要的脱敏内容，内部日志与完整附件不自动公开。

手动复核与每日任务按以下顺序进行，分别遵守对应入口的预算；预算不足时保存待续位置，不为完成数量扩大范围：

1. 冻结当前 `main` SHA 和 issue 讨论快照，先检查 assignee、最新讨论、关联开放或已合并 PR，以及本机正在处理该问题的任务。没有 assignee 不等于无人处理；范围重叠时复用已有工作或保留待办。
2. 每日默认最多复核 5 条 issue，手动补扫按所选清单逐条进行，读取完整讨论和关联实现，追踪真实入口及上下游一跳。在冻结版本上分别记录“仍可复现”“已修复或部分修复”“证据不足或未复现”“需要产品或策略裁决”。后续评论可以纠正原报告，不能只按标题开修。
3. 最多推进1条已确认、范围清楚且获授权的问题；明确当前剩余问题、owning boundary、文件范围和回归条件。沿现有本地复现、修复、验证与独立审核步骤处理；低影响的文档修正核对事实来源、链接和门禁即可，不为单行措辞新增镜像测试。没有合格问题时零修复有效。
4. 在 gitignored 的 `docs/reviews/` 保留本轮报告、命令退出码、必要反证和验证证据。报告按 issue 链接列出冻结版本、结论、已改与未改范围、证据位置、阻塞与下一步；它是执行证据，不作为第二套待办。issue 更新区分已完成和残留，交由日常评论阶段按已授权范围发送；人工关闭仍需另行明确授权。
5. 有符合条件的真实使用反馈时，将其整理为新 issue 或原 issue 的补充，获授权后写入，再按同一流程复核。没有新反馈就明确记录“反馈建单未验收”，不能编造故障来演示。完成本地改动与审核后按上述已授权门禁交付 PR、核实合并，再由独立发布阶段处理发版和结果回复；不扩大生产操作权限。

处理进度与合并后审查游标分开。issue 中的历史问题或使用反馈不属于本轮新增提交时，不改写 `completed_commits`，也不推进定时任务的覆盖游标。用户于 2026-11-03 授权将 issue 处理接入现有每日任务：`scripts/code_health_issues.py` 负责独立取数、复核及准入，复用 `scripts/code_health_repair.py`；不改变线上 Bot 权限。

每日通过只读 GitHub API 分页读取全部开放 issue 摘要，默认选 5 条，由 `max_issues` 覆盖，不再存在三条硬上限。先排除已完成的 `(issue编号, target SHA, input_digest)`；身份历史保存在 `issue-state.json`，输入 A→B→A 也不会重复调用。版本 3 输入指纹包含该 issue 的有效字段、非 loop 自发评论及关联 PR 摘要；忽略仅更新时间/评论计数和已有回执绑定的自发评论，避免回复使同一 issue 无限重审。本地 repair 状态和无关开放 PR 不使同一身份失效。剩余候选按 `(bool(reviewed_at), last_attempted_at 或 reviewed_at 或空串, issue编号)` 排序，复核时间跨 target 保留。有活跃 repair 的 issue 不享受优先权；持续超时的条目保留尝试时间，其他未尝试条目先行。固定 64 条、每日实际完成 5 条时，首轮需 13 次调度；这不等于全部修复或关闭。

身份规划在两种模式下均分页读取评论和 timeline，从本仓库 cross-reference、正文/评论 PR 链接，以及开放 PR 正文/标题中的 issue 引用构建关联集合。PR 摘要按编号排序，绑定标题、正文、状态、更新时间、HEAD 和合并信息，覆盖仍开放和已关闭的关联 PR；无关 PR 新建、编辑或合并不改该 issue 的指纹。`hydrate=False` 省去完整复核上下文，不省略这些身份读取，因此与完整 plan 的 digest 一致；代价是规划阶段也需要讨论和关联 PR 元数据请求，不依赖可能过期的本地关联缓存。全局开放 PR 的文件列表继续用于范围占用检查，独立于指纹，未链接但文件重叠的 PR 仍阻止准入。单条关联读取失败或请求超时而总预算尚足时，仅使该条身份不可用；总预算耗尽则停止规划。缺失信息不当成空关联集合。版本 3 与旧指纹不兼容，且复用结论必须含明确 triage：旧确认记录须重新复核，不能只改哈希后认可原结论；本次代码交付不迁移安装目录里的状态数据。

选中的条目在调用模型前再次获取完整评论、timeline 和关联 PR，核对规划期间的 issue/关联 PR 是否变化，再独立调用、验证及落盘。`issues/items/<编号>/` 保存该条 plan、原始结果、报告、证据或 `pending.json`；完成后立即更新状态和累计汇总。单条超时、取数失败或无效结果只使该条待续，不能抹掉其他条目的结果；未完成项不写已完成身份。提供有效阻塞说明的结论归入 `needs_decision`，没有返回有效结论的尝试仍待续。

模型返回 `confirmed`、`already_fixed`、`not_reproduced`、`needs_decision`、`blocked`、`duplicate`、`in_progress` 或 `not_worthwhile`，另必须提供 triage 的 decision、reason 和 third_party_service。先判断是否值得做：项目职责内、收益高于维护成本、没有未决产品/策略选择才是 `worthwhile`；超范围、重复、非本项目职责或收益不足为 `not_worthwhile`，保存理由并走 `reply`。用户于 2026-11-14 授权仅对 `not_worthwhile` 自动关闭：独立交付阶段先确认分诊评论成功，再投递引用其链接的关闭说明，以 `not planned` 关闭并 GET 回读确认；`github_closed` 记录确认结果。评论失败时保持开放并保存失败原因；关闭失败或响应丢失时，下一轮沿 `comment-state.json` 的 marker 与关闭收据续办，不要求重新复核，也不重复评论或重复关闭。需要用户选择为 `needs_decision`；`needs_decision`、`blocked` 和第三方服务接入保持开放。第三方中转/provider 接入无论同时报告什么技术结论均强制待裁决。只有 `worthwhile`、third_party_service=false 且冻结版本上的实测证据、明确 owner 与文件范围齐备时才入修复队列；已有 assignee、关联开放 PR 或与开放 PR 文件重叠时记录 `in_progress`，不接管。稳定故障 key 在两种来源间复用同一任务；不同 key 的故障分别保留完整发现与验收范围，文件相交不能证明是同一问题。两种来源均在执行时检查本地文件冲突：与更早的待修或待交付工作重叠时暂停后来的任务，保留各自证据和待续资格，交人工解决冲突。issue 来源的待续任务需核对每个仍开放的来源：归属未变、输入指纹未变、先前复核仍为 confirmed、triage=worthwhile 且没有第三方服务接入，且 `latest.repair_task` 仍是当前任务 ID。同一 finding key 的重建或复用产生同一 ID；不同 key 是不同任务，不能因共享 issue 就绕过 ID 守卫。仅验证目标过期时，repair 阶段使用自己的剩余预算单独复核，不占每日 5 条；当前身份已有结论时复用。复核后仍须 confirmed 且验证目标相同才可恢复。尚无工作树、未开始实现的任务可记录重验证并更新基线；已有旧基线工作树一律保留，明确等待人工迁移，不自动 reset/rebase。输入变化时暂停续修，等待正常复核。一个故障关联多条 issue 时，不要求所有来源同日占用日常配额。新反证、已修结论、取数失败或变化尚未复核均保留工作区并暂停。来源不在开放列表时另作 GET 核对，只有明确返回 `closed` 才算已关闭；缺失、错误响应或权限失败都不等于关闭。所有来源均为已关闭 issue 的任务，在本地调度/交付前转为 `source_closed` 终态，保存原状态、关闭来源、时间、finding、attempts、证据及工作树；不占修复名额，不参与文件范围冲突，不删除数据或关闭远端 PR/issue。混合来源仅排除已确认关闭的 issue，其余开放来源仍逐项严格核对；有独立提交巡检来源时不会因 issue 关闭自动终止。报告单列 `source_closed` 数量。后续出现新确认时保留原工作并进入 `needs_triage`，不直接恢复旧实现。来源 issue、验证 SHA 和新增证据另记，不伪称发现来自本轮新增提交。

反馈文字准确区分“已复现”“本地修复完成”“已合并”“已发布”和“运行版本已验证”，绑定对应提交、版本与证据。合并到 `main` 不等于用户运行版本已修复；新建 issue 不代表承诺修改策略或发布。使用反馈的手动验收要求至少有一条真实反馈完成查重、建单或补充、复核与明确下一步；缺少输入时，只确认规则落地及历史 issue 处理结果。

## 历史 issue 的处置与人工关闭

复核有四种本地出口：值得做的当前实证缺陷转 `repair`，按稳定 key 新建或复用任务；不值得做为 `reply`，写明理由，日常交付确认评论后自动关闭；已修复或失效且证据齐备时为 `close_recommended`；产品待决、无法判定、归属其他工作或关闭证据不足时为 `needs_decision`。模型的 `already_fixed`、`not_reproduced`、`duplicate` 本身不能证明可关闭。阻塞项、未执行的测试和通用测试通过不能推断原问题已消失。

每条关闭建议记录 issue 编号、冻结 SHA、输入指纹、关闭理由、可选的修复提交、实际验证日志及命令与整数退出码。日志必须在本条 work 内、非空，使用 JSON Lines 逐条记录 `command`、整数 `exit_code` 和实际 `output`。每条建议中的命令必须与自身日志记录对应，匹配记录的退出码均为 0；其他命令的成功码不能掩盖它的失败。提供修复提交时还校验完整 SHA 是冻结目标的祖先。结构性检查不替代人工判断日志是否覆盖原缺陷；不合格证据降为 `needs_decision`。

每轮 `issues/summary.json` 和 `issues/report.md` 汇总五类计数与清单：建议关闭、转 repair、reply、needs_decision、超时或其他待续（附具体原因）。`issues/close-recommendations.json` 按最多 10 条一批输出关闭建议，供用户明确整批确认。复核阶段及补扫不关闭 issue，日常评论和 `not_worthwhile` 的自动关闭由后续交付阶段独立执行；交付后将确认关闭的编号回写 `github_closed`、issue 回执和上述报告，评论或关闭失败时记录错误并留待重试。补扫仍不发布评论或关闭 issue。建议关闭不表示 GitHub 已关闭。这些建议仍须由人明确确认并另行关闭；本地入队也不表示已经修复。上文已验证修复合并后的自动关闭独立记录在 repair 交付回执中，不消费或冒用关闭建议。

## 手动全量补扫

补扫复用相同状态目录和互斥锁，冻结开始时的全部 open issue，不受日常 `max_issues` 限制。它不运行提交审查和 repair 实现，不推进 `state.json`，不修改 `config.json`、`installation.json`、每日 `last-run.json` 或 launchd 日程。

```bash
python scripts/code_health_review.py issues-backfill \
  --state-dir "$HOME/Library/Application Support/options-monitor/code-health-review"
```

每条仍受 `issue_unit_timeout_seconds`（默认 600 秒）限制；手动全量扫描按条目数分配总时间，不套用每日总预算上限。64 条全部用满时，仅单条预算合计约 10 小时 40 分钟，准备阶段另限 600 秒。已有同身份结论复用，不再次调用模型。每条落盘并打印进度，每处理 10 条及末尾打印累计清单，关闭建议也按 10 条分组；结束写入 `runs/issues-backfill-<时间>/receipt.json` 和 `last-backfill.json`。有待续项时 CLI 返回 2；全部得出本地处置时返回 0，仍不意味着 GitHub 已关闭或 repair 已完成。

```bash
python scripts/code_health_review.py issues-backfill \
  --state-dir "$HOME/Library/Application Support/options-monitor/code-health-review" \
  --resume-run "$HOME/Library/Application Support/options-monitor/code-health-review/runs/issues-backfill-<时间>"
```

续跑保留原冻结 SHA、issue 清单及源码，在新目录写结果，复用已完成身份，仅重试未完成项。它刷新未完成条目的 issue 元数据、开放 PR 快照与输入指纹，新增评论、正文修改或指派变化不会使旧任务永久卡住。条目在这次取数期间再次变化时保留待续，下次续跑再刷新；不带 `--resume-run` 则冻结新的 main 和完整开放清单。每日任务和补扫不能同时持锁运行；锁冲突明确报错，不改变游标。

## 每日本地调度

执行入口为 `scripts/code_health_review.py`，macOS 使用同一个用户级 launchd，每天本地时间 09:17 运行一轮。首次游标来自已完成首轮的完整 SHA；每轮先冻结远端 main，按配置 `max_units` 限制处理的 first-parent 主线单元数（默认 10，PR 合并或直接提交各计一个），并默认复核 5 条 issue（由 `max_issues` 覆盖）。没有增量时仍可处理 issue 和修复待办；两种输入均无待办时不调用模型。两种发现共用一个修复队列，每轮默认最多推进 1 个本地任务，不增加第二个日程。

安装状态、规则副本、独立 Git mirror、游标与报告位于本机用户目录的 `Library/Application Support/options-monitor/code-health-review/`；任务工作区不作为调度运行依赖。每轮持久保存 `plan.json`、patch、报告、结构化结果及执行回执。`state.json` 保存连续已决策提交位置，以及 `review_unit_failures`（逐提交连续失败次数与诊断）、`review_needs_human`（已升级并跳过的提交）、`reviewed_units`（缺口之后已完成、下轮可直接复用的单元）；`issue-state.json` 保存各 issue 的尝试时间、已完成身份历史、输入指纹、验证 SHA、处置及报告/修复任务位置，作为 GitHub 待办的本地执行进度；`last-run.json` 保存最近执行状态；runner 每次成功运行后同时刷新已有 `installation.json` 的 `next_calendar_trigger`，记录完成时刻之后的下一个系统本地时间 09:17。定时巡检报告位于 `runs/<运行编号>/report.md`，issue 的完整快照、逐条报告与评论草稿位于同轮 `issues/`。评论草稿与实际发送回执分开保存；只有部署了新交付阶段且开关启用时才自动发送，草稿存在不代表已回复。部署须将同一修订的 runner.py 及 code_health_issues.py、code_health_repair.py、code_health_pull_requests.py、code_health_delivery.py 一起安装，并同步 policy.md；不能只替换旧的三个模块。本次不修改安装目录或其配置、队列。

runner 先获取只读 GitHub 元数据和冻结源码，再用已有订阅登录的 Codex CLI 按 first-parent 顺序逐提交调用审查。Agent 不加载个人 CLI 配置或 MCP，模型网关和目录参数由本地任务配置明确指定；巡检和审核上下文只可在阶段 `work/` 写临时文件，复现/实现上下文额外只允许写独立修复树，均禁止网络和交互提权。外层 runner 负责写报告与游标。修复回归使用无网络验证沙箱与独立依赖环境，日志和 JUnit 由 runner 回读。运行锁防止手动与定时并发更新状态。

每片只接收一个主线单元、上一轮报告、历史已确认 key 及本轮先前已确认的 key；源码和冻结目标共用，候选仍须在冻结目标验证。分片沿用原结果 schema 和 finding 校验，不降低报告、证据及范围要求；提示写明实际硬超时，并要求预留最后 `min(60秒, 本片预算的四分之一)` 停止新探针、生成结果。每片产物位于 `runs/<运行编号>/work/shards/<序号>-<SHA>/`，包括 `plan.json`、`result.json`、`report.md`、`receipt.json`、模型执行日志、超时诊断 `agent-diagnostics.json` 及片内 `work/` 中的复现证据。runner 先验证并持久化 finding 与证据，再合成顶层 `result.json`、`report.md` 和回执，最后保存 `state.json` 的连续已决策前缀。相同 key 只关联原任务的来源及证据，汇总 findings 不重复；历史 key 同时从 state 与修复记录读取。最终仍输出现有完整报告形状，证据路径相对于本轮顶层 `work/`。

某片超时或返回未完成时，记录该单元的待续状态、连续失败次数与诊断后继续下一片；只有 review 剩余预算不足 30 秒才停止。单个提交不再拖垮整轮：已完成但位置在缺口之后的单元同样计入本轮完成清单并记录到 `state.json`，下一轮直接复用，但游标只推进到“已决策”的连续前缀（完成、上一轮已完成或已升级 `needs_human`）为止。同一提交连续 `review_max_unit_failures`（缺省 2）轮超时或返回未完成时升级为 `needs_human`：写入 `state.json` 的 `review_needs_human`，后续队列跳过，并在每轮报告中单独列出；N=2 的取舍是两次独立整片尝试仍不能完成，说明该提交在当前 600 秒单元预算下不可审，继续重试只会每天重复烧掉 10 分钟。格式非法或证据缺失仍按原规则拒绝，不越过该片。模型完全无输出（`agent.jsonl` 只有 `thread.started` / `turn.started`，且没有其它事件）单独归类为 `agent_no_output`，与“任务太慢”区分：它不增加该提交的失败次数；无论发生在 review、issue、PR 还是 repair 阶段（含 repair 准入时为刷新过期结论而执行的嵌套 issue 复核），该阶段立即停止并记录 `agent_unavailable`，后续阶段（review 的后续提交、issue 的其余条目、PR 的其余条目、repair 的后续任务、交付）记录为 `skipped` 或 `not_attempted` 且不再调用模型，避免模型网关故障把整轮预算耗成零产出。PR 阶段当前条目记 `pending` 并写入诊断，剩余 PR 列入 pending 后中止；repair 阶段嵌套复核无输出时当前任务保持 pending、队列其余任务列入 deferred 且不再执行本地修复或交付；`daily-summary.agent_unavailable` 会指向该阶段。超时单元的回执与 `agent-diagnostics.json` 保存实际事件数、事件类型、最后事件、距最后事件秒数、stderr 尾部与耗时。issue 快照和复核在 review 之后使用独立预算，issue 结果不参与提交游标；issue 报告及合格修复记录先持久化，再写 issue 进度。修复阶段使用其保底额度及审核节省的剩余时间，不承诺单轮完成。`daily-summary.json` 分别列出已审提交/issue、入队任务、实际尝试、本地验证、跟进的 PR、本轮新建的 `prs_created`、本轮首次核实的 `merged`、本轮关闭回读确认的 `closed`，以及剩余 pending/blocked/待裁决、卡点与各阶段耗时。`merged` 保存首次核实的任务 ID，实际合并时间与提交见任务的 `delivery.merge_proof`；跨轮继续关闭不重复累计合并。不得将入队、本地验证或 PR 创建计为已合并修复。人工待办保留原 blocked 原因、证据及继续所需动作，不通过重置 pending 消除阻塞。已合并任务不再占用后续任务的文件冲突门禁。

预算公式：每日总上限 `D = timeout_seconds`（新缺省 15000 秒），启动时刻 `t0`，总截止 `E = t0 + D`。原有分配保持：启用 repair 时 `P = min(D, repair_budget_seconds)`（缺省 1800 秒，至少 30 秒），否则 0；启用 issue 时 `I = min(D - P, issue_timeout_seconds)`（缺省 3600 秒），否则 0；提交审查 `R = min(D - P - I, review_budget_seconds)`（缺省 6600 秒）。新增 PR 分配 `Q = pull_request_budget_seconds`（缺省 1800 秒，关闭 PR 阶段时为 0）；评论或发布任一开启时 `L = delivery_budget_seconds`（缺省 1200 秒），否则 0。必须满足 `R + I + P + Q + L <= D`，不满足直接报告配置容量不足，不静默压缩原 R/I/P。新默认全开启时为 `15000 = 6600 + 3600 + 1800 + 1800 + 1200`。旧 12000 秒配置若已占满原额度，部署时须显式扩总预算、重配额度或关闭新阶段；本次不改本机实际值。smoke 的 I/P/Q/L 都为 0。额度是上限而不是必须用满：某阶段提前结束的剩余时间按顺序转给下一阶段，但任何阶段都不能突破后面阶段保底额度对应的绝对截止（见下）。

提交逐片只使用 `review_unit_timeout_seconds`（缺省 600 秒，最低 30 秒），调用超时为该上限与 review 剩余时间的较小值；剩余不足 30 秒不启动下一片。同一提交连续 `review_max_unit_failures`（缺省 2，最小 2）轮超时或未完成才升级 `needs_human`，模型无输出不计入该次数。准备源码和提交元数据计入 review 额度；提交元数据超时保存待续范围，继续 issue。提交额度为 0 时，共用准备先计入可用 issue 额度；两种审核额度均为 0 时，准备和现有任务预检使用 repair 自身额度，提交模型不启动。旧键 `review_timeout_seconds` 完全不控制逐提交超时，与 `review_unit_timeout_seconds` 同时存在时也不生效。

阶段顺序为 review → issues → pull_requests → repair → delivery。review 截止 `Er = t0 + R`；issue 阶段实际开始时刻 `ti`，截止 `Ei = min(E - P - Q - L, ti + I + max(0, Er - ti))`，即 issue 自己的 `I` 加上 review 提前结束留下的未用额度，取数、逐条复核及证据校验共用该截止，每条另受 `issue_unit_timeout_seconds`（缺省 600 秒）限制；PR 阶段实际开始时刻 `tq`，截止 `Eq = min(E - P - L, tq + Q + max(0, Ei - tq))`，吸收 issue 的剩余额度（issue 阶段关闭时 `Ei` 不存在，review 的剩余额度直接计入 PR），每条另受 900 秒上限；repair 截止 `Ep = E - L`，自然吸收此前所有未用时间（其保底 `P` 已由 `E - P - ...` 的绝对截止保护）；交付开始时刻 `tl`，截止 `El = min(E, tl + L)`。任何阶段都不能借用 repair 保底、PR 或交付的预留额度；PR 超时不能借 issue、修复保底或发布额度。建议 `max_units=10`、`max_issues=5`、`max_pull_requests=3`、`max_repairs=1`。发布任务未完成时继续审查其他 PR，但暂停本 loop 的其他 PR 自动合并，避免先推进 main 使待发布覆盖失效。数量均是上限，超时不记成完成；执行失败不影响已持久化的其他阶段。`--smoke` 继续只受总预算剩余时间限制。

发现中的 `evidence_log` 可以是阶段 `work/` 内的相对路径，或规范化后仍位于该目录内的绝对路径。绝对路径的原始组件含 `..` 或 `.git` 时拒绝；解析路径与 `work/` 的 symlink 后，必须能通过路径级 `relative_to(work)` 检查，转换结果还须满足原有相对路径校验（无 `..`、`.git`）。目录外路径、同前缀兄弟目录和通过 symlink 逃逸的路径仍拒绝。归一化后的 finding、持久化结构化结果与修复任务均保存相对 `work/` 的 `evidence_log`；入队仍要求该证据为目录内实际文件。`owner_path`、`fix_scope` 继续只允许原有规则下的相对路径。

状态与维护命令使用本机实际路径替换占位符：

```bash
<python> <状态目录>/runner.py status --state-dir <状态目录>
<python> <状态目录>/runner.py run --state-dir <状态目录> --dry-run
launchctl print gui/$(id -u)/local.options-monitor.code-health-review
launchctl kickstart gui/$(id -u)/local.options-monitor.code-health-review
```

`--dry-run` 只取数规划，包括由 `max_issues` 限制的 issue 摘要及开放 PR 范围，不调用模型、不改两种进度；完整讨论在逐条执行时获取。`--smoke` 仅调用模型读规则和源码验证结构化结果，不处理 issue、不审查增量、不推进游标。状态入口同时显示 issue 进度；回执中 `issues.status` 单独表示 issue 阶段的完成、待续或失败，不能用外层进程成功推断 issue 已完成。暂停任务使用 `launchctl bootout gui/$(id -u) <plist路径>`，恢复使用 `launchctl bootstrap gui/$(id -u) <plist路径>`；操作调度或删除状态仍需要相应用户授权。不要删除唯一游标及报告副本。

任务跨本次聊天会话继续，但不是云端常驻：依赖该 macOS 用户已登录、机器可运行、GitHub认证和本地模型网关可用。睡眠错过的 calendar 触发由 launchd 在唤醒后合并补跑；关机或退出登录时不运行，也不承诺补齐每天一次。失败保留回执，等下次日跑或显式手动重试；不自动修改权限、切换模型或高频重试。

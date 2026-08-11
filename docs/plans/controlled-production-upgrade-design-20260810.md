# Controlled Production Upgrade Design

> 日期：2026-08-10
> 状态：Proposed
> 分支：`design/controlled-auto-upgrade`
> 基线：`main@b07b4aad` / VERSION `1.13.7`
> 本文范围：设计与实施切片；不修改远端配置、systemd、运行版本或业务数据

## 1. 决策

生产环境不再允许 timer 直接执行 `update apply --auto --confirm`。

保留现有升级器的 release materialize、venv 准备、配置 staging、symlink 切换、service drift、即时健康检查和失败补偿能力；重做它前面的目标选择与授权，以及后面的完整验证。

目标流程固定为：

```text
正式 Release
  -> 只读发现并冻结目标身份
  -> 生成 target-bound upgrade plan
  -> 人工确认同一份 plan
  -> 准备 release/runtime/config
  -> 短临界区激活
  -> 无通知验证
  -> active | active_degraded | rolled_back | blocked
```

最终默认状态不保留定时升级 service/timer。为了让存量 `auto_upgrade.enabled=true` profile 能安全迁移，首个过渡 release 先把现有同名 unit 改成 **check-only**；完成受控迁移后，再显式退休这两个 unit。

## 2. 当前事实与问题

### 2.1 已有可靠能力

当前 `service_upgrade()` 已具备以下可复用能力：

- 使用独立 upgrade lock，阻止两个升级同时执行；
- 从 git cache materialize release，不直接在 active tree 上 pull；
- 在切换前创建/复用目标 venv，并执行 release metadata 和 Agent spec 检查；
- 从 YAML authoring source 构建、校验并 staging US/HK/assistant runtime config；
- 原子切换 `current` symlink；
- 从当前 release reconcile systemd unit/profile；
- 重启长期服务并检查基本 service/channel health；
- 切换后失败时恢复 symlink、runtime config、service 定义和服务状态；
- 保留 prior release 作为显式 rollback 目标。

本设计不新建第二套安装器或回滚器。

### 2.2 当前控制缺陷

远端当前 unit 的核心命令是：

```text
om update apply --repo-root ... --runtime-root ... --auto --confirm
```

已确认的缺陷：

1. `auto` 仅出现在状态字段中，没有独立的 eligibility、approval 或 verification policy。
2. 没有 `--target-version`；每次运行重新选择最高 SemVer tag，相当于永久预授权未来所有同 major 版本。
3. target parser 接受 prerelease；从 `1.13.7` 看，`v1.14.0-rc.1` 可以成为更高的同-major 目标。
4. 只观察 git tag，不证明 GitHub Release 已正式 published、非 draft、非 prerelease，也不冻结 tag object/commit identity。
5. timer 使用 `Persistent=true`；错过 06:10 后可在任意恢复时间补跑并重启服务。
6. unit 未传 `--preserve-activation-state`；升级 reconcile 可以恢复人工暂停的 timer。
7. 升级成功判定主要覆盖长期服务的 `is-active/is-enabled` 和 channel check，不证明 scheduled jobs、OpenD 业务请求、projection 或 scheduler 全部健康。
8. 新 release 会用新默认值重建 runtime config；无人审核的升级因此不仅切换代码，也可能改变生成后的生产行为。

2026-08-10 的远端证据没有显示自动切换事故：最近一次 timer 运行只判断 `1.12.1` 已是最新版本；当前 `1.13.7` 来自人工受控升级。但下一次出现更高 tag 时，上述路径会真实执行。

## 3. 目标与非目标

### 3.1 目标

1. 发布与生产升级保持两个独立授权边界。
2. 每次确认绑定精确 target version、tag object SHA、commit SHA 和 plan digest。
3. 默认只允许正式稳定 GitHub Release；prerelease、draft、缺失 Release 一律 fail closed。
4. confirmed apply 不得再次按“最新版本”漂移选目标。
5. 升级默认保留人工暂停、disabled 或 masked 的 timer 状态。
6. activation 不与已运行的 scheduled oneshot 发生切换竞争。
7. 升级结果明确区分“切换成功”“即时运行验证成功”和“等待自然调度观察”，不把 service active 冒充业务健康。
8. 失败补偿和显式 rollback 都不能重新启用已退休的自动 apply timer。
9. CLI 与 Feishu/WeChat 入站升级复用同一个 plan/identity contract。
10. 生产验证不发送测试通知，不运行会写持仓、交易、broker 或 Feishu 业务数据的 canary。

### 3.2 非目标

- 不建设通用部署平台、GitOps controller 或新的常驻 daemon。
- 不让 GitHub Actions直接连接生产环境。
- 不实现无人值守 patch/minor 自动发布或自动激活。
- 不改变 VERSION 驱动的 GitHub Release workflow。
- 不把所有 systemd job 重写为新的 scheduler。
- 不自动清理历史 releases/cache；清理保持独立确认。
- 不在本 work unit 修改通知、持仓、trade event 或 broker-facing 数据。

## 4. 必须保持的不变式

### 4.1 Release identity

- `VERSION == v<version> == tag commit 中的 VERSION`。
- target 必须是 core SemVer：`X.Y.Z`；自动发现不接受 `-rc`、`-beta` 等 prerelease。
- GitHub Release 必须存在，且 `draft=false`、`prerelease=false`、`tag_name` 精确匹配。
- remote tag object SHA 和 peeled commit SHA 必须在 preview 与 apply 间保持不变。
- materialize 必须使用冻结的 commit SHA；tag name 只用于展示和交叉校验。

### 4.2 Authorization

- `release published` 不等于 `production upgrade approved`。
- confirmed apply 必须显式提供 target 和 expected plan digest。
- `--confirm`、`--yes` 或入站“确认升级”只确认当前 preview；不能成为未来版本的通配授权。
- preview 过期、payload/signature/digest 不一致或 target identity 漂移时不得切换。

### 4.3 Activation

- 所有耗时的下载、venv、release check、spec 和 config staging 都发生在 symlink 切换前。
- 切换前失败不修改 current symlink、live runtime config 或 service 状态。
- 默认 activation policy 是 `preserve-existing`，不是 `ensure-active`。
- activation 期间不得与已运行的 managed oneshot 交叉切换；无法取得独占 gate 时在切换前终止。

### 4.4 Verification truthfulness

- symlink/version/config/service transition 成功只证明 control plane 成功。
- scheduled job 必须等下一次自然执行后才能证明对应业务路径健康。
- 既有失败与升级后新增失败分开记录；既有失败不能被描述为本次升级造成，也不能被总体 `ok` 隐藏。
- 任何验证都不得以发送真实通知、重跑真实交易接入或写业务账本作为探针。

## 5. 目标身份与 Upgrade Plan

### 5.1 `UpgradeTargetIdentity`

在 `release_target.py` 中增加 upgrade 专用的稳定 release resolver；不要改变需要支持 prerelease 的通用版本比较 facade。

```json
{
  "schema_version": "upgrade_target_identity.v1",
  "version": "1.13.8",
  "tag": "v1.13.8",
  "remote_tag_object_sha": "<sha>",
  "remote_commit_sha": "<sha>",
  "release": {
    "provider": "github",
    "repository": "owner/options-monitor",
    "release_id": 123,
    "draft": false,
    "prerelease": false,
    "published_at": "<utc>"
  }
}
```

规则：

- 自动发现只枚举 `vMAJOR.MINOR.PATCH` stable refs；复用已有 `RemoteStableTagIdentity`，不使用接受 prerelease 的 `parse_release_tags()`。
- GitHub Release 查询失败、release 缺失或身份不一致时返回 typed blocker，不回退为“tag 即发布”。
- 显式 prerelease 只允许未来单独设计的非生产 canary；本方案生产 apply 不提供 override。
- major 继续默认阻断；即使显式 `--allow-major`，仍必须走 target-bound preview/confirm。

### 5.2 `UpgradePlan`

dry-run 返回 canonical plan，不修改生产状态：

```json
{
  "schema_version": "upgrade_plan.v2",
  "current": {
    "version": "1.13.7",
    "release_path": "/opt/options-monitor/releases/1.13.7"
  },
  "target": { "...": "UpgradeTargetIdentity" },
  "deployment": {
    "repo_root": "/opt/options-monitor",
    "runtime_root": "/var/lib/options-monitor",
    "releases_root": "/opt/options-monitor/releases",
    "profile_sha256": "<sha>",
    "config_yaml_sha256": "<sha>",
    "activation_policy": "preserve-existing",
    "restart_services": true
  },
  "planned_operations": [],
  "plan_digest": "sha256:<canonical-json>"
}
```

digest 必须覆盖：

- current version 和 resolved active release path；
- target version/tag/tag object SHA/commit SHA/GitHub Release identity；
- repo/runtime/releases roots；
- service profile bytes hash；
- YAML authoring source bytes hash；
- activation、restart、cleanup、major policy；
- versioned operation-plan schema。

digest 不覆盖 preview 时间、展示文本、cache hit、stdout/stderr 等易变字段。

confirmed apply 在任何 target code 执行前重新解析 remote identity、profile 和 config hash，并以常量时间比较 expected digest。任何变化返回 `upgrade_plan_stale`，不自动生成并执行一份新计划。

## 6. 三个入口的统一授权

### 6.1 Operator CLI

预览：

```bash
./om update apply \
  --repo-root /opt/options-monitor \
  --runtime-root /var/lib/options-monitor \
  --target-version 1.13.8
```

确认：

```bash
./om update apply \
  --repo-root /opt/options-monitor \
  --runtime-root /var/lib/options-monitor \
  --target-version 1.13.8 \
  --expected-plan-digest sha256:<digest> \
  --preserve-activation-state \
  --confirm --yes
```

生产 symlink 上的 confirmed apply 若缺少 `target_version` 或 `expected_plan_digest` 必须 fail closed。非生产测试 fixture 可以通过显式注入绕开，不给普通 CLI 增加隐式 fallback。

### 6.2 Feishu/WeChat 入站确认

现有 `InboundOperationStore`、payload hash、operation signature、TTL 和 confirm lifecycle 继续作为 authority；不新增第二个 approval store。

preview payload 额外冻结：

- `target_version`
- `release_tag`
- `remote_tag_object_sha`
- `remote_commit_sha`
- `release_id`
- `plan_digest`
- `preserve_activation_state=true`

worker 执行前同时验证 operation signature/payload hash 和重新计算的 plan digest。当前 `_upgrade_defaults(auto=True)` 改为显式 `trigger_kind=assistant_confirmed`；`auto` 仅保留为兼容输出字段并标记 deprecated，不能决定权限。

### 6.3 Scheduled unit

scheduled unit 永远不进入 apply：

```text
options-monitor-upgrade.service
  ExecStart=... om update check --stable-only
```

过渡 release 保留原 unit 名称以便现有 profile 原地 reconcile，但移除 `--auto --confirm`，Description 改为 release check。check 可以刷新 git cache、输出候选到 journal；不得 materialize release、创建 venv、重建 live config、切换 symlink、reconcile/restart service 或写业务状态。

最终迁移后默认不渲染该 pair。若未来确有“发现新版本”产品需求，再以 `--include-upgrade-check` 显式启用 check-only pair；不得恢复 `--include-auto-upgrade` 的 apply 语义。

## 7. 执行状态机

```text
planned
  -> preparing
  -> prepared
  -> waiting_activation_gate
  -> activating
  -> verifying_transition
  -> verifying_operational
  -> active_pending_observation

preparing/prepared
  -> blocked                         # current 未变

activating/verifying_*
  -> rollback_in_progress
  -> rolled_back | rollback_failed
```

### 7.1 Prepare phase

在现有 `upgrade.lock` 内：

1. 重算并验证 plan digest；
2. 以 frozen commit SHA materialize target；
3. 校验 materialized commit 对应的 VERSION/release metadata；
4. 创建或验证 target venv；
5. 执行严格 release check：taxonomy、delta coverage、dependency graph、Agent spec；
6. 用 target release 构建 staged US/HK/assistant config；
7. 校验 staged config identity/freshness/schedule；
8. 从 target release 对当前 profile 做 service drift dry-run；
9. 写入 `prepared` status，但不修改 live symlink/config/service。

目标 release 的 Python 代码只能在 GitHub Release 和 frozen commit identity 验证后执行。

### 7.2 Activation gate

增加一个窄的 Linux systemd activation gate，避免 scheduled oneshot 与切换交叉：

- 所有 repository-rendered oneshot job 在执行真实命令前对
  `/var/lib/options-monitor/locks/upgrade-activation.lock` 持 shared lock；
- upgrade prepare 不占 exclusive lock；
- 即将切换 symlink 前，upgrader 以 bounded timeout 取得 exclusive lock；
- 已运行 job 可以自然完成；新触发 job 等待 activation 完成；
- 无法在 timeout 内取得 exclusive lock时返回 `activation_gate_busy`，current 保持不变；
- 长期 service 不持 shared lock；它们在 exclusive activation 内按 profile 重启和验证；
- launchd 不在首个实现 slice 内启用自动 activation gate，且同样没有 scheduled apply。

systemd wrapper 由 service renderer 统一生成，不在每个业务命令中复制锁逻辑。upgrade check unit可以使用 shared lock，但不得取得 exclusive lock。

### 7.3 Activate phase

取得 exclusive gate 后：

1. 再确认 active symlink/current version 没有变化；
2. 捕获现有 timer activation snapshot；
3. 原子切换 symlink；
4. 原子提交 staged runtime configs；
5. 用 target release reconcile unit/profile，强制使用 `preserve-existing`；
6. 重启 profile-owned 长期 services；
7. 进入 transition verification；
8. 验证完成或 rollback 结束后释放 exclusive gate。

任何 compensation 都使用同一 activation snapshot。新增 timer 可以按 target profile 创建，但存量 disabled/masked/inactive timer 不得被激活。

## 8. 验证与成功语义

### 8.1 Transition verification：失败即 rollback

- current symlink 精确指向 target release；
- active `VERSION`、target identity 和 generated config version 一致；
- US/HK config validate、identity、freshness、schedule 通过；
- service drift 无 error，且不存在 target release 要求之外的旧自动 apply unit；
- 所有应重启的长期 services active/enabled；
- Feishu WS 和 WeChat ClawBot check 通过；
- OpenD 两个账户的进程/端口和只读 health surface 通过。

### 8.2 Operational verification：新失败才 rollback

升级前先记录 compact baseline，升级后比较：

- `runtime_status` US/HK；
- `healthcheck` US/HK；
- projection verification；
- scheduler/timer activation 与 next-run 合法性；
- systemd failed-unit 集合；
- credential metadata readiness，仅检查 name/status/permission，不读取值；
- release/current/profile/config identity。

如果目标 release 新增 blocker 或使原本健康的检查失败，则 rollback。升级前已经存在的 Auto Close credentials failure、Position Advice Promotion timeout 等必须输出为 `preexisting_degradation`；它们不能被本次升级标为成功修复，也不应在未恶化时触发无意义 rollback。

### 8.3 Deferred observation

以下事实只能由自然调度证明：

- 下一次 US/HK tick 完成；
- 下一次 Auto Close 成功；
- 下一次 AI evidence collection 成功；
- 下一次 Position Advice promotion 成功；
- 下一次 quality day-end/Strategy Lab job 成功。

因此即时成功状态使用：

```text
active_pending_observation
```

并记录 `verification_scope`、`preexisting_degradations`、`pending_natural_checks`。后续 `runtime_status` 可以把自然执行结果归并为 `active_verified`；不主动重跑这些任务。

### 8.4 无通知边界

升级验证不得调用普通 tick、通知发送、trade intake replay、position write 或任何 provider send。允许的 canary 必须是现有 read-only/`--no-write-outputs` surface，并明确记录 `notification_attempted=false`。

## 9. Rollback contract

即时失败补偿和显式 rollback 必须：

1. 绑定准确的 previous release path/version；
2. 恢复 runtime config bundle及其 hash；
3. 从 previous profile reconcile service definitions；
4. 恢复升级前 timer activation snapshot；
5. 重启并验证 previous release 长期 services；
6. 记录 rollback 后仍存在的 preexisting degradation；
7. 保持 legacy auto-apply timer disabled/retired。

如果回滚到仍包含旧 `--auto --confirm` 代码的 release，activation restoration 必须以“升级前已 disabled”状态为准，绝不能因 previous profile 的 `auto_upgrade.enabled=true` 重新启用它。

## 10. Profile 与 systemd 迁移

### 10.1 Profile schema

把旧字段：

```json
{"auto_upgrade": {"enabled": true, "schedule_beijing": "06:10"}}
```

迁移为显式模式：

```json
{"upgrade_check": {"enabled": true, "schedule_beijing": "06:10"},
 "auto_upgrade": {"enabled": false, "retired": true}}
```

`service_drift` 的兼容规则：

- `auto_upgrade` key 不存在时，才允许根据 legacy service list 推断过渡 check-only unit；
- `auto_upgrade.enabled=false` 是 tombstone，优先于 service list，允许 drift 退休旧 unit；
- 任何 legacy `enabled=true` 在新 release 中最多迁移为 check-only，绝不能继续渲染 apply command；
- profile comparison 和 runtime status 显式展示 `upgrade_mode=manual` 或 `check_only`。

### 10.2 受控远端迁移顺序

该顺序需要单独生产授权，本设计分支不执行：

1. read-only 记录当前 unit、timer activation、last result、active version 和 rollback release；
2. `disable --now options-monitor-upgrade.timer`，验证 inactive/disabled；
3. 发布包含 check-only bridge 和 plan contract 的 release；
4. 用 exact target、plan digest、`preserve-activation-state` 人工升级；
5. 运行 drift dry-run，确认旧 apply ExecStart 将被移除；
6. confirmed drift 写入 tombstone并退休旧 service/timer，或按明确选择保留 check-only pair；
7. 验证 unit 文件、profile、runtime status 和 rollback 均不会恢复 auto apply；
8. 至少保留前一 release，但其 upgrade timer activation 必须保持 disabled。

最终默认选择是退休 pair，systemd unit 总数减少 2。

## 11. 实施切片

### Slice 1：立即切断 unattended apply

文件边界：

- `src/application/service_deploy.py`
- `src/application/service_drift.py`
- `src/interfaces/cli/service_ops.py`
- `tests/test_service_deploy.py`
- 当前部署/发布文档

实现：

- 现有 upgrade timer 只执行 stable check，不再包含 `apply`、`--auto` 或 `--confirm`；
- 增加 `upgrade_check` profile mode 和 `auto_upgrade` tombstone；
- legacy enabled profile 只能兼容迁移为 check-only；
- confirmed upgrade 默认 `preserve_activation_state=true`；显式 activation repair 使用单独、清晰命名的 override；
- 测试证明 render、drift、rollback 均不能恢复旧 apply command。

Gate：repository-wide 搜索生成 unit/fixture，不存在 scheduled `update apply --auto --confirm`。

### Slice 2：稳定 target identity 与 plan CAS

文件边界：

- `src/application/release_target.py`
- `src/application/service_upgrade.py`
- `src/interfaces/cli/service_ops.py`
- `src/application/assistant/upgrade_operations.py`
- 对应 release/upgrade/inbound tests

实现：

- stable-only target resolver；
- GitHub Release publication adapter；
- `UpgradeTargetIdentity` 和 canonical `UpgradePlan`；
- `--expected-plan-digest`；
- CLI confirmed apply 强制 target+digest；
- 入站 preview 冻结 SHA/release/digest，worker 重算 CAS；
- `auto` 改为兼容展示字段，权限使用 `trigger_kind` 与 approval evidence。

Gate：prerelease、draft、tag moved、Release missing、profile/config drift、expired preview、digest mismatch 全部在执行 target code 前 fail closed。

### Slice 3：activation gate 与验证语义

文件边界：

- `src/application/service_deploy.py`
- `src/application/service_upgrade.py`
- `src/application/service_drift.py`
- `src/application/agent_tools/runtime_status_impl.py`
- systemd/upgrade tests 和 operator docs

实现：

- rendered oneshot shared activation lock；
- upgrade exclusive activation lock和 bounded wait；
- pre/post compact health baseline；
- transition/new-regression/preexisting-degradation 分类；
- `active_pending_observation` 与 deferred natural checks；
- rollback 复用同一 activation snapshot。

Gate：并发 job 故障注入证明切换不交叉；新增 regression rollback；preexisting failure 不误归因；验证不产生通知/provider write。

### Slice 4：远端迁移和最终退休

前置条件：前三个 slice 已发布且本地测试/CI通过。

操作：

- 只读 preflight；
- 显式授权后停用旧 timer；
- exact target 人工升级；
- drift dry-run/confirm；
- 最终删除两个旧 unit；
- 完整无通知验证；
- 等自然调度完成 deferred checks。

Gate：远端不存在任何定时 confirmed apply；当前 release/rollback release、profile 和 activation state 都可证明；unit 数按最终选择减少 2。

## 12. 测试矩阵

| 场景 | 预期 |
|---|---|
| stable `v1.13.8` + published Release | 可生成 plan |
| `v1.14.0-rc.1` 高于当前 | 自动发现忽略/阻断 |
| draft/prerelease/missing GitHub Release | `release_not_deployable` |
| preview 后 tag commit 改变 | `upgrade_plan_stale` |
| preview 后 config YAML/profile 改变 | `upgrade_plan_stale` |
| confirmed apply 缺 target/digest | 不执行 |
| timer render | 只能出现 check，不能出现 apply/confirm |
| legacy profile enabled | 迁移为 check-only，不恢复 apply |
| tombstone + extra旧 unit | drift 计划并退休旧 unit |
| paused/masked timer | upgrade/rollback 后状态不变 |
| scheduled oneshot 正在运行 | activation 等待；超时则 current 不变 |
| target pre-switch validation 失败 | current/config/service 不变 |
| 新增长期 service health failure | 自动补偿 rollback |
| 升级前已有 failed oneshot | `preexisting_degradation`，不伪装 healthy |
| rollback 到旧代码 release | auto apply timer 仍 disabled/retired |
| assistant preview 被篡改/过期 | signature/hash/digest 拒绝 |
| post-upgrade verification | `notification_attempted=false` |

## 13. 验收标准

代码完成不等于生产迁移完成。最终验收必须同时满足：

1. focused release/service/inbound/runtime-status tests 通过；
2. full pytest 和 dependency graph check 通过；
3. generated systemd bundle 中没有 scheduled confirmed apply；
4. dry-run 对 exact target 输出稳定 plan digest；
5. CAS、prerelease、tag drift、profile/config drift 和 activation concurrency 故障注入通过；
6. 发布后的远端升级使用 exact target/plan，不使用 latest apply；
7. transition 和 operational verification 通过或仅保留明确的 preexisting degradation；
8. 旧 auto-apply timer inactive/disabled，最终 retired；
9. rollback 演练或 deterministic fixture 证明不会重新启用旧 timer；
10. 自然调度完成前只报告 `active_pending_observation`，完成后才报告相应业务路径 verified。

## 14. 回滚与停止条件

任一 slice 出现以下情况立即停止，不继续扩大 scope：

- 无法从 remote/tag/GitHub Release 得到唯一 target identity；
- 需要读取或输出 secret value 才能完成验证；
- profile migration 不能证明旧 apply unit保持 disabled；
- activation gate 会杀死而不是等待既有 business job；
- rollback 不能恢复 config、symlink、service 和 timer activation 的同一份快照；
- 验证需要发送真实通知或写业务状态；
- 工作区存在所有权不明且与本 slice 文件重叠的改动。

## 15. 最小实现结论

最小正确方案不是给现有 `--auto` 增加更多 if，而是：

1. timer 不再有 apply authority；
2. confirmed apply 必须绑定 exact stable Release identity 和 plan digest；
3. activation 默认 preserve existing，并与 scheduled oneshot 隔离；
4. 验证诚实地区分即时 control-plane 成功与待自然运行证明的业务健康；
5. 存量 unit 通过 check-only bridge 安全迁移，最终默认退休。

这保留现有升级器中最有价值的原子切换与补偿能力，同时移除不必要的永久生产授权。

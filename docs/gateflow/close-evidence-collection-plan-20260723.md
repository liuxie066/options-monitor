# Close Advice S5 Evidence Collection Bridge — Implementation Plan

## 1. Goal

在不改变生产 Close Advice 决策权威的前提下，把已经存在的 `close_advice.csv` 正式决策证据接入现有 Strategy Lab recorder，使 S5 能自动积累 episode、mark 和 outcome 数据，并可用现有 readiness / experiment 流程评估 P0/P1/P2/P3。

本 work unit 只建立 evidence collection bridge。P0 继续是唯一生产策略；P1/P2/P3 继续 shadow-only。

## 2. Current Facts

- `research shadow-replay build --include-close-decisions` 已能严格构建 Close Advice facet，但只能手工调用。
- Strategy Lab build timer 每 6 小时运行 `research strategy-lab update --build-dataset --write`，当前没有启用 Close Advice facet。
- `run_strategy_lab_update()` 只选择“最新包含 candidate / trace / reject evidence 的 run”。生产 run 中 Close Advice 文件与最新 candidate run 不保证出现在同一个目录，因此直接把 close flag 传给现有 latest build 会周期性失败。
- Close capture 已在 owner boundary 内 fail-closed：缺少 lot identity、audit timestamp、position context 或正式决策字段时抛错，不生成不完整数据集。
- Dataset ID 默认使用 run ID；已存在的数据集不会被覆盖，从而保护已采集的 mark / outcome。

## 3. Scope

### In scope

1. 给 Strategy Lab update 增加显式 opt-in `include_close_decisions` 契约。
2. 在一次 build update 中，独立选择最新包含至少一条 Close Advice 行的 run，并先构建带 Close Advice facet 的 dataset。
3. 保持现有 latest candidate dataset build 的选择和行为不变；Close build 完成后再执行 candidate build。
4. 让 systemd / launchd Strategy Lab recorder build action 默认显式启用 Close Advice evidence collection，并在 service profile 中公开该能力。
5. 补齐 CLI、application、service rendering、idempotency、empty / malformed evidence 和安全边界测试。
6. 更新最小必要运维文档，明确采样频率、失败语义、写入边界和验证命令。
7. 合并并发布后，执行一次生产 research canary，再启用更新后的既有 recorder unit；验证不触发通知、配置、交易或券商写入。

### Out of scope

- 不修改 `domain/domain/close_advice.py`、P0/P1/P2/P3 阈值或排序。
- 不改变通知文案、通知路由或发送行为。
- 不修改 `config.yaml`、生成配置、position ledger、trade events、Feishu mirror 或 broker-facing data。
- 不新增 service / timer，不提高现有 6h build、2h mark、每日 settle 的频率。
- 不回填历史 legacy run，不覆盖或原地升级已存在的 candidate-only dataset。
- 不自动晋升 shadow policy，不改变 production recommendation authority。

## 4. Architecture and Ownership

### 4.1 Run selection owner

在 `src/application/shadow_replay/capture.py` 增加窄 helper：

```python
latest_close_decision_run_dir(
    *, repo_root: Path, runs_root: Path | None = None
) -> tuple[Path | None, dict[str, Any]]
```

契约：

- 按现有 `latest_shadow_replay_run_dir()` 相同的 runs-root / mtime 约定遍历 run。
- 用现有 `ShadowReplaySourceSelection` 与 `close_advice_paths_from_selection()` 发现 account-scoped `close_advice.csv`。
- 只有至少一个 CSV data row 的 run 才算可采集；header-only / empty 文件计入 skipped-empty，不阻止继续寻找更早的非空 run。
- helper 只做可采集性选择，不复制 Close Advice schema/policy 校验。发现非空 run 后，由现有 `capture_close_decision_episodes()` 做完整 fail-closed 校验；不能因新 selector 而把 malformed evidence 静默跳过。
- 返回稳定、可测试的 selection metadata：found、run ID/path、searched count、skipped-without-close count、skipped-empty count、close path count、close row count。

这保持 evidence discovery 属于 Shadow Replay capture owner，Strategy Lab 只负责编排。

### 4.2 Strategy Lab orchestration

在 `src/application/strategy_lab/update.py`：

- `run_strategy_lab_update()` 新增 `include_close_decisions: bool = False`。
- `include_close_decisions=True` 必须与 `build_dataset=True` 一起使用；同时禁止显式 `dataset_id`，因为一次 update 最多涉及 close run 和 candidate run 两个独立 run ID，单一显式 ID 会产生冲突语义。
- 新增窄的 `_build_latest_close_decision_dataset()`：
  - 未启用时返回 `not_requested`；
  - 找不到非空 Close Advice run 时返回 `latest_close_decision_run_not_found`，这是正常 empty state，不让 recorder unit 失败；
  - dry-run 返回 `requires_write` 且不创建目录；
  - target 已存在时不覆盖，并报告：若 manifest 已声明 close facet 则为正常 idempotent skip；若 target 是 candidate-only，则报告 `dataset_exists_without_close_decisions`，保留数据且不伪装为采集成功；
  - write 时以选中的 `run_dir` 和 run ID 调用现有 `build_shadow_replay_dataset(..., include_close_decisions=True)`；所有正式 evidence 校验继续由 capture owner 执行。
- 执行顺序固定为 close build -> candidate build -> data plan：
  - 同一 run 同时有 close 和 candidate evidence 时，close build 先生成完整 dataset，candidate build 因 run ID 已存在而幂等跳过；
  - 不同 run 时各生成一个 dataset，候选采样行为不变；
  - data plan 在两个 build 后运行，立即看见新 dataset。
- Close build 与 candidate build 是独立写入单元。若 close build 因非空 malformed evidence 抛出 `ValueError`，orchestrator 必须保存原异常、继续执行原 candidate build，再重新抛出同一错误；不得把 Close failure 转成 success，也不得让新 facet 阻断既有 candidate evidence accumulation。异常路径不继续 data plan。
- 同 run malformed 时，异常后的 candidate build 可以安全落下 candidate-only dataset；这会让该 run 后续显示 `dataset_exists_without_close_decisions`，但不覆盖/迁移它。下一条合法 Close run 继续自然恢复自动采集。
- 保留 `shadow_replay.dataset_build` 作为现有 candidate build 公共字段；新增 `shadow_replay.close_decision_dataset_build`，避免破坏既有消费者。
- 现有 singular summary 字段 `dataset_build_requested`、`dataset_built`、`dataset_build_reason`、`built_dataset_id` 继续严格表示 candidate build，保持兼容；新增 `close_decision_dataset_build_requested`、`close_decision_dataset_built`、`close_decision_dataset_build_reason`、`built_close_decision_dataset_id`。
- 顶层 `summary.status` 与 safety persistent write targets 对任一 build 聚合；`next_action` 的 dry-run 可以继续使用通用 rerun-with-write action，malformed evidence 仍通过异常暴露。

### 4.3 Public CLI and service wiring

在 `src/interfaces/cli/research.py`：

- 给 `research strategy-lab update` 增加 `--include-close-decisions`。
- 参数仅传入 application contract；不在 CLI 重做选择或校验。

在 `src/application/service_deploy.py`：

- 现有 systemd / launchd Strategy Lab build action 增加 `--include-close-decisions`。
- `service.profile.json.strategy_lab_recorder` 增加 `include_close_decisions: true`，作为可观测部署事实。
- 不新增 renderer 参数或 operator-authored runtime config knob：Strategy Lab recorder 本身已是显式 opt-in，启用 recorder 即采集其当前支持的正式 research evidence，避免第二层容易漂移的开关。

## 5. State and Failure Semantics

| State | Close build result | Candidate build | Unit result |
|---|---|---|---|
| 没有 Close Advice 文件或全部为空 | skip: `latest_close_decision_run_not_found` | 照常 | 成功 |
| 最新非空 Close run 合法且 target 不存在 | build close facet | 照常；同 run 时幂等 skip | 成功 |
| target 已含 close facet | idempotent skip | 照常 | 成功 |
| target 已存在但无 close facet | safe skip + explicit reason | 照常 | 成功但 evidence gap 可见 |
| 非空 Close run 缺必需正式字段/context/audit | capture 抛错，目标目录在严格校验前不应形成有效 close dataset | candidate build 仍执行，随后重新抛出原错误 | 失败，等待更新的合法 run；candidate evidence 不丢失 |
| dry-run | 只返回选择和 `requires_write` | 现有 dry-run | 无写入 |

不使用 suffix dataset ID 绕过冲突，因为同一 run 的 candidate snapshots 会在跨 dataset 分析中重复计数。也不原地增补 candidate-only dataset，因为那会引入对 manifest、marks、outcomes 的部分更新/恢复事务，本 work unit 没有必要承担该迁移风险。

## 6. Implementation Slices

### S1 — Close-aware Strategy Lab build contract

Files:

- `src/application/shadow_replay/capture.py`
- `src/application/strategy_lab/update.py`
- `src/interfaces/cli/research.py`
- `tests/test_strategy_lab.py`
- `tests/test_research.py`
- `tests/test_close_advice_shadow_capture.py`（只有 selector owner 的测试确有必要时）

Deliverables:

- latest non-empty close-run selector and metadata;
- opt-in application / CLI contract;
- close-first / candidate-second orchestration;
- explicit result, summary, safety and next-action semantics;
- deterministic tests for different-run, same-run, empty, malformed, dry-run, collision and idempotency paths.
- CLI parser-to-handler forwarding 及不合法参数组合测试。

Acceptance:

- 现有 Strategy Lab update tests 不改语义地通过；
- 新 tests 证明最新 candidate run 无 close 文件时不会导致 close capture 错选或失败；
- malformed non-empty close evidence 仍 fail-closed；
- malformed close evidence 抛错前仍完成独立 candidate build；
- build 不覆盖已有 marks/outcomes；
- 既有 candidate summary 字段保持原语义，新增 close-specific summary 字段准确反映第二个 build；
- CLI 拒绝 `--include-close-decisions` 缺少 `--build-dataset` 或同时携带 `--dataset-id`，并证明合法 flag 被传到 application；
- safety 保持 `writes_runtime_config=false`、`writes_trade_state=false`、`sends_notifications=false`。

### S2 — Existing recorder wiring and operator contract

Files:

- `src/application/service_deploy.py`
- `tests/test_service_deploy.py`
- `docs/TOOL_REFERENCE.md`
- `docs/SHADOW_REPLAY_RUNBOOK.md`
- `docs/DEPLOY_LINUX_MAC.md`（仅当现有 recorder 安装/验证段需要同步）

Deliverables:

- systemd / launchd build command 启用 close collection；
- service profile 明确暴露该事实；
- 文档说明 6h sampling、2h marks、daily settlement、fail-closed 和不改变生产策略的边界；
- renderer / drift tests 更新。

Acceptance:

- systemd 与 launchd 生成命令均含 `--include-close-decisions`；
- 未启用 Strategy Lab recorder 时仍不生成相关 unit；
- service drift 仍通过；
- 不新增 timer/service 或 operator-authored runtime config key；只新增上述 service profile observability field。

## 7. Verification

### Focused tests

```bash
python3.12 -m pytest -q \
  tests/test_close_advice_shadow_capture.py \
  tests/test_strategy_lab.py \
  tests/test_service_deploy.py
```

### Broader checks

```bash
python3.12 -m pytest -q tests/test_shadow_replay_*.py tests/test_research.py
python3.12 scripts/check_dependency_graph.py
python3.12 scripts/release_check.py --check
```

最终发布前按仓库 release flow 运行完整 Python 3.12 suite、release smoke 和 dependency graph。

## 8. Production Rollout and Canary

Gateflow 完成 Draft PR 后停在 merge 授权边界。获 CEO 明确合并/发布授权后：

1. 合并并走 VERSION-driven patch release；不手工修改生产配置。
2. 远端升级前检查 release、磁盘 headroom、当前 service profile 和 Strategy Lab units。
3. 安装/刷新同一组既有 recorder units，使 build command 带新 flag；不新增服务。
4. 单次手工 research canary：运行与 build unit 等价的 `strategy-lab update --latest --build-dataset --include-close-decisions --write --max-datasets 0`。该操作只写本地 research dataset；不发送通知、不写交易/配置/券商数据。
5. 验证：
   - result 显示 close dataset built 或明确幂等/empty reason；
   - 新 dataset manifest 有 `close_decision_facet` 且 episode count 与源文件可核对；
   - P0 shadow evaluation 与生产 `recommendation_state` 一致；
   - mark sampler / settle timer active，unit 最近一次状态成功；
   - runtime health、核心 services/timers 保持正常。
6. 若 canary 发现 malformed evidence，停止 rollout diagnosis，不弱化 capture 校验、不删除/覆盖 dataset、不提升策略。

## 9. Success Criteria

- 自动 recorder 能从最新非空 Close Advice run 生成严格、可审计的 close decision dataset。
- candidate recorder 原有选择行为保持不变。
- 同 run 不重复建 dataset，不覆盖已有 research state；collision 明确可见。
- mark / outcome lifecycle 无需新服务即可继续处理 close facet。
- 生产 P0 authority、通知、配置、交易状态和 broker-facing data 均未改变。
- focused + relevant broader tests、dependency graph、release checks 和生产 canary 全部通过。

## 10. Residual Risks and Deferred Work

- 6 小时 recorder 是时间采样，不保证捕获每一次 tick 内部状态转换；它足以启动 S5 evidence accumulation，但 readiness 报告必须把采样密度视为 coverage，而不是完整事件日志。
- 已存在的 candidate-only dataset 不在本次原地迁移；collision 会被显式报告，后续合法 Close run 会自然建立新 evidence。
- 目录 mtime 与现有 selector 保持一致；活跃 run 的短暂部分写入可能令一次 unit fail-closed，下一次完整 run 会恢复。若生产观察到持续竞争，再单独设计 run-complete marker，不在本 work unit 推断新增状态。
- 自动采集只解除“零数据”阻塞，不等于策略可晋升；P1/P2/P3 仍需达到既定样本量、mark/outcome、分层稳定性和 CEO 决策门槛。

## 11. Open Questions

- 无。实现不得重新解释以上范围；若发现必须覆盖现有 dataset 或改变 timer cadence 才能达成目标，停止并回到 CEO 决策。

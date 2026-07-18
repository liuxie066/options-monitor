# 升级与发布流程优化实施计划

> 日期：2026-07-18
> 状态：Revised after `docs/reviews/plan-review-20260718-194351.md`
> 基线：`origin/main` `b5ba7693` / VERSION `1.2.413`
> 目标：在不削弱质量门、最小权限、升级原子性和回滚能力的前提下，显著降低本地发布检查、GitHub Release 和生产依赖变更升级耗时。

## 1. 执行摘要

原方案保留方向，但改为 **6 个互相隔离的实施 slice + 2 个可选观测 slice**。核心调整：

1. **先拿低风险确定收益**：去掉测试中的两个真实 30 秒等待；`--full` 不重复执行 focused tests。
2. **可靠性先于安装器提速**：先补 timeout、进程终止、状态落盘和重试语义，再改 dependency/cache。
3. **dependency lock 不再后置**：先把 release 依赖固定下来，再优化 server 单次安装、共享 venv 和 `uv`。
4. **不再使用“临时 venv 构建后 rename”**：venv 从一开始使用最终绝对路径，靠 incomplete/complete manifest 隔离半成品；完成前禁止 release symlink 指向它。
5. **CI 去重不合并安全职责**：Guardrails、Agent Plugin、Release 保持独立 trigger、check name 和 permission；只优化 setup/cache，并消除 `resolve-tag` 调度 job。
6. **`uv` 只做独立 canary rollout**：先用 `auto`，验证后才考虑 strict；strict 配置属于生产变更，必须单独确认。

## 2. 成功标准

### 2.1 性能指标

| 路径 | 已测基线 | 本计划目标 | 验收方法 |
|---|---:|---:|---|
| 本地 `release_preflight --full` | 三次 108/103/106s，median 106s；full pytest median 101.16s | 3 次运行 median ≤ 45s；无单测 > 5s（明确标记的 integration test 除外） | 同一机器、同一 clean worktree，连续 3 次 |
| GitHub `Release from VERSION` | median 73s，P90 79s | 10 次运行 median ≤ 62s，P90 ≤ 75s | workflow run 数据；不以单次 run 验收 |
| 正常生产升级（venv cache hit） | 20.06s；venv reuse 0.81s | 不回退：总耗时 ≤ 25s；venv reuse ≤ 2s | 至少 3 次无依赖变化升级/演练 |
| 依赖变化、warm download cache | 曾出现约 39m38s 残留构建 | P50 ≤ 180s；单次硬上限 600s；失败后 30s 内完成状态落盘与清理判断 | disposable cache canary，至少 3 次 |
| 异常恢复 | 当前 timeout 可逃逸 runtime cleanup | timeout 后无活跃安装进程；半成品不可复用；下一次重试可成功 | 故障注入测试 + dry-run/canary |

### 2.2 正确性与安全指标

- 全量测试不低于基线：`2666 passed, 10 skipped` 所覆盖行为不得减少；若 origin/main 测试数变化，以执行时最新 clean baseline 为准。
- cooldown 行为仍有独立测试断言 `30.0s`，只是测试不真实 sleep。
- `upgrade_status.json` 保持旧字段兼容，并为 timeout/cleanup 增加结构化字段。
- 任何 incomplete shared venv 都不能被 `_shared_venv_valid()` 接受，不能被 release `.venv` symlink 使用。
- 同一 release lock 在 pip/uv 安装后得到相同的 package name/version 集合。
- Guardrails/Agent Plugin 继续使用 `contents: read`；只有 publish job 使用 `contents: write`。
- 不修改 `config.yaml`、生成 runtime config、服务定义或真实通知路径。

## 3. 非目标

本计划不包含：

- 删除生产历史 stale venv/cache/release；清理需要单独 read-only inventory 和明确确认。
- 修改 Feishu 通知、持仓、交易、broker-facing 数据。
- 把全部 workflow 合并成一个“大流水线”。
- 为两个测试引入通用虚拟时钟框架。
- 在首次代码发布中启用 `OM_UPGRADE_INSTALLER=uv` strict mode。
- 仅为节省几秒而把 Feishu 实际 import 检查替换成 `find_spec`。

## 4. 必须保持的不变式

### 4.1 发布不变式

1. VERSION、tag、commit 必须一致；release archive、spec 和 release notes 来自同一 commit。
2. 发布 validation 与 publish 权限分离；只读检查不得因去重获得写权限。
3. workflow/job 名称若被 branch protection 使用，则本计划不改名。

### 4.2 命令执行不变式

每个外部命令必须归一化为以下 outcome 之一：

- `succeeded`
- `failed`
- `timed_out`
- `spawn_failed`
- `cancelled`

无论 outcome 如何，都必须：

- 记录 started/ended/duration/command/cwd；
- 保留 `ok` 和 `returncode` 兼容字段；
- 把 stdout/stderr 截断后写入 operation；
- 在升级顶层持久化最终状态；
- timeout 时先终止 process group，再判断是否允许 cleanup；
- 不把 cleanup failure 伪装成普通 command failure。

### 4.3 shared venv 不变式

shared venv 状态机固定为：

```text
ABSENT
  -> BUILDING
  -> VALIDATING
  -> COMPLETE

BUILDING/VALIDATING
  -> FAILED_INCOMPLETE
  -> ABSENT（确认 owner 已退出后清理）
```

规则：

- venv 从创建时就使用最终绝对路径，禁止构建后 rename/copy。
- `BUILDING`/`VALIDATING` 目录必须没有 complete marker。
- 只有 atomic 写入 complete manifest 后才是 `COMPLETE`。
- release `.venv` symlink 只能在 `COMPLETE` 后创建/切换。
- 发现 incomplete 目录时：owner PID 活跃则返回 busy；owner 不活跃才允许删除并重建。
- SIGKILL、主机重启、磁盘满后，下一次升级必须能明确识别并恢复。

### 4.4 dependency 不变式

本计划采用 **release-pinned lock**：

- `requirements/runtime.txt`、`requirements/server.txt` 继续作为人工声明入口。
- 新增生成文件：
  - `requirements/locks/runtime-py311.txt`
  - `requirements/locks/server-py311.txt`
- lock 固定全部 transitive package versions；server lock 必须是可独立安装的 runtime superset。
- 生产安装器读取 lock，不再直接解析浮动 source requirements。
- dependency hash 覆盖：lock bytes、Python implementation/major.minor、platform、architecture、server mode、installer mode。
- completion manifest 记录 lock SHA256 和实际安装 package name/version；cache reuse 前校验 manifest context。

这意味着 SDK 更新通过“更新 source requirement → 重新生成 lock → validation → release”进入生产，而不是同一 release tag 随时间漂移。

## 5. 实施切片

---

## Slice 0：执行前基线与事实冻结

### 目标

避免在本地落后分支或变化后的 origin/main 上按旧证据实施。

### 操作

1. 从执行时最新 `origin/main` 创建 clean worktree；不得直接在当前本地 `v1.2.402` 分支实施。
2. 记录：commit、VERSION、Python 版本、pytest count、release preflight 三次耗时。
3. 记录最近 20 次 GitHub workflow 的 median/P90、job/check names 和 permissions。
4. read-only 记录生产当前版本、installer、cache hit 状态、服务环境 PATH、index/proxy 变量是否存在；不得输出 secret value。

### Gate 0 验收

- baseline 写入实施记录；
- clean worktree；
- 确认无并行 release/upgrade 工作；
- 若源代码已修复任一问题，先缩减后续 scope，不重复实现。

### 回滚

无写入，无需回滚。

---

## Slice 1：消除本地 preflight 的确定性浪费

### 目标

在不改生产逻辑的前提下，将 full pytest 中的真实等待从约 60 秒降至接近 0。

### 文件边界

优先只修改：

- `tests/test_phase1_tool_boundary.py`
- `tests/test_fetch_market_data_opend_explicit_expirations.py`
- `scripts/release_preflight.sh`
- `tests/test_release_test_plan.py`（用于 preflight command-plan 契约）

只有测试无法局部注入时，才允许在已有 owning module 增加一个窄的 sleeper seam；禁止新增通用 clock abstraction。

### 实现要求

1. 两个慢测试 monkeypatch 实际 sleep/cooldown 边界，但保留原错误和 audit assertions。
2. 独立 cooldown 测试继续断言 `sleeps == [30.0]`。
3. `release_preflight.sh --full` 只运行一次 full pytest；focused agent/plugin tests 仅在非 `--full` 模式单独运行。
4. `--skip-focused`、`--skip-deps` 的现有 CLI 行为保持兼容。

### 测试

```bash
python3 -m pytest \
  tests/test_phase1_tool_boundary.py::test_prefetch_required_data_protections_minimal \
  tests/test_fetch_market_data_opend_explicit_expirations.py::test_fetch_symbol_reports_snapshot_rate_limit_errors \
  tests/test_required_data_prefetch_inprocess.py \
  tests/test_release_test_plan.py

bash scripts/release_preflight.sh --full --require-clean
```

### Gate 1 验收

- 两个目标测试各自 < 0.5s；
- full preflight 3 次 median ≤ 45s；
- full suite 测试数未减少；
- 生产 cooldown 实现无改动，或仅增加无行为变化的窄注入 seam。

### 回滚

只回滚本 slice 的测试/preflight commit，不影响生产升级代码。

---

## Slice 2：补齐 timeout、进程回收与结构化失败状态

### 目标

确保安装命令 timeout、spawn failure 和 cleanup failure 都是有界、可观测、可重试的。

### 文件边界

- `src/application/service_upgrade.py`
- `tests/test_service_deploy.py`
- 若 status public payload 新增字段，更新相应 docs/contract tests；不改变 CLI 命令名。

### 实现要求

1. 在 `service_upgrade.py` 内实现一个私有、POSIX-aware command executor，使用 `subprocess.Popen(..., start_new_session=True)`；测试通过 factory 注入，不从 application 层引入 scripts。
2. timeout sequence 固定：
   - 标记 `timed_out`；
   - 向 process group 发送 TERM；
   - 最多等待 5 秒；
   - 仍未退出则 KILL；
   - `wait()` 回收；
   - 收集 partial stdout/stderr；
   - 追加 operation 后返回 `ok=false`。
3. spawn failure 也返回结构化 operation，不直接逃逸丢失 context。
4. `_run_required()` 将非 success outcome 转换成带 operation context 的内部异常。
5. `_ensure_release_runtime()` 对所有安装失败生成 `RuntimePrepareError`，其中包含：
   - `failure_stage`
   - `outcome`
   - `cleanup.attempted`
   - `cleanup.ok`
   - `cleanup.paths`
   - `cleanup.errors`
6. 顶层 `service_upgrade_apply` 继续通过 atomic temp→replace 写 `upgrade_status.json`；旧字段保持兼容。
7. 只有在 process group 已确认退出后才能删除 incomplete build；否则记录 remediation 并保留现场。

### 故障注入测试

- direct child timeout；
- child 派生子进程；
- child 忽略 TERM，随后 KILL；
- spawn `FileNotFoundError`；
- cleanup 抛 `PermissionError`；
- status write 仍包含 timeout operation；
- timeout 后立即重试成功；
- 锁在退出后释放，活跃 owner 不被误判 stale。

### Gate 2 验收

- 所有故障路径在测试中 ≤ 10 秒结束；
- timeout 后无残留测试子进程；
- status 明确区分 command failure、timeout、spawn failure、cleanup failure；
- 现有 upgrade dry-run、success、rollback tests 全部通过。

### 回滚

回滚 command executor commit 即恢复旧行为；本 slice 不改变 dependency/cache layout，也不触碰生产配置。

---

## Slice 3：固定 dependency contract，并消除 server 重复安装

### 目标

先解决 dependency reproducibility，再把 server install 收敛为一次安装。

### 文件边界

- `requirements/runtime.txt`
- `requirements/server.txt`
- `requirements/locks/runtime-py311.txt`（新增）
- `requirements/locks/server-py311.txt`（新增）
- `requirements/dev.txt` / `constraints/dev.txt`（固定 lock generator 版本，如需要）
- `scripts/release_check.py`
- 可新增一个窄的 lock generation/check script；不得引入常驻服务或新配置层。
- `src/application/service_upgrade.py`
- `tests/test_service_deploy.py`
- release/plugin contract tests

### 实现要求

1. 用固定版本 resolver 生成两个 exact-version lock；server lock 从 `requirements/server.txt` 生成并完整包含 runtime。
2. lock generation 必须可重复；`release_check.py` 检查 lock 与 source requirements 同步。
3. server mode 只执行一次：安装 `server-py311.txt`；非 server mode 只安装 `runtime-py311.txt`。
4. pip path 不再先 `pip install -U pip`；构建工具版本由 venv/bootstrap policy 固定，避免每次网络升级 pip。
5. pip 与 uv 均安装同一 lock；不得为两个 installer 维护两套依赖版本。
6. 安装后用 `importlib.metadata` 生成 package manifest，并验证至少：
   - runtime imports：`pandas`、`yaml`、Futu/yfinance owning smoke；
   - server imports：以上全部 + `lark_oapi`；
   - package versions 与 lock 一致。

### 测试

- lock freshness check；
- server lock 是 runtime superset；
- pip command plan 只出现一次 install；
- uv command plan 只出现一次 install；
- runtime/server import smoke；
- source requirement 改变但 lock 未更新时 release check 必须失败；
- 同 lock 两次生成结果 byte-identical。

### Gate 3 验收

- server path 不再重复解析/安装 runtime；
- package manifest 与 lock 完全匹配；
- 同 release tag 的 fresh build package versions 一致；
- 完整 release preflight 通过。

### 回滚

保留原 source requirements；回滚后 pip 可恢复旧安装命令。此 slice 尚不修改生产 installer mode。

---

## Slice 4：修复 shared venv shebang，并建立 crash-safe lifecycle

### 目标

消除 rename 后脚本绝对路径失效，同时保证 incomplete venv 永不被复用。

### 文件边界

- `src/application/service_upgrade.py`
- `tests/test_service_deploy.py`
- 若 shared cache contract 属于 public operations behavior，更新 `docs/AGENT_WIKI.md` 或 upgrade runbook。

### 完成 manifest

用现有 `.options-monitor-deps-complete` 路径写 JSON，而不是只写 timestamp；atomic temp→replace。至少包含：

```json
{
  "schema_version": 1,
  "state": "complete",
  "dependency_hash": "...",
  "lock_sha256": "...",
  "installer": "pip|uv",
  "python": "3.11.x",
  "platform": "...",
  "created_at": "...",
  "packages": {"name": "version"}
}
```

构建开始时在最终 venv 目录旁或目录内写 `.options-monitor-deps-building.json`，记录 owner PID、started_at、dependency hash。complete manifest 写入后删除 building marker。

### 实现要求

1. 在最终 hash 路径直接调用 venv create，保证 `bin/pip`/entrypoints shebang 从一开始就是最终路径。
2. 不再调用 `build_venv.rename(shared_venv)`。
3. `_shared_venv_valid()` 必须校验：
   - complete manifest 可解析且 state/context/hash 匹配；
   - building marker 不存在；
   - Python executable 存在；
   - `sys.executable` 存在；
   - package manifest 与 lock 一致；
   - 最小 import smoke 通过。
4. 只有 valid 后调用 `_link_release_venv()`。
5. incomplete recovery：
   - owner PID 活跃：返回 `runtime_prepare_busy`，不删除；
   - owner dead：记录 recovery operation，删除 incomplete dir 后重建；
   - 删除失败：失败状态带 remediation，不继续 link。
6. 兼容旧 timestamp marker：旧 cache 可执行一次 validation 后升级为 JSON manifest；验证失败则按 stale cache 处理，不直接信任。

### Crash-point tests

每个点模拟异常并重试：

1. 创建目录后；
2. 创建 venv 后；
3. runtime/server install 后；
4. package validation 后；
5. complete manifest 写入前；
6. complete 后、release symlink 前。

另测：

- 同 hash 活跃 owner；
- dead owner；
- malformed manifest；
- lock SHA mismatch；
- `bin/pip` shebang 指向最终 hash 路径；
- release symlink 永不指向 incomplete dir；
- cache hit ≤ 2s。

### Gate 4 验收

- 所有 crash point 可重试；
- 无 rename 路径残留；
- `venv/bin/pip --version` 与 `venv/bin/python -m pip --version` 均成功；
- cache hit 和 rollback 到前一 release 均成功；
- 不自动删除历史无关 cache。

### 回滚

代码回滚前保留旧 release 指向的已验证 venv。新 JSON marker 兼容逻辑必须保证旧版本不会误用半成品；若旧版本只检查 marker 存在，building 阶段不得创建 complete marker。

---

## Slice 5：安全地缩短 GitHub Release/CI

### 目标

消除 `resolve-tag` 的独立 runner 调度和重复 dependency download，但保持现有权限与 required checks。

### 前置事实门

实施前记录 GitHub branch protection 当前 required check names。若无法读取，禁止删除或改名 Guardrails/Agent Plugin jobs，只做不改变 check identity 的 setup cache 优化。

### 文件边界

- `.github/workflows/release-from-version.yml`
- `.github/workflows/_release-reusable.yml`
- `.github/workflows/release.yml`
- `.github/workflows/guardrails.yml`
- `.github/workflows/agent-plugin.yml`

### 实现要求

1. `_release-reusable.yml` 的 `tag` input 改为可选：
   - tag-triggered `release.yml` 继续显式传 `${{ github.ref_name }}`；
   - VERSION-triggered workflow 不再创建 `resolve-tag` job，由 reusable release job checkout 后从 VERSION 解析 tag，并输出给后续 metadata/publish steps。
2. metadata validation 仍使用 `release_check.py --tag <resolved-tag>`，不能只拼字符串后直接发布。
3. Guardrails、Agent Plugin、Release 保持独立 workflow/job 和原权限：
   - Guardrails：`contents: read`
   - Agent Plugin：`contents: read`
   - Release：publish job `contents: write`
4. 在各自 `actions/setup-python` 使用 pip download cache，`cache-dependency-path` 覆盖相关 requirement/constraint/lock 文件。
5. 不在本 slice 建跨 workflow venv artifact，不让只读 validation 在 write job 中运行。
6. 不删除 Release 内的 smoke/plugin checks，除非未来有 GitHub-native、同 commit、不可伪造的 required artifact handoff 设计；本 slice不做该复杂化。

### 测试/验证

- YAML/schema validation；
- tag push path；
- VERSION push path；
- VERSION/tag mismatch 必须失败；
- publish step 之前的 job token permissions 检查；
- 普通 PR 仍出现原 Guardrails/Agent Plugin required checks；
- 10 次 workflow timing median/P90。

### Gate 5 验收

- `resolve-tag` runner job 消失；
- release median ≤ 62s，P90 ≤ 75s；
- required check names/branch protection 无变化；
- validation job 未获得写权限；
- release artifact 内容与目标 commit 一致。

### 回滚

恢复原 `resolve-tag` caller/reusable input；cache 设置可独立保留或回滚。不得通过关闭 branch protection 解决 check 问题。

---

## Slice 6：`uv` 独立 canary rollout

### 目标

在 dependency lock 和 venv lifecycle 已稳定后，用 `uv` 缩短 dependency-change slow path；不把它立即变成硬依赖。

### 前置条件

- Gate 2、3、4 全部通过并已发布；
- 至少一次 pip dependency-change canary 成功；
- production host change 获得明确确认；
- 已记录 cron/service 实际用户、PATH、proxy/index/cache 环境，secret 只验证存在性不输出值。

### Rollout 阶段

#### 6A：安装与 readiness（需确认）

- 用明确 owner 和固定版本安装 `uv`；记录版本、路径、升级责任和卸载方法。
- 在 service 相同用户/环境运行：version、Python 3.11、index/proxy mapping、disposable venv install smoke。

#### 6B：`auto` canary（需确认）

- 保持 `OM_UPGRADE_INSTALLER=auto`；在 disposable dependency hash 上构建。
- 对比 pip/uv package manifest，必须 name/version 完全一致。
- 不覆盖当前 release venv，不删除 pip cache。

#### 6C：受控真实升级（需确认）

- 执行一次依赖变化 release；观察 status、构建耗时、service health、rollback。
- 失败时 `auto` 回退 pip；记录 `fallback_from=uv`。

#### 6D：是否 strict 的决策门

默认**不启用** `OM_UPGRADE_INSTALLER=uv`。只有以下条件全部满足并再次明确确认才启用：

- 至少 3 次 uv canary 成功；
- P50 ≤ 180s，且较 pip 改善 ≥ 30%；
- package manifest 无差异；
- fallback 与 rollback 演练成功；
- service 环境能稳定发现固定版本 uv。

### 回滚

- 移除/改回 `OM_UPGRADE_INSTALLER=auto|pip`；
- 复用已验证的旧 complete venv；
- 不需要改 release symlink 之外的生产状态；
- uv 卸载是独立操作，不作为应用回滚前置条件。

---

## Slice 7（可选）：Feishu check 冷启动观测优化

### 进入条件

只有 Gate 1–6 完成后，且正常升级 3 次观测仍显示 Feishu check 占比 > 30%，才进入。

### 约束

- 不允许用 `find_spec` 替代真实 import validation。
- 可选择在同一 upgrade process 复用一次 import 结果，或把 SDK import validation 合并到已存在的 server import smoke。
- 仍需至少一次真实 client module import；不得发送真实消息或建立未授权连接。

### 验收

- check 从约 9s 降至 ≤ 4s；
- 缺包、transitive import error 仍能被检测；
- 不改变通知行为。

---

## Slice 8（可选）：post-switch config rebuild 去重

### 进入条件

先证明 pre-switch 与 post-switch 的 input、compiler version、输出 hash 和验证职责完全相同。任何一项不同则保留两次执行。

### 推荐方向

- pre-switch 生成 immutable config artifact 和 hash；
- post-switch 只验证 current release 使用的是同一 artifact/hash，不重新 build；
- 若 post-switch 的意义是用新 release compiler 再验证，则不能删除，只能优化内部 cache。

### 验收

- 正常升级 config 阶段节省 ≥ 2s；
- 配置漂移、旧 compiler/new compiler 差异仍能阻止错误切换；
- 不修改生产 config 内容。

## 6. 实施顺序与停止条件

```text
Slice 0 baseline
  -> Slice 1 test/preflight
  -> Slice 2 timeout/recovery
  -> Slice 3 dependency lock + single install
  -> Slice 4 shared venv lifecycle
  -> Slice 5 GitHub CI
  -> Slice 6 uv canary（需生产确认）
  -> Slice 7/8 optional
```

每个 slice 必须独立 commit/PR、独立验收、独立回滚。禁止为了“顺手”提前实现后续 slice。

立即停止后续推进的条件：

- full tests 数量下降或 assertion 被弱化；
- upgrade status 无法区分 timeout/cleanup failure；
- 出现活跃安装子进程或 incomplete venv 被 link；
- package manifest 与 lock 不一致；
- required checks/permissions 发生非预期变化；
- 性能未达该 slice 的最低改善，或错误率/恢复时间变差；
- 需要修改 production config/service，而未获得明确确认。

## 7. 测试矩阵

| 区域 | Happy path | Failure path | Recovery/compatibility |
|---|---|---|---|
| Test sleep | 无真实等待、assertions 保留 | cooldown/error payload | 独立 cooldown test 仍断言 30s |
| Command executor | success/nonzero | timeout/spawn/TERM ignored/KILL | retry、锁释放、operation durability |
| Dependency lock | runtime/server install | stale lock、resolver failure | 同 tag reproducible、pip/uv manifest equal |
| Shared venv | fresh build/cache hit | crash at six points、bad manifest、disk/permission error | dead/live owner、old marker compatibility、rollback |
| CI | PR/main/VERSION/tag | metadata mismatch、validation failure | stable check names、least privilege |
| uv rollout | auto canary | uv unavailable/fails | pip fallback、old venv rollback |
| Optional checks | Feishu/config success | missing SDK/config drift | 保留真实 validation semantics |

## 8. 预期收益

### 高确定性

- 本地 preflight：约 **99s → 35–45s**，主要来自移除两个真实 30s sleep。
- GitHub VERSION release：约 **73s → 58–62s median**，主要来自取消独立 `resolve-tag` runner 和 download cache。
- server cold install：避免重复解析/安装 runtime；具体收益由 canary 测量。

### 可靠性收益

- dependency install timeout 从“可能留下 40 分钟级残留且状态不完整”变为 10 分钟硬边界、结构化失败和可重试。
- shared venv 的 `bin/pip` shebang 不再指向已 rename 的临时目录。
- 同一 release tag 的 dependency versions 固定、可审计、可回滚。

### 暂不承诺

- 正常 cache-hit 升级目前约 20s，已不属于首要瓶颈；在不完成 Slice 7/8 证据门前，不承诺进一步压缩。
- hosted GitHub runner 有冷启动波动，只按 median/P90 验收。

## 9. Planreview finding closure mapping

| Finding | 本计划关闭方式 |
|---|---|
| PR-001 timeout/recovery 未定义 | Slice 2 定义 outcome、process group termination、cleanup/status/retry tests |
| PR-002 final venv 路径破坏原子性 | Slice 4 使用 final path + building/complete manifest + link gate + crash tests |
| PR-003 lock 被后置 | Section 4.4 + Slice 3 将 release-pinned lock 设为 cache/uv 前置条件 |
| PR-004 uv rollout 无 fallback/rollback | Slice 6 拆成 readiness、auto canary、真实升级、strict 决策门 |
| PR-005 CI 权限/required checks | Slice 5 保持独立 workflow/permissions/check identity，只优化 setup 与 resolve job |
| PR-006 server include 隐式契约 | Slice 3 生成可独立安装的 server superset lock，并加 contract tests |
| PR-007 scope 过宽 | 6 个独立 slice，逐 slice gate、stop condition、rollback；P2 改为证据驱动 optional |

## 10. 最终执行建议

先只批准 **Slice 0 + Slice 1**。这是最低风险、最高确定性收益，可快速把本地发布检查减少约一分钟。

Slice 1 通过后再单独进入 Slice 2。生产 `uv` 安装、环境变量或真实升级均不在首批执行范围内，届时必须再次获得明确确认。

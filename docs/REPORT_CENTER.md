# Report Center

报告中心为所有报告类型提供一份**可枚举的注册表**、统一的**运行/历史入口**和**可导出的调度描述**。它不重写现有 tick、launchd 或报告实现。

## 定位与边界

- 统一登记：报告类型、频率、生成入口、渲染入口、状态目录、配置开关、迁移状态。
- 统一入口：`om report list / run / history / show / schedule`。
- 统一产物：迁移后的报告写入 `<runtime_root>/reports/<report_id>/<run_id>/`。
- 不改现有行为：未迁移报告仍由原实现运行；`om report run <legacy_id>` 返回 `REPORT_NOT_MIGRATED`，并给出原实现位置。
- 不接管现有调度：`local.options-monitor.code-health-review`、`com.options-monitor.code-entropy.daily`、`com.options-monitor.code-entropy.weekly` 保持原样。

## 注册表

定义在 `src/application/reports/registry.py` 的 `REPORT_REGISTRY`。每条声明：

| 字段 | 含义 |
|---|---|
| `report_id` | 稳定字符串，CLI 的唯一标识 |
| `display_name` / `description` | 中文展示名与一句话说明 |
| `schedules` | 一到多条调度：`daily` / `weekly` / `tick` / `manual`，含时区、时间、ISO weekday、launchd label、plist 路径 |
| `run_entry` | 生成入口（模块/函数或脚本） |
| `render_entries` | 渲染入口与产物格式线索 |
| `state_dir` | 现有或新的状态/历史目录约定 |
| `config_keys` | 控制该报告的配置键（逻辑点路径；没有则为空） |
| `migrated` | 是否由报告中心执行。当前只有 `config_review` 为 `true` |
| `retention` | 报告中心自己的保留策略（见下） |
| `legacy_probe` | 未迁移报告的上次运行读取方式 |

当前登记：

| `report_id` | 展示名 | 频率 | 迁移 |
|---|---|---|---|
| `daily_brief` | 每日决策简报 | tick 触发 | 否 |
| `close_advice` | 平仓建议报告 | tick 触发 | 否 |
| `code_health` | 代码健康每日巡检 | 每日 09:17（本机 launchd） | 否 |
| `code_entropy` | 代码熵评分卡 | 每日 04:20 + 每周日 01:20（本机 launchd） | 否 |
| `config_review` | 周度配置清单报告 | 每周日 20:00（Asia/Shanghai，远端 systemd） | 是 |

## CLI

```bash
om report list [--json] [--configs-root PATH]
om report run <report_id> [--dry-run] [--no-deliver] [--json] [--configs-root PATH] [--prices-file PATH] [--candidate-evidence-file PATH]
om report history <report_id> [--limit N] [--json]
om report show <report_id> <run_id> [--artifact NAME] [--json]
om report schedule [--format text|json]
```

- `list`：报告类型、频率、配置开关状态、上次运行与结果。默认不读本机配置副本；显式指定 `--configs-root` 时才从该目录的 `system.json` 的 `defaults` / `markets` 读取开关。未迁移报告用 `legacy_probe` 做只读探测。
- `run`：只对 `migrated: true` 的报告生效。`--dry-run` 只生成并打印，不创建运行目录，不写历史；`--no-deliver` 只在本地生成报告，跳过外部投递。
- `history`：按 UTC 时间倒序读取 `<runtime_root>/reports/<report_id>/*/run.json`。
- `show`：默认显示 markdown 产物；`--artifact report.json` 可查看结构化产物。
- `schedule`：输出每个报告的调度条目；JSON 里包含 launchd label、`StartCalendarInterval`、plist 路径是否存在、迁移状态和保留策略。

## 产物目录与运行清单

```
<runtime_root>/reports/<report_id>/<run_id>/
  run.json
  report.md
  report.json
  candidate_evidence.json  # config_review 的本次候选证据投影
```

`run_id` 形如 `20261112T014229Z-60951035`，以 UTC 时间开头，后接随机后缀，保证同秒并发也不会冲突。

`run.json`（`schema_version: report_run.v1`）记录：

- `report_id` / `run_id` / `status`（`running` / `succeeded` / `failed`）
- `started_at_utc` / `finished_at_utc` / `trigger`（`manual` / `schedule`）
- `command`：实际执行入口
- `artifacts`：文件名、相对路径、字节数、SHA-256
- `summary`：报告摘要（结构化）
- `delivery`：投递结果（`report_delivery.v1`；见下）
- `error`：失败时的 `code` / `message`

失败运行也会落 `run.json`，便于 `history` 复盘；生成失败不会留下半份 `report.md`。

## 调度描述与模板

`om report schedule --format json` 导出注册表声明，不代替目标主机的真实调度状态。`config_review` 的条目为 `frequency=weekly`、`trigger=systemd`、`weekday=7`、`hour=20`、`minute=0`、`timezone=Asia/Shanghai`。ISO weekday 是 `1=Monday .. 7=Sunday`。

周报模板位于 `deploy/systemd/options-monitor-report-config-review.service.template` 和 `deploy/systemd/options-monitor-report-config-review.timer.template`。timer 使用 `OnCalendar=Sun *-*-* 20:00:00 Asia/Shanghai`、`Persistent=true`；真实下次运行时间以目标机 `systemctl list-timers` 读回为准。持久 timer 补跑前先核对本次投递回执，避免重复手动触发。

其他报告继续使用原调度。旧周报 launchd 模板仅供显式本地兼容使用；生产迁移步骤见下文。`plist_present` 对仍声明 launchd 的报告只检查文件，不加载、不修改。

新增报告需登记注册表并补充相应调度入口与验证，不自动安装服务。

## 保留策略

`output_runs` 的现有默认清理策略是 **14 天 / 200 次**（`service_cleanup.py` 的 `output_runs_keep_days` / `output_runs_keep_count`）。周报套用这个策略只能留下约 2 份，无法做跨月/跨季对比，因此报告中心使用**按报告类型声明的独立策略**：

- `config_review`：`keep_count = null`，追加保留全部周报与投递回执，不自动清理。每周一份，定期检查 `<runtime_root>/reports/config_review/` 大小；容量预算或删除策略需另行安排。
- 未迁移报告：继续由各自现有目录/清理策略管理，报告中心不接管。

本期只**声明**策略，不实现自动删除：删除属于需要单独授权的操作，且旧报告尚未迁移。后续若要执行，应在 `service_cleanup` 增加 `reports/` 类别并沿用 `run.json` 身份校验，而不是在每次 `run` 后静默删除。

## 周度配置结论与候选证据

`config_review.v2` 先显示 **建议调整 / 观察 / 无需调整**，再列 A、B 和配置清单。指标 owner 是 `src/application/reports/strike_metrics.py`；配置继承、价格选择、渲染和落盘仍使用原报告 owner。[完整定义与取舍](WEEKLY_CONFIG_REVIEW_COMPLETION_DESIGN.md)。

- A 读取 `output_runs/<run_id>/accounts/<account>/state/opening_candidate_snapshot.json`，按封存时间选取 **Asia/Shanghai 上一完整自然周**（周一 00:00 含，到下周一 00:00 不含）。只消费封存决定，不重跑策略。显式 `--candidate-evidence-file` 使用该文件独立的数据集，不与本机记录混合。
- 四种方向码为 `put_strike_below_min`、`put_strike_above_max`、`call_strike_below_min`、`call_strike_above_max`。同一观测同一方向去重，总量取观测并集。观测身份是 run/account/market/candidate；不同 tick 或账户重复扫描仍是不同观测，不是独立交易机会。
- A 展示市场、标的、方向、独立合约和日期；另按当前配置的同值边界统计。策略的有效阈值也可能来自价格窗口或成本，“同值”不能证明用户配置是唯一原因。
- 新快照的可选哈希字段 `strike_boundary_reason_codes_version: 1` 声明方向码能力。无能力标记、遗留 `hard_strike`、失败/未知 scope、缺失或不兼容文件不能证明零；未知计数为 `null`，正文写“该期无数据”或部分覆盖。无标记且没有方向码的文件提前记录为不支持，不计入已验证观测；无标记但有方向码的文件仍走原 loader，合格时保留部分观测。所有被采纳记录继续验证原始快照及依赖。只有完整、有标记的数据能报告真实 0。覆盖仅指留存记录，不证明每个历史 tick 都已保留。
- B 的 signed distance 为 `(spot - boundary) / boundary * 100`；max 边界压力等于此值，min 边界压力取相反数。v1 以现价为分母，v2 明确改为边界分母。缺价仍保留边界行。

规则常量随版本固定。N 是有效封存 policy 与当前边界同值的可比较观测；R 是方向与 threshold 同值的拒绝；C 是 R 中只因四种 strike 方向码拒绝的观测。多原因含非 strike 拒绝的不计入 C。共同门槛：启用且边界有效，价格有效、时间有时区且不在未来/不超过 96 小时，相关 A 完整，N ≥ 30，观测日期 ≥ 3。

| 结论 | 共同门槛外的条件 |
|---|---|
| 建议调整 | 压力 ≥ 15%，C ≥ 20，C/N ≥ 30%，C 的独立合约 ≥ 5、日期 ≥ 3；仅建议人工复核，不自动给替换行权价 |
| 无需调整 | 压力 ≤ 5%，R/N < 10% |
| 观察 | 其余情形，包括数据不足、不可比较或边界停用 |

任一启用边界达到建议条件，报告为“建议调整”；全部启用边界均合格且无数据缺口才是“无需调整”；其余为“观察”。停用边界展示但不驱动整体结论。阈值比较使用未四舍五入的值。

## 配置与价格来源（`config_review`）

默认读取 `<runtime_root>/config.hk.json`、`config.us.json`，经 `src/application/agent_tool_config.py::load_runtime_config` 校验 generated identity 与市场。`config.yaml` 是人工权威源，runtime JSON 是 build 后扫描实际消费的快照；报告读取后者，避免重新构建 YAML 或叠加另一份本机默认值。线上运行使用 `/var/lib/options-monitor`，不读取本机 `configs/user.*.json`。

显式 `--configs-root PATH` 仍可审阅该目录的 `user.hk.json` / `user.us.json` 和本地 system defaults，供开发或测试；runtime 缺失/损坏不会自动切回此模式。

默认价格来自 `output_runs/<run_id>/state/required_data_snapshot_manifest.json`：每市场按有效 `sealed_at_utc` 从新到旧选一个完整扫描；tick metrics 必须包含该市场，且至少一个 `accounts` 项 `ran_scan=true` 与同账户的 `scan_outcomes=true` 正向匹配。失败、缺少成功证据、不完整或未来清单不算成功扫描。

通过 `src/application/required_data_snapshot.py::resolve_frozen_required_data_csv_bytes_batch` 读取校验后的冻结 CSV/共享 blob，不依赖可能已退役的 raw/CSV 副本。行情值取 `spot`，时点取 `spot_observed_at_utc`，不使用目录 mtime、报告时间或封存时间冒充行情时间。历史解码关闭日内 receipt TTL，但仍按既有 **96 小时**报告政策判断过期；长假期间可能全部进入“观察”。缺价、无时区、未来时间和过期都不会当作 0 或新鲜价格。所选市场运行中缺失的标的不再从更老运行拼补。

`report.json` 的 `config_source` / `config_files` / `price_runs` / `price_diagnostics` 与每标的 `source_path` 记录来源。较新扫描不可用而改用旧扫描时显示缺口；完全缺失时保留边界行并标注不可用。默认不读取 daily brief 或另建线上价格文件，也不额外调用 OpenD。

人工固定价格只在显式 `--prices-file PATH` 时生效。接受 `{"prices": {"NVDA": 173.73}}` 或 `{"NVDA": {"price": 173.73, "observed_at_utc": "..."}}`；没有可验证时间的输入仍会被新鲜度规则判为“观察”。

## 远端周度任务安装与切换（需明确授权）

1. 从审查通过的提交安装稳定报告源码，替换 service 模板的 `@DEPLOY_USER@`、`@SOURCE_ROOT@`、`@RUNTIME_ROOT@`、`@PYTHON@`；源码和解释器必须独立于开发 worktree 和主程序 release 清理。保持主程序 symlink、配置、tick 调度不变。可复制现役依赖环境到报告专属目录，只调用其 Python，验证 `sys.prefix` 和插件导入路径；不修改共享缓存环境。
2. service 读取 `/etc/options-monitor/options-monitor.env`；沿现役 materialize helper 从 `/etc/credstore.encrypted` 加载同一组飞书 credential IDs 到该服务专属 `CREDENTIALS_DIRECTORY`，结束时 cleanup。该模板用于已部署 helper 的 Linux 环境；不依赖本机 Keychain，不复制 secret 到源码或日志。[凭证契约](SECRET_STORAGE.md)。
3. 审阅已渲染单元及目标文件，运行 `systemd-analyze verify`，安装 service/timer 后 reload、启用 timer；读回下次触发时间。独立单元不加入通用 service profile 或 managed allowlist，旧版本 drift 可显示 `extra_installed` 警告；确认 `ok=true`、error=0 且没有退役计划。它们的源码更新由独立报告部署维护，不随主程序自动升级。
4. 手动 `systemctl start options-monitor-report-config-review.service`，读回终止码和 `/var/lib/options-monitor/reports/config_review/<run_id>/`。`deploy/systemd/run-config-review-weekly.py` 仅调用公共 report CLI 一次；`delivered` 且有 `message_id` 才允许服务成功。`skipped`、失败或无确认均保留报告/回执并使服务失败；不自动重发。结果不明确时先查消息和回执，再决定重跑，新命令会生成新 run。
5. 核对实际报告配置值、价格时间、飞书消息读回及 timer 后，才 `launchctl bootout` 本机 `com.options-monitor.report.config-review.weekly`，把原 plist 移到 LaunchAgents 之外的时间戳备份；确认 job 查不到、配置文件哈希未变。保留旧源码、输入、用户 `configs/user.*.json` 和报告。远端验收失败时先保留本机调度；需回退时恢复备份 plist 并读回本机运行状态后再停远端 timer。

## 投递

报告注册表的 `delivery` 字段声明投递渠道，默认 `none`；当前只有 `config_review` 声明 `feishu_app`。

- 复用现有飞书应用通道：`src/application/channels/feishu_notification.py` 的 `send_feishu_app_message`（与 daily brief 同一个发送实现），不新增 HTTP 客户端，也不新增凭证。
- 目标与凭证来自现有 Bot 设置：`OM_FEISHU_BOT_APP_ID`、`OM_FEISHU_BOT_APP_SECRET`、`OM_FEISHU_BOT_USER_OPEN_ID`。launchd 不读 shell profile，因此 plist 通过 `OM_ENV_FILE` 指向实例 env 文件（本机为 `.env/options-monitor.env`）。
- 飞书 SDK 来自可选插件；未执行 `om plugins install feishu --confirm`（或源码环境的 pip 安装）时，投递记录为 skipped，报告照常生成。
- 幂等键由 `report_id + run_id` 派生，同一 run 重试不会重复发消息。

`run.json` 的 `delivery` 有三种终态：

| status | 含义 | CLI |
|---|---|---|
| `delivered` | 飞书返回 `message_id`，渠道已接受；目标会话独立读回/用户确认是更强证据 | 退出码 0 |
| `skipped` | 未声明渠道、`--no-deliver`、插件未安装或凭证缺失；`reason` / `message` 明确写出原因 | 退出码 0 |
| `failed` | 已尝试发送但未确认；报告仍完整落盘 | 退出码 1，stderr 输出错误 |

`--dry-run` 不写运行目录，也不投递。投递失败不会回滚或删除 `report.md` / `report.json`。

## 迁移顺序与风险

本期**不迁移任何老报告**。建议顺序与风险如下：

1. `code_health`
   - 风险最低：状态目录、运行记录（`last-run.json`）和产物都在独立目录，launchd 只调用脚本。
   - 迁移风险：需要把状态目录搬到 `<runtime_root>/reports/`，或保留旧目录并让新框架只读；迁移期间必须避免两套 runner 同时写 `run.lock`。
2. `code_entropy`
   - 风险较低：`scorecards.jsonl` 已经是运行历史；但 daily/weekly 两种 mode 共享一个状态目录和 issue 去重账本。
   - 迁移风险：不能改变 `decisions.json` 的 issue 发布身份，否则会重复创建 issue；两个 plist 的 label 和预算要保持。
3. `close_advice`
   - 风险较高：产物在 `output_accounts/<account>/reports`，由 tick 在报价封存事务内生成，且与通知、恢复补报、manifest 校验强耦合。
   - 迁移风险：把产物改写到 `reports/` 会破坏现有账户报告契约和下游读取；正确做法是先让报告中心**读取**现有 manifest，再评估是否改写入路径。
4. `daily_brief`
   - 风险最高：涉及投递状态、失败恢复补报、决策历史修订和 `daily_decision_brief.v1` 身份语义。
   - 迁移风险：任何“只搬目录/只换入口”的做法都会丢失投递确认和恢复身份；应保持 repository 原样，仅登记和只读展示。

## 新增报告 checklist

1. 在 `REPORT_REGISTRY` 增加一条 `ReportDefinition`，声明频率、入口、状态目录、开关、迁移状态与保留策略。
2. 在 `src/application/reports/runner.py` 的 `RUNNERS` 注册生成函数（输入 `ReportRunContext`，输出 `ReportBuild`）。
3. 生成函数返回 markdown/JSON 产物内容，不直接写磁盘；落盘、SHA-256、`run.json` 由 `execute_report` 统一处理。
4. 添加测试：注册表覆盖、生成内容、CLI `run/history/show`、失败路径。
5. 在 `om report schedule` 输出中核对调度字段；需要定时运行就补 plist 模板，仍由人工确认后安装。

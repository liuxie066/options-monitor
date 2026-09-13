# OM Quality Producer 操作契约

OM producer 只读取现有 runtime/intake/ledger/lifecycle 事实，并通过独立
`refresh_cache=True` OpenD 查询取得最终期权持仓。检查不会修改交易事件、
position lots、生命周期 case 或 OpenD 数据。

本地入口：

```text
./om quality refresh --config-key us --config-key hk
./om quality refresh --config-key us --config-key hk --no-deep
./om quality recheck-due --config-key us --config-key hk
./om quality refresh --config-key us --day-end-strict
./om quality status --json
./om quality integrity --config-key us --config-key hk
./om quality integrity-status --json
./om quality cutover --evidence <cutover-evidence.json>
./om quality cutover --evidence <cutover-evidence.json> --apply
./om-agent run --tool quality_status --input-json '{}'
```

首次 baseline 或人工强制权威对账使用默认 `refresh`；15 分钟常规定时器使用
`--no-deep`。后者会继续发布 runtime、ledger、intake、lifecycle 等当前检查，
但只在本地持仓 revision 改变、差异复查到期、日终 deadline 到期或缺少有效
baseline 时访问 OpenD；否则沿用仍在有效期内的最近一次权威 OpenD 证据。

普通 `refresh` 在 cutover 前继续兼容旧的详细生命周期数据。`integrity` 是显式的
全历史 replay，并单独发布 `integrity_status.v1.json`；普通 status 和 gate 不会隐式
触发它。`cutover` 默认只校验证据，只有 `--apply` 才写入不可变激活回执。激活后
第一次普通 refresh 必须同时包含 `us` 和 `hk`，之后单市场日终刷新才可保留另一
市场最近一次 current-only 汇总。激活仍要求两个市场各 14 个合格交易日、零
unexplained/legacy read、静态 consumer inventory 与 deployment-access 证据。

`refresh` 会原子发布：

```text
<OM_RUNTIME_ROOT>/output_shared/state/quality/status.v1.json
<OM_RUNTIME_ROOT>/output_shared/state/quality/control_state.v1.json
```

第二个文件只保存差异首次出现时间、下一次只读复查时间、生命周期首次深对账时间、
市场交易日列表和本地 `position_lots` 控制状态哈希，不保存账户 ID、完整持仓或
OpenD 原始响应。

读取结果使用 `./om quality status --json` 或 `quality_status` Tool Gateway 工具。
HTTP 服务和外部 Hub 接入已退役；本地检查、artifact 和业务门禁继续保留。
静态 consumer inventory 已移除 HTTP reader；旧 inventory 的 cutover 证据或激活回执
不再匹配当前程序，须按现有 cutover 流程重新验证，不能自动改写旧回执。

门禁：

- `OM_QUALITY_ONBOARDED=false` 时 producer 可部署和建立 baseline，但不改变消费者行为；
- 完成生产 baseline 与本地消费者接入验证后设为 `true`；
- 此后 stale artifact 或明确 blocking 结论会阻断 close advice 和正式
  option performance；
- 普通候选扫描不读取该门禁；
- 没有临时 observe/bypass 开关，门禁实现故障通过回滚 producer release 处理。

调度语义：

- 常规 producer 每 15 分钟执行 `refresh --no-deep`；quality-monitored account 从交易日
  `08:30` 到日终窗口每次常规 refresh 都重新读取 OpenD 当前合约条款，避免账本未变但
  公司行动或遗漏 intake 已改变 broker positions 时沿用旧证据；
- `recheck-due` 每 1 分钟只比较控制状态哈希和差异到期时间；无变化时不重建
  artifact，也不访问 OpenD；
- 持仓首次差异保存 `next_recheck_at_utc=+1m`，第二次窗口到 `+5m`；
- 调度器只在到期时再次运行只读 refresh，不在单次进程中 sleep；
- 日终分别在所属市场时区周一至周五 `16:30` 执行
  `refresh --day-end-strict`，首次确定性差异立即阻断；
- 单市场日终刷新保留另一市场最近一次有效数据集，不把未请求市场误删；
- 其余 OpenD 权威查询仍由 baseline、ledger 变化、差异到期、日终或人工强制触发。

systemd renderer 默认不改变现有部署。生产准备时显式加入：

```text
./om service render \
  --target systemd \
  --config-yaml <config.yaml> \
  --runtime-root /var/lib/options-monitor \
  --env-file /etc/options-monitor/options-monitor.env \
  --include-quality-monitoring \
  --include-secret-credentials
```

该选项生成：

- `options-monitor-quality-refresh.timer`：15 分钟常规刷新；
- `options-monitor-quality-recheck.timer`：1 分钟轻量到期探测；
- `options-monitor-quality-day-end-us.timer`：美东 `16:30`；
- `options-monitor-quality-day-end-hk.timer`：香港 `16:30`。

renderer 只生成文件和安装命令，不会自行写 `/etc`、启用 timer 或启动服务。

## Public source snapshot contract

`datasets[*].source_snapshots[*]` is the public
`investment.quality_status.v1` boundary. `OpenDOptionSnapshot.public_source_snapshot()`
must be an explicit allowlist projection. The repaired producer must emit exactly
these eight fields: `provider`, `snapshot_id`, `observed_at_utc`, `complete`,
`refresh_cache`, `account_fingerprint`, `environment`, and `market`. The current
`origin/main` implementation still spreads four internal fields into this object;
that is the defect being removed.
`source_currency` and `payload_sha256` are permitted by the local schema but
are not emitted by this producer today.

OpenD position-input fields such as `scope`, `completeness`, `quality`, and
`source_as_of_utc` remain in the internal `snapshot_input` used by position
checks. They must never be deleted or cleared merely to make publication pass,
and they are never copied into a public source snapshot. The current
`sourceSnapshot` schema has no source-level `extensions` slot; dataset-level
extensions must not be used as an undocumented escape hatch. Any future
producer-specific field requires a compatible change to the OM-owned local
schema and regression evidence for its consumers.

The relevant call chain is:

```text
OMQualityService._refresh
  -> OpenDOptionPositionAdapter.fetch
  -> build_opend_runtime_check / build_position_dataset
  -> public_source_snapshot (position dataset path)
  -> validate_payload
  -> artifact_repository.write_atomic
```

Normal refreshes, `source_ok=false` incomplete/error results, and explicit
integrity refreshes all use the same public projection. The repair therefore has
one code owner and does not change OpenD queries, position comparison, quality
decisions, artifact paths, or production write semantics.

`status.v1.json` and `control_state.v1.json` are each written atomically but are
not one cross-file transaction. Control state is persisted before payload
validation; when validation fails, the previous status artifact remains and the
control state may already contain the new probe metadata. This existing split is
documented residual risk for the quality-service maintainer's next
`quality-refresh` reliability work unit, not part of this minimal contract
repair. Carried-forward snapshots are likewise not sanitized in this change;
the quality artifact owner must address that in a separate data-integrity work
unit only if an existing artifact is shown to contain undeclared source fields.

Validation must include an adapter key-set regression and quality-service tests
with enriched `snapshot_input` for both complete and incomplete/error paths, plus
the normal release preflight and the explicit `tests/quality/*` suite. A deployed
oneshot refresh is successful only when its exit result is success and the
published status artifact validates and can be read back; timer
activity, or process presence alone is insufficient. Publishing and remote
upgrade remain separate authorization gates. A schema extension is not part of this producer-side repair.

## Hotfix scope and implementation slices

目标是让带有内部 `snapshot_input` 的 OpenD 结果重新能够通过现有
`investment.quality_status.v1` 校验并发布，同时保持质量判断和生产扫描行为不变。
验收信号是：公共 source snapshot 的键集合严格等于上述八个字段；完整和不完整/错误
结果都能完成服务级 schema 校验；quality-refresh oneshot 以成功结果退出并能读回
有效 status artifact。

实现只分两个行为切片：

1. 在 `OpenDOptionSnapshot.public_source_snapshot()` 保留显式八字段 allowlist，
   不修改或清空 `snapshot_input`，也不改变 `fetch`、position checks 或 artifact writer。
2. 在 adapter 和 quality service 的公共边界补回归：用带内部哨兵字段的完整、
   不完整/错误 snapshot 验证精确键集合和 schema 通过；保留旧的八字段 snapshot
   仍可通过。测试必须走 `build_position_dataset`/service 真实发布路径，而不是只测
   一个未被调用的 helper。

以下不属于本 hotfix：修改本地 schema、增加 source-level `extensions` 或新版本、
改变 OpenD 查询或消费者、重写跨文件事务、清洗历史 carry-forward artifact、修改
发布工作流。发布前只读校验现有 status artifact；若发现历史 artifact 已含未声明字段，
保留原始事实并另开数据修复工作，不在本 hotfix 中覆盖它。

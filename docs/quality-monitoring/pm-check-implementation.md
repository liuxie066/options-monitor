# PM 质量检查实现映射

- **状态**：代码与本地验证完成；生产 onboarding 待 Phase 5
- **日期**：2026-07-26
- **规范来源**：[check-matrix.md](check-matrix.md)

本文把规范中的 PM 检查 ID 映射到唯一实现入口、确定性测试和门禁范围。它是 Phase 2 的完成证据，不改变检查矩阵的业务语义。

## 运行检查

| ID | 实现入口 | 当前证据 | 结论边界 |
|---|---|---|---|
| `RT-PM-001` | Hub `HostWatchdog` 检查 `portfolio-management.service`；Hub producer pull 检查 PM HTTP/auth/Schema/freshness | Hub `tests/test_phase4_integration.py`、producer client tests；PM `tests/test_service_http.py` | systemd 与 HTTP 两类独立反证均 fail closed；PM `/health` 不代表业务数据可信 |
| `RT-PM-002` | `src/app/quality/service.py::PMQualityService._runtime_checks`；`src/app/quality/futu_evidence.py::evaluate_receipt_freshness` | `tests/test_pm_quality.py`、`tests/test_futu_sync_evidence.py` | 周一至周六 08:10、周一至周五 17:10，宽限 15 分钟；明确失败立即 fail，超期旧回执不得冒充当前成功 |
| `RT-PM-003` | `src/app/quality/service.py::PMQualityService._runtime_checks`；`src/app/quality/futu_evidence.py::source_receipt_complete` | `tests/test_pm_quality.py`、`tests/test_futu_balance_sync_service.py` | 要求显式唯一账户、REAL、CNH→CNY、`refresh_cache=True`、账户已验证、分页完整、完整 position snapshot 和 payload digest |

`RT-PM-001` 的 host 与 HTTP 证据由 Hub 分开取得；任一证据缺失都会形成独立 blocking 结论。PM producer 不读取 systemd，也不通过本地 HTTP 反向调用自己。

## 数据检查

| ID | 实现入口 | 确定性回归证据 | `blocked_consumers` |
|---|---|---|---|
| `PM-ACC-001` | `src/app/quality/futu_evidence.py::resolve_account_mappings`、`source_receipt_complete` | missing/duplicate/SIMULATE/market/currency 及两账户碰撞测试 | 对应账户同步、正式 NAV |
| `PM-SRC-001` | `FutuOpenApiBalanceProvider.fetch_portfolio`、`FutuBalanceSyncService._public_source_metadata`、`source_receipt_complete` | forced refresh、分页、账户验证、完整 position snapshot、payload hash、异常空快照测试 | holdings/cash/MMF |
| `PM-SRC-002` | 同上 | CNH source 与 CNY normalized evidence、错误币种 fail-closed 测试 | cash/MMF/NAV |
| `PM-POS-001` | `src/app/futu_sync_reconciler.py::FutuSyncReconciler._read_and_compare` | immediate match、30 秒只读重查、persistent quantity mismatch | NAV、持仓报告 |
| `PM-POS-002` | `FutuOpenApiBalanceProvider._fetch_security_types`、`FutuBalanceSyncService._build_position_diff` | unknown classification、short/unknown side、duplicate identity、empty snapshot guard | holdings sync、NAV |
| `PM-COST-001` | `FutuSyncReconciler._read_and_compare` | Decimal 存储精度比较、persistent mismatch；NAV 不依赖 cost dataset | 成本/盈亏报告 |
| `PM-CASH-001` | `FutuOpenApiBalanceProvider._cash_from_row`、`FutuSyncReconciler._cash_verdict` | `cash` presence、0、missing、写后金额比较 | cash_like、NAV |
| `PM-CASH-002` | `FutuOpenApiBalanceProvider.CASH_COLUMNS`、`_validate_authoritative_balances` | `available_funds`/`withdraw_cash`/`power` 不得回退测试 | cash sync、NAV |
| `PM-MMF-001` | `FutuOpenApiBalanceProvider._mmf_from_row`、`FutuSyncReconciler._cash_verdict` | `fund_assets` 0/missing/invalid、写后金额比较 | cash_like、NAV |
| `PM-CASHLIKE-001` | `PMQualityService._account_datasets` | 两项 trusted、单项异常 partial、两项 unavailable 和 stale 传播 | 正式 NAV、流动性报告 |
| `PM-SYNC-001` | `FutuBalanceSyncService.sync_portfolio`、durable receipt stages | positions/cash/MMF 全阶段成功、partial-write、写前失败回执 | 对应写入阶段数据集 |
| `PM-SYNC-002` | 同一 durable receipt 的 `sync_run_id`、`source_snapshot_id`、三阶段 | restart read、同 generation、balance-only receipt 不等于完整 portfolio sync | NAV、正式报告 |
| `PM-SYNC-003` | `FutuSyncReconciler` | immediate readback、30 秒只读重查、persistent mismatch、repository unavailable | 对应数据集消费者 |
| `PM-PRICE-001` | `src/app/quality/evidence.py::valuation_quality_evidence`、`PMQualityService._valuation_and_nav_datasets` | missing/stale/fallback 分离测试 | NAV、日报 |
| `PM-FX-001` | 同上 | 非 CNY 持仓缺少 fact-time FX 时 unavailable；不使用当前汇率补历史证据 | NAV、历史/业绩报告 |
| `PM-NAV-001` | `src/app/quality/policy.py::nav_gate`、`assert_official_nav_write_allowed` | holdings/cash/MMF/prices/FX/finality；旧同步回执本地门禁 fail closed | 正式 NAV、日报、业绩报告 |
| `PM-NAV-002` | `PMQualityService._nav_history_dataset`、repository duplicate audit | duplicate/no-audit/clean；finality 保存与重启回归 | 历史 NAV、业绩报告 |

## 来源、发布和门禁边界

| 能力 | 实现 | 约束 |
|---|---|---|
| OpenD 权威查询 | `FutuOpenApiBalanceProvider.from_account` | 显式 account-scoped `acc_id`；查询前验证 OpenD account list；生产只允许 REAL |
| 来源快照 | `FutuBalanceSnapshot` / `FutuPortfolioSnapshot` | 公开 evidence 不含 acc_id、金额或持仓；只公开 fingerprint、完整性标志和 payload SHA-256 |
| 同步 receipt | `src/app/futu_sync_evidence.py` | latest/history 原子持久化；写前明确失败也覆盖 latest 为脱敏失败事实 |
| 写后对账 | `src/app/futu_sync_reconciler.py` | 首次比较；仅在不一致时等待 30 秒后只读重查，不重复业务写 |
| Artifact | `src/app/quality/artifact.py` | 原子发布已校验 V1 payload |
| CLI/HTTP | `pm quality status/refresh`、`GET /quality/status` | status/HTTP 只读已发布 artifact；refresh 只写控制面 artifact，不触发 OpenD 或业务写 |
| 本地 NAV 门禁 | `src/app/quality/policy.py` | onboarding 后在权威写入边界执行；要求当前调度窗口内的完整本地 receipt，不依赖 Hub |

## 本地质量基线

- canonical Schema 校验：通过；
- focused PM 质量/OpenD/receipt/mapping 回归：`46 passed`；
- 完整 pytest：`761 passed`；
- touched Ruff 与 `git diff --check`：通过；
- 当前 PM 质量分支提交：`c67ccf4`；
- 目标版本：`0.1.27`。

生产只读 canary、真实 OpenD baseline、失败数据重跑、Hub onboard、真实告警/恢复和 rollback 属于 Phase 5，不能由本地测试替代。

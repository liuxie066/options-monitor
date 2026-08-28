# OM 质量检查实现映射

- **规范来源**：[investment-quality check matrix](https://github.com/liuxie066/investment-quality/blob/main/docs/quality-monitoring/check-matrix.md)

本文把规范中的 OM 检查 ID 映射到当前实现入口、确定性测试和门禁范围，不改变检查矩阵的业务语义。

## 运行检查

| ID | 实现入口 | 当前证据 | 本地结论边界 |
|---|---|---|---|
| `RT-OM-001` | `src/application/quality/runtime_checks.py::build_runtime_checks` | `tests/quality/test_om_quality_service.py` | 复用 `runtime_status` service profile；Host systemd 独立证据由 Watchdog 补充 |
| `RT-OM-002` | `src/application/quality/runtime_checks.py::build_runtime_checks` | `tests/quality/test_om_quality_service.py` | 按 account/source 判断 listener heartbeat、stage、last error |
| `RT-OM-003` | `src/application/quality/runtime_checks.py::build_runtime_checks` | `tests/quality/test_om_quality_service.py` | 读取现有 timer/run receipt；Host timer 独立证据由 Watchdog 补充 |
| `RT-OM-004` | `src/application/quality/position_checks.py::build_opend_runtime_check` | `tests/quality/test_om_quality_service.py`、`tests/quality/test_om_quality_checks.py` | 要求 REAL、显式账户、`refresh_cache=True`、snapshot complete |

## 数据检查

| ID | 实现入口 | 确定性回归证据 | `blocked_consumers` |
|---|---|---|---|
| `OM-INT-001` | `src/application/quality/intake_checks.py::build_trade_intake_datasets` | service fixture 覆盖 pending/heartbeat/checkpoint facade | `option_position_report`、`lifecycle`、`close_advice` |
| `OM-INT-002` | 同上 | service fixture 覆盖 failed/unresolved facade | 同上 |
| `OM-INT-003` | 同上 | service fixture 覆盖 reconciliation preview/window completeness | `option_position_report`、历史成交消费者 |
| `OM-LED-001` | `src/application/quality/ledger_checks.py::build_ledger_datasets` | `test_full_replay_mismatch_blocks_position_consumers` | `option_position_report`、`lifecycle`、`close_advice` |
| `OM-LED-002` | 同上 | `test_duplicate_broker_identity_with_economic_conflict_is_blocking` | 同上 |
| `OM-POS-001` | `src/application/quality/position_checks.py::build_position_dataset` | schema-valid service fixture；OpenD completeness 回归 | `option_position_report`、`lifecycle`、`close_advice` |
| `OM-POS-002` | 同上 | 持仓 code lineage 对比当前 snapshot 条款；换仓反例、lifecycle 优先级、Scheduled Tick account/market gate、transient、5 分钟 persistent 回归 | 同上 |
| `OM-LCY-001` | `src/application/quality/lifecycle_checks.py::build_lifecycle_datasets` | 周末/假日 deadline；11 条 stale 固定回归 | `lifecycle`、`close_advice` |
| `OM-LCY-002` | 同上 | external adjustment 与 legacy gap 分离回归 | 受影响的 `lifecycle`、`close_advice` |
| `OM-LCY-003` | 同上 | legacy history 独立 dataset 回归 | 受影响历史报告 |
| `OM-HSYNC-001` | `src/application/quality/service.py::_holdings_sync_dataset` | service fixture 覆盖 disabled/intent result facade | 仅正股刷新时效视图；不替 PM 判定数据可信度 |

## 发布、读取和门禁边界

| 能力 | 实现 | 验证 |
|---|---|---|
| 原子 artifact | `src/infrastructure/quality/artifact_repository.py` | schema-valid service 发布测试 |
| 控制状态 | `src/infrastructure/quality/control_state_repository.py` | transient→persistent、首次 deep reconcile 测试 |
| OpenD 只读快照 | `src/application/quality/opend_position_adapter.py` | fake adapter、请求市场隔离与 position/lifecycle 测试 |
| CLI | `src/interfaces/quality/cli.py` | 复用同一 service/artifact |
| HTTP | `src/interfaces/quality/http.py` | bearer auth、ETag、`no-store`、只读 artifact 测试 |
| Agent tool | `src/application/agent_tools/quality.py` | agent contract/plugin smoke 全量回归 |
| 本地门禁 | `src/application/quality/gate.py` | onboarding 前无效；onboarding 后按 account/market/consumer 阻断；目标持仓数据集缺失/重复和 stale 均 fail closed |

本地门禁已接入 `option_positions_read`、option performance、持仓物化/报告和 close-advice 读取/生成边界。消费者名称与 payload 中的 `blocked_consumers` 使用同一稳定标识。普通候选扫描不依赖持仓质量，不受无关异常影响。producer 与 gate 都不依赖 Hub 在线；Hub 只消费已发布的 V1 状态。

生产只读 canary、Host Watchdog 交叉证据、Hub onboarding、真实告警/恢复和 rollback
必须以当前部署证据验证，不能由本地测试替代。

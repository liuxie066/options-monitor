# 兼容面冻结反馈

- **状态**：当前约束
- **更新时间**：2026-08-29

本文冻结仍在使用、但不再承接新功能的兼容入口。冻结不是立即删除：bug、数据正确性、安全、
幂等和 readback 可以修；新字段、新命令、新策略和新 schema 必须进入 canonical owner。只有退出
证据完整时，才能在独立改动中删除冻结面。

## 反馈结论

这套“只修不增”边界仅适用于下表仍在使用的兼容面。Strategy Lab 的旧 nested CLI、
recorder 包装和 Top1 产品壳已退役，不再列为冻结兼容面。当前根级
`./om strategy-lab` 只提供 targeted history-K readiness；其他产品能力见
[Strategy Lab 当前实现清单](STRATEGY_LAB_DESIGN.md)。

## 冻结矩阵

| 冻结面 | 允许 | 新功能 owner | 退出证据 |
|---|---|---|---|
| `notify_symbols.py`、`preview_notification`、legacy renderer、`alert_engine.py` 用户正文投影 | 修渲染崩溃、错字、漏字段和 alert 误报；保持 compact 默认与 legacy 警告 | scheduled 用户正文进入 Daily Brief service / repository / renderer | tick 不再写 `symbols_notification.txt` / `symbols_alerts.txt`，preview 已改为 Daily Brief 投影，legacy enum 与断言无 caller |
| lifecycle / position projection / current decision / cash conversion / order fee migration 与 `ledger.api` migration re-export | 修 inventory、verify、apply、幂等、replay、readback 和旧事实对账 | 日常能力进入 `ledger.commands`、`ledger.queries`、projection；cash / fee 新语义在写入时形成 canonical provenance | 生产 inventory / pending 为零，CLI 已直连 migration owner，`ledger.api` 不再有 caller 后移除 re-export |
| tick 内 recommendation point、formal corpus capture、expectation seal 旁路 | 修 capture、封口、redaction、幂等和 fail-closed；不得丢失主扫描或通知 | 候选决策进入 Candidate Engine；研究取证进入 Research / Shadow Replay；formal 实验进入 Strategy Lab | research capture 已迁到独立 job，tick / pipeline / ledger 不再依赖 Research 或 Strategy Lab；旧 recommendation point reader 已退出 |
| ignored OI / volume flags、`OM_SECRET_BACKEND=env`、旧 credential env、`--config-path` alias、service credential migration | 保持旧调用可解析；修显式兼容路径 | 策略参数进入 Candidate Engine + YAML；secret 进入 Keychain / systemd credentials；参数使用现行 canonical 名称 | 运行诊断、CI、生产 unit、文档和 caller 均不再使用后删除；不得借兼容入口增加新 secret 或参数 |
| `python -m src.application.*` 旧入口、option-position repository wrapper、Futu combined client | 修现有参数解析或旧 backend 崩溃；wrapper 只转调 | 人工入口使用 `./om`，结构化入口使用 `./om-agent`；能力进入现有 application facade 或 capability client | caller 和测试完成迁移后删除 wrapper / `__main__`；已退休 candidate CSV 参数只保持拒绝，不得复活写出 |
| `compatibility_amount`、旧 fees / fee、旧 market 列、非 Futu `fetch_source` 名字 | 只读旧事实并保持审计可解释；冲突继续 fail closed | 新费用进入 fee provenance，新持仓字段进入当前 portfolio schema；实际行情读取保持 Futu / OpenD owner | 历史事实和兼容 reader 无引用后，按独立数据迁移与 readback 证据删除 |

## 不冻结的 canonical owner

以下生产核继续演进，但不得再建平行实现：

- Candidate Engine、Cash-Secured Put (CSP) / Covered Call (CC) / Combo Yield steps；
- Daily Brief service、repository、renderer；
- ledger commands、queries、projection；
- trade intake / lifecycle（migration 与 backfill 除外）；
- required-data 与 tick spine；
- `./om research shadow-replay ...`；
- `./om strategy-lab readiness refresh-history-k ...`。

`account_config_compatibility_path` 仍是 account 子进程配置权威链的一部分，不属于冻结兼容面，也不得
因此新增第二条 account config 文件通道。

## 变更检查

改动触及上表左列时，必须先回答：

1. 这是修复现有行为，还是新增行为？
2. 新增行为的 canonical owner 是谁？
3. 若要删除，退出证据是否同时覆盖 caller、公开合同、数据和 owning tests？

冻结面上的新增行为应停止并移到 canonical owner。冻结面上的回归修复应在变更说明中写明
“只修不增”。Architecture guard 只为可静态断言的高价值边界增加；不把整张表复制成脆弱的源码文本测试。

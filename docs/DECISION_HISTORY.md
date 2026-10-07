# 决策历史

[产品合同](DECISION_HISTORY_PRD.md)规定：历史建议必须可追溯，交易关联只能使用明确证据。

## 数据来源

`output_shared/state/decision_history.sqlite3` 是决策历史唯一权威来源。实际扫描运行在通知准备阶段保存快照；通知关闭、只生成不发送也保存。发送重试和跳过扫描不产生新决策。数据库保存原始输入、已归一化的 Brief、身份范围和内容摘要；同一运行的不同内容被拒绝，新运行追加版本。失败或数据不足记录不替换供发送使用的最新成功 Brief。

原有 revision/current/run JSON 是导出产物。删除或修改它们不改变已保存历史，SQLite 缺失或损坏时查询明确不可用。导出写失败可能发生在 SQLite 已提交之后；相同输入重试不重复记录。不要通过编辑数据库或重新扫描改写过去的建议。

交易事实仍归现有 `option_positions.sqlite3` 账本。历史快照不复制交易事件；刷新查询可反映账本纠正，原建议保持不变。

## 查询

在目标运行根目录，用现有 Tool Gateway：

```bash
./om-agent run --tool decision_history_read --input-json '{"config_key":"us","account":"lx","market":"US","start_date":"2026-07-01","end_date":"2026-07-31","limit":10}'
```

这里的账户、市场、日期仅为例子。实际范围由所选运行配置绑定的账户、物理账户和 REAL/SIMULATE 环境决定。可加 `symbol`；只有历史记录里出现该标的才匹配。缺少身份的历史记录显示覆盖缺口，不能归入当前环境。结果区分 `ok`、`empty`、`partial`、`unavailable`。

有 `next_cursor` 时，保留原条件并传入该游标继续。翻页使用既有本地 HMAC 密钥签名，固定第一次查询时的记录上限；新增决策只在刷新后出现。密钥不可用或游标过期、范围变化时需重新查询。日期采用市场交易日期，格式 `YYYY-MM-DD`。

历史通知可将 `notification_perception_read` 保存的单条 `report_refs` 作为 `reference` 传入，并保留账户、市场和覆盖对应日期的范围。查询校验其 `source_run_id`、`market_date`、revision 与 source_digest；发送尝试的 run_id 不作为原决策。没有可验证引用时返回原因及可改用普通历史查询的提示，不寻找替代报告。

查询不扫描、不发送、不导入、不写交易或业务状态。固定范围只约束决策版本；每页的交易结果来自查询时的账本。

## 明确关联和结果

当前可用关联来自 Wheel 原候选的 final_candidate_id、run-bound snapshot hash 和已有 intent-created/consumed 事件，且开仓身份和数量可核对。原始输入没保留这些字段，或者只有相同合约、相近时间、策略归属、Combo 推测匹配时，显示“未建立关联”。这不表示用户没交易或没采纳。

- 未结束或部分平仓仍有余量：展示状态及数量，不显示最终收益或浮动盈亏。
- 已结束：复用账本已有结果，保留币种、数量、乘数和实际费用。已实现期权净现金流不代表股票加期权总收益。
- 缺少身份、费用、乘数、数量依据或存在冲突：结果不可用，说明缺口；不默认乘数、不猜分配、不混加币种。

## 旧 JSON 迁移

源码交付不执行生产迁移。旧安装切换到本实现前，由运行环境操作人保存现有数据并单独批准迁移；真实覆盖率取决于仍保留的证据。存在旧 revision 文件而该范围还没有 SQLite 历史时，新保存报 `history_migration_required`，防止复用旧版本号。

在明确的目标运行根目录预览（可通过项目既有 `OM_RUNTIME_ROOT` 选择；先核对目标）：

```bash
./om decision-history import --account lx --market US
```

预览只读，列出 `ready`、`already_imported`、`rejected` 及原因。验证版本文件与原运行副本一致，校验候选封存清单和该运行冻结的配置，再确认历史物理账户及环境。不用今天的配置补历史身份，不用现行策略重算旧建议。可选字段缺失保留原样，来源或身份不可证明则拒绝。

检查预览后，显式应用同一个摘要：

```bash
./om decision-history import --account lx --market US --apply --preview-hash '<preview_hash>'
```

源文件或目标历史变化使旧预览失效。导入事务提交可验证记录及迁移回执，并独立读回；重复导入不新增决策。全部旧记录被拒绝时，显式应用仍保存拒绝清单及已占用版本号，允许后续新运行从更高版本继续；查询保留历史覆盖缺口。被拒绝记录不代表正常空历史，也不应直接删除其来源。导入后读取不再依赖 JSON。生产迁移、升级和验收仍需各自授权。

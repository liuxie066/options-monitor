# 订单 / 成交 / 持仓领域模型统一需求

- 状态：已收口——需求已确认，可交接 devflow；code/ready 门槛 exit 0。
- 日期：2026-09-16。
- 根任务：全库只有一个权威的期权/股票订单、成交、持仓字段定义。
- 需求 owner：本文（产品需求合同）。技术实现参考另见 [ORDER_DOMAIN_MODEL_DESIGN.md](ORDER_DOMAIN_MODEL_DESIGN.md)（字段表/映射/迁移路径，非需求真源）。
- 实现目标仓库：options-monitor。

## 1. 目标、边界与成功信号

### 1.1 目标与困难

目标用户是 options-monitor 各模块的维护者。当前订单/成交/持仓的「领域模型」散落在约 20+ 套互不相同的表示里，字段命名与类型对不齐，导致期权与股票、事实层与策略层各自为政。目标是把它们收敛为**一套**权威结构，所有相关模块（ledger、trades、Combo、Wheel、assigned_stock、费用同步）共用同一套设计，不再各自搞一套。

困难不在「缺模型」，而在三处结构性碎片：①事实层与投影层是期权专属（股票被硬塞进期权形状）；②「订单（Order）」没有正位（订单只在费用同步、回查、通知文本三处各写各的）；③策略层各自重声明（Combo/Wheel 在账本之上各造一套字段）。具体现状见 §2。

### 1.2 非目标

不做：自动交易链路或投影算法本身的语义改动；期权/股票双账本（用 `asset_type` 判别而非两套骨架）；交割（assignment/exercise）的 deliverable 核算新功能；对 Futu/OpenD 等外部契约的改造；回测或研究平台。统一不新增持久化实体，只做「归位、定名、定类型」。

### 1.3 成功信号

- **S1（资产判别统一）**：期权与股票的成交事实、持仓投影共用一套 `asset_type ∈ {stock, option}` 判别结构，股票不再被塞进期权形状，`shares`/`cost_basis` 与 `contracts`/`premium` 不再两套词汇并存。
- **S2（订单归位）**：订单明确为入口归组层（非持久化实体），订单级费用按 `order_id` 归组摊到各成交，不再有以订单为主键的独立领域结构。
- **S3（策略层收敛）**：Combo/Wheel 策略投影改读权威 `PositionLot`，不再自造 lot 数量/价格字段与双名。
- **S4（命名与类型单一）**：同一语义全库只有一个字段名、一种类型（金额 Decimal、乘数 int、时间 ms、数量 int（期权）/ Decimal（股票）+ unit）；旧记录层（`PositionLotFields`/`OpenPositionCommand`）退役。
- **S5（无 v2 平行结构）**：统一通过原地优化现有 `ContractKey`/`TradeEvent`/`PositionLot` 完成，旧期权事件兼容读缺省推导 `option`，不新建 v2 类/版本化 schema。
- **S6（跨界校验收敛）**：期权↔股票的跨界校验/计算不再多点各自实现，收敛为「单一校验器 + 单一仲裁器」的少量权威函数，业务层只消费类型化结果（5 家族约 37 行号引用见设计文档 §10.3）。

## 2. 当前事实与目标变化

源码依据：§2 现状证据读自本地检出 `main@d0d6094bc53db3d4ce653a0a65ccb81d13cf082b`；本文与设计文档的落库基线为 `main@4fc161b8042f4282cdc2a7c01c06df4bfdc3f3a8`，未核实生产部署配置，也未在新基线逐条重核 §2 现状。落库基线内已落地 `541c19c8`（股票/ETF execution currency 由 symbol identity 派生），与设计文档 §4.1.2 的 currency 归属口径一致。

| 现状证据（源码） | 本次目标 |
|---|---|
| `ContractKey` 无 `asset_type`，`position_side` 混在身份里，`strike: float` | `ContractKey` 回归纯合约身份 + `asset_type`，`position_side` 移出（§5.2） |
| `TradeEvent`/`PositionLot` 只有 `contracts`/`premium_open`（期权专属），`price`/`multiplier`/`fees` 为 float | 事实层/投影层加 `asset_type`/`quantity_unit`，金额 Decimal、乘数 int |
| `assigned_stock.py` 股票 lot 用 `shares`/`stock_cost_basis_total`/`stock_lot_id` 一套词汇 | 股票 lot 提升为 `asset_type=stock` 的一等 `PositionLot`，`lot_id` 统一 |
| Combo `_Lot.contracts_original`/`multiplier: str`/`strike: str`；Wheel `net_premium`/`net_income` 双名 | 策略投影改读权威字段，删自造字段与遗留别名 |
| `position_fields.py` 的 `PositionLotFields`/`OpenPositionCommand` 旧记录层 | 退役，读模型以 `PositionLot.to_dict()` 为准 |
| `order_fee_sync.py` 以 `order_id` 归组订单级费用 | 保留归组行为，订单作为入口归组引用进权威结构 |
| 跨界校验/计算散落约 25 处（5 家族，见设计文档 §10.3） | 收敛为 5 家族的单一校验器/仲裁器，业务层不各自实现/回退/仲裁 |

## 3. 主线（数据从入口到投影的统一链路）

后台数据需求，以事件与结果表达：

```text
外部成交/订单 (Futu order/deal、OpenD、文件、手动文本)
   │  各 adapter 归一化为 ExecutionInput（订单只作可空引用 + 入口归组）
   ▼
ExecutionInput (trade_execution.v1)  ── 唯一事实，asset_type 判别
   │  → TradeEvent（账本事件，ExecutionInput 的持久化形态）
   ▼
PositionLot ── 投影层（持仓手，asset_type 判别）
   │  策略投影 / 元数据挂载
   ▼
Combo / Wheel 投影 & 元数据 ── 策略层（只读投影，不重复声明 lot 字段）
```

- 订单 = 入口归组：`external_order_namespace`/`external_order_id` 是可空引用，不参与 lot 投影。

## 4. 数据 / 判断 / 动作合同

### 4.1 数据合同

- `asset_type ∈ {stock, option}` 是资产判别；`quantity_unit ∈ {share, contract}` 由 `asset_type` 派生。
- 期权特有字段：`option_type`/`strike`(Decimal)/`expiration_ymd`/`multiplier`(int)/`deliverable`(可空)。
- 股票特有字段：`cost_basis_total`(Decimal，可空，含费总额)；每股成本 `cost_basis_per_share = cost_basis_total / shares_opened` 为派生值，不存储。 多头按买入金额加已确认开仓费，空头按卖出金额减已确认开仓费形成平仓收益基准；空头费用超过卖出金额时该基准可以为负。平仓收益按持仓方向计算，并扣已确认平仓费；费用未知时成本及依赖它的收益为 null。
- 数量三态按单位命名：期权 `contracts_opened/open/closed`，股票 `shares_opened/open/closed`。
- 金额/价格统一 Decimal（JSON 用十进制字符串）；时间统一 epoch 毫秒 int（`*_ms`）；到期日统一 `expiration_ymd`（`YYYY-MM-DD`）。

### 4.2 判断合同

- `side`/`position_effect` 统一走 `normalize_trade_side`/`normalize_position_effect`，策略层不得自造别名映射。
- `position_side`（long/short）是持仓方向，属于成交/持仓语义，不属于合约身份。

### 4.3 动作合同

- 只有「适配器」把外部数据转成 `ExecutionInput`；只有「投影」从 `TradeEvent` 推出 `PositionLot`。中间层与策略层不碰原始字段名。
- 序列化只在边界：dataclass 是内部唯一形态，dict/JSON 只出现在 SQLite `event_json`、Inbox `payload_json`、文件 JSONL 三处。

## 5. 已确认决定与理由

1. **Order 是归组层，不是持久化实体**：订单只承载委托意图与分组，不参与 lot 投影；成交才是进账本的经济事实。（决定：入口归组层）
2. **命名以 `ExecutionInput`（`trade_execution.v1`）为基准**：不改名、不重排，只补 `asset_type` 语义与类型收紧。
3. **不搞 v2，原地优化**：直接改现有 `ContractKey`/`TradeEvent`/`PositionLot`，旧数据用兼容读缺省推导，不新建并行的 v2 类/版本化 schema。（理由：现有 `wheel_event.v1/v2`、`combo_identity.v2` 等版本化分裂正是字段不统一的来源之一，再引入 v2 加剧碎片。）
4. **股票 `cost_basis` 口径 = 存 lot 总额、派生每股**：`cost_basis_total` 含费用为权威字段；外部 Futu 每股 `avg_cost` 只在适配层转总额进账本。
5. **`position_side` 移出身份，三步迁移**：先加（新增 `derive_position_side(position_effect, side)` 派生函数，`position_side` 不新增存储字段；旧事件缺省从 `contract_key` 推导）→ 再迁（30+ 处读点改读 lot/event 侧派生 side，`position_key` 聚合改来源不变字符串）→ 后删（`ContractKey` 去掉 `position_side`）。
6. **`deliverable` 保持可空**：当前账本不消费它，遇到即 `unsupported_contract_deliverable`，防止静默误处理。
7. **跨界校验/计算收敛为 5 家族**：期权↔股票的跨界校验/计算是全库跨界与入账边界的系统性重复，不是 wheel 独有。统一领域的验收红线是收敛为「单一校验器 + 单一仲裁器」——5 家族：①数量换算（`contracts × multiplier = shares`）②`stock_settlement` 校验 ③执行身份/经济冲突仲裁 ④双名回退 ⑤费用/币种归属仲裁，落点约 37 处行号引用（见设计文档 §10.3）。

弃选方案不再作为当前要求：双账本（期权/股票两套骨架）；每股成本为权威存储（会造成费用总额舍入丢失）；v2 平行 schema 演进。

## 6. 验收

> 证明方法引用已核查的现有测试/入口；新增文件、函数与测试接口设计留给 Devflow。

> 2026-09-22 状态：D1–D4 生产迁移已在 R1 窗口完成，证据目录为 `<deploy-home>/migration-evidence/lot-identity-20260921T191339Z`；required follow-up rebuild 后 head generation 一致。R2 已完成源码实现与本地定向验证，尚未发布或升级：它删除普通运行路径的双形状 SQL，普通开库只接受最终 `lot_id` 结构并关闭迁移写窗口。历史 `trade_events` 的兼容读继续保留，不等同于兼容旧数据库结构。

> 已裁决的数据语义不变：金额 codec 不经过中间 float；股票数量以十进制字符串保留、期权拒绝小数合约、历史 `contracts` JSON 键继续兼容。股票开仓成本只纳入已确认费用，实际零费用也要有证据；费用缺失或估算时，成本及依赖它的已实现收益为 null，数量仍发布；平仓费用缺失时已实现收益同样为 null，费用确认后通过重放恢复。

| ID | 验收目标 | 输入 / 条件 | 通过判据 | 证明方法 | 环境 | 模拟边界 | 前置条件 | 证据状态 |
|---|---|---|---|---|---|---|---|---|
| A1 | 资产判别统一（S1） | 期权、股票成交各一 | `TradeEvent`/`PositionLot` 均含 `asset_type ∈ {stock,option}`，股票 lot 不再用 `contracts`/`premium` 字段 | 读 `domain/domain/ledger/events.py`/`lots.py` 字段定义；跑 `tests/test_ledger_projection.py`、`test_assigned_stock_projection.py` | 本地 venv | 字段结构可静态断言；投影行为需真实事件样本或 fixture | `tests/fixtures` 现有样例 | planned |
| A2 | 策略层收敛（S3） | Combo/Wheel 的 lot 读取路径 | `_Lot` 不再定义 `contracts_original`/`multiplier: str`/`strike: str`；Wheel 候选不再 `net_premium`/`net_income` 双名 | 读 `combo_reconciliation.py`/`wheel.py`；跑 `tests/test_combo_reconciliation_domain.py`、`test_wheel_strategy.py` | 本地 venv | 静态删除字段 + 回归断言 | 同上 | planned |
| A3 | 旧数据兼容（S5） | 存量无 `asset_type` 的旧期权 `trade_events` | 兼容读缺省推导 `option`，投影出的 lot 与迁移前等价 | 跑 `tests/test_ledger_migration.py`、`test_position_projection_migration.py`、`test_resumable_projection_state.py` | 本地 venv | 需存量事件 fixture；无真实生产数据则用构造样本 | 迁移 fixture | planned |
| A4 | 命名类型单一（S4） | 各权威结构字段 | 金额/价格 Decimal、乘数 int、时间 ms、数量 int（期权）/ Decimal（股票）+unit，无 float 承载金额 | 静态检查 + `tests/test_architecture_guards.py`（import 边界）+ 类型校验 | 本地 venv | 类型断言；金额精度需 Decimal 构造样本 | 同上 | planned |
| A5 | 订单归组不变（S2） | 一笔多成交订单 + 订单级费用 | `order_fee_sync` 仍按 `order_id` 归组摊派，结果与迁移前一致 | 跑 `tests/test_order_fee_sync.py`、`test_order_fee_settlement.py`、`test_order_fee_namespace.py` | 本地 venv | 费用归组逻辑可纯函数化 | 订单/成交/费用 fixture | planned |
| A6 | 旧记录层退役（S4） | 投影与读模型路径 | 读模型以 `PositionLot.to_dict()` 为准，`PositionLotFields`/`OpenPositionCommand` 不再作为权威 | 读 `src/application/ledger/read_model.py`；跑 `tests/test_ledger_module_facades.py`、`test_position_projection_facade_inventory.py` | 本地 venv | 读模型字段映射回归 | 同上 | planned |
| A7 | 跨界校验收敛（S6） | 设计文档 §10.3 的 5 家族约 37 行号引用 | ①`contracts × multiplier` 收敛为单一换算函数；<br>②`stock_settlement` 的 `expected_side` 映射 + `shares==multiplier×contracts` 不变式收敛为单一校验器；<br>③执行身份/经济冲突仲裁收敛为单一仲裁器；<br>④无 `get(a) or get(b)` 双名回退残留（兼容读集中到归一化层）；<br>⑤费用/币种仲裁收敛到费用语义层 | 读设计文档 §10.3 落点文件（`lifecycle_allocation.py`/`writer_lifecycle_support.py`/`deal_identity.py`/`trade_execution.py` 等）；grep 校验无业务层散落乘法与双名回退；跑相关回归测试 | 本地 venv | 静态收敛断言 + 回归；跨界换算需 assignment 定向用例 | 同上 | planned |
| A8 | 列退役不产生「不可用」库（D1/D2） | 一个旧形状 store，含非空 `position_lots` | 重建后列合同闭合，无 `column_contract_open`，head 不落 `untrusted`；R2 普通开库只接受最终结构 | 生产证据目录的迁移与 required follow-up rebuild 读回；`tests/test_lot_identity_migration.py`、`tests/test_ledger_lot_identity_schema_guard.py` | 本地 venv + 生产迁移读回 | 旧形状只通过受控迁移入口 | 设计文档 §12.3 | R1 production verified; R2 local verified |
| A9 | 重建的原子性与校验（D1/D2） | 重建中途注入失败 | 行数、读回等值、`foreign_key_check` 任一不通过则整体回滚，旧表原样保留 | 生产证据目录的校验读回；`tests/test_lot_identity_migration.py` 的失败注入与原样读回用例 | 本地 venv + 生产迁移读回 | 生产形态不可注入 | 同上 | R1 production verified; R2 local verified |
| A10 | payload 重写可回滚（D3/D4） | 一次遍历重写全部 `position_lots.fields_json` | 重写后读模型输出等价；窗口 `.backup` 副本可逐行比对，`integrity_check=ok`，哈希与幂等读回成立 | 生产证据目录的 backup、逐行/哈希、完整性与幂等证据；`tests/test_lot_identity_migration.py` | 本地 venv + 受控窗口 | 写窗口已关闭 | §7.1 第 2 条 | R1 production verified; R2 local verified |
| A11 | 迁移形态与门控（全批次） | 升级与普通开库 | 数据改写仅由受控命令触发；R2 普通开库对旧/部分结构只读拒绝 | 生产证据目录；`tests/test_option_positions_cli.py`、`tests/test_ledger_lot_identity_schema_guard.py` | 本地 venv + 生产升级读回 | `apply` 只保留只读 preview，写入口禁用 | 设计文档 §9.5 M1/M5 | R1 production verified; R2 local verified |

## 7. 存量数据处理

存量 SQLite ledger 中无 `asset_type` 的旧期权事件，通过兼容读缺省推导 `option` 处理；不迁移、不改写历史事件，只在新代码读取时补缺省。迁移后新事件一律显式 `asset_type`。旧记录层（`PositionLotFields`/`OpenPositionCommand`）随读模型切换退役，不保留双写。

以上「不迁移、不改写」的定界适用于**代码语义收敛批次**（已随 #311 合入 main）。**列退役批次（D1–D4）另行改写存量**，见下节。

### 7.1 列退役批次的存量处理要求（D1–D4）

本批次是唯一会改写已持久化数据的批次，需求侧的四条硬要求：

1. **生产方式不得依赖「升级即自动改库」。** 本仓的升级流程只切 symlink + 重启（`service_upgrade.py:2527` 起），改库发生在重启后首次开库的 bootstrap 事务里，**该路径没有 dry-run 门、也没有备份**。因此全部改动必须由操作者显式触发并可先备份——已裁定 **D1–D4 一律走「声明即止 + 显式命令迁移」**（设计文档 §9.5 M1），并必须提供 preview/dry-run（§9.5 M5）。
2. **必须有可回滚的窗口流程。** 备份用 SQLite `.backup` API（禁止裸 `cp`）、`integrity_check` 必须为 `ok`、源库原样保留作为回滚证据；窗口内停**所有**账本写入方。流程沿用 `docs/DEPLOY_LINUX_MAC.md:403-438`，不新造。
3. **结构改动与列分类合同必须同批改。** `POSITION_LOTS_COLUMN_CLASSIFICATION`（`repository_common.py:99-108`）是开库合同的一部分；漏改会让重建成功但库停在 `untrusted`，即「改成功了却不可用」。
4. **迁移的权威仍是 `fields_json`。** D1 退役的 `position_lots.expiration` 是派生镜像，D2 改的是身份列名；两者的数据真源都在 payload，迁移不得引入第二个真源。

执行设计（机制事实、形态对照、重建配方、逐项落点、⑥ 的前置判定）见设计文档 §12；**执行决策（形态/D2 改法/⑥ 顺序/身份同一性/dry-run/落地顺序）已收口于设计文档 §9.5 M1–M6**。

## 8. 未决问题

无阻塞产品决定（三个原待确认口径已在 §5 落定，跨界收敛方向见 §5 第 7 条）。剩余为待 Devflow 实现方案核实的技术事实：30+ 处 `contract_key.position_side` 读点的具体迁移清单、兼容读缺省推导的落点、`position_key` 概念下沉到 lot 层的具体拆法、5 家族约 25 落点的精确收敛清单（设计文档 §10.3 已给落点文件与行号，Devflow 需逐一核对并绑定为单一权威函数）——这些是实现细节，不影响产品合同。

## 9. 批准记录

| 决定 | 用户原话（摘录，无编造时间戳/消息 ID） |
|---|---|
| 统一领域模型，一个权威设计 | 「先做归一化的领域模型设计，明确期权订单包含哪些字段、股票订单包含哪些字段，全库应该只有一个权威设计」 |
| 各模块共用一套，不各自搞 | 「接下来我们把订单和交易的数据结构确定下来，各个相关模块都使用同一套设计，不要各自搞一套」 |
| 不搞 v2、原地优化 | 「不要在代码里搞v2，直接优化，下一次继续解决三个待确认」 |
| Order = 入口归组层 | AskUserQuestion 选「仅入口归组层（推荐）」 |
| 命名以 ExecutionInput 为基准 | AskUserQuestion 选「沿用 ExecutionInput（推荐）」 |
| 三个口径落定 | 「继续解决三个待确认」后于本会话定稿（§5 第 4/5/6 条） |
| 用 prdflow 落需求 | 「先用prdflow落需求」 |
| 跨界校验收敛为 5 家族；两文档审查精简 | 「扩大范围再找找，还有没有类似wheel校验的代码块没有被考虑进来？」→「要补，要统一领域模型」→「看看现在的prd有没有可以优化，简化的地方」「看看设计文档」→「全部执行」 |
| 修 round-9 的 6 条 finding 并提交 | 「修 F1…F6 和 tests:712，然后提交」 |
| 推送分支并开 PR | 「推送分支，开 PR」 |
| 合并 PR #311 进 main | 「合入 main」 |
| 改 PR 描述 | 「改 PR 描述」 |
| 开列退役批次，先落需求与设计 | 「开迁移批次」→ AskUserQuestion 选「先落需求+设计文档」 |
| 迁移批次执行决策五条（形态/D2 改法/⑥ 顺序/身份同一性/dry-run） | 「你的建议是啥」→「按建议，落进 §9 已定决策」 |

## 10. devflow 交接

- 根目标：全库一个权威的订单/成交/持仓字段定义。
- `prd_doc`：本文（已随 #311 合入 main）。
- `design_doc`：`docs/ORDER_DOMAIN_MODEL_DESIGN.md`（已存在，技术实现参考；本文是需求真源）。
- 交付状态：代码语义收敛批次已合入 main；列退役 D1–D4 已完成 R1 生产迁移与读回（`<deploy-home>/migration-evidence/lot-identity-20260921T191339Z`，含 required follow-up rebuild 后 head generation 一致证据）。R2 已实现并通过本地定向验证，尚未发布或升级。
- `inventory` / `verify` / `apply` 默认 preview 保留为只读历史诊断；普通构建的 `--apply` 不可用。
- 历史事件兼容读继续保留；旧 `position_lots` / `wheel_events` 结构必须由历史受控迁移处理，普通 repository 不修复、不改写。
- 实现细节与验收测试的落点：§12.3 的重建配方、§12.4 的逐项落点、§9.5 M6 的落地顺序。

## 附录：prdflow-gate 记录

```prdflow-gate
{
  "schema": "prdflow-gate.v1",
  "code": {
    "status": "pass",
    "reason": "已从入口到投影读通链路并绑定文件字节，关键现状（期权专属事实层、股票独立词汇、策略层自造字段、订单无正位）均有源码依据",
    "root": "/Volumes/workspace/options-monitor",
    "branch": "main",
    "revision": "d0d6094bc53db3d4ce653a0a65ccb81d13cf082b",
    "comparison": "本地 main 有未提交差异：M .agents/skills/*、AGENTS.md、CLAUDE.md、docs/AGENT_WIKI.md、docs/INDEX.md；?? docs/ORDER_DOMAIN_MODEL_DESIGN.md、codex/*。本次结论基于工作区实际字节，不依赖这些未提交改动",
    "deployment": "未核验生产部署配置；仅为本地源码事实",
    "chain": "src/infrastructure/futu_gateway.py(order/deal 查询) → domain/domain/trade_execution.py(ExecutionInput 归一) → domain/domain/ledger/events.py(TradeEvent 持久化) → domain/domain/ledger/projection.py + lots.py(PositionLot 投影) → combo_reconciliation.py / wheel.py(策略投影)",
    "findings": "已有能力：ExecutionInput 已定义 asset_type/quantity_unit/Decimal 命名基准；投影链路 canonical。真实缺口：ContractKey/TradeEvent/PositionLot 期权专属无 asset_type；assigned_stock 独立 shares/cost_basis 词汇；Combo _Lot 与 Wheel 候选自造字段；PositionLotFields 旧记录层冗余；订单只作可空引用无归组主键；跨界校验/计算散落 5 家族约 25 落点（数量换算、stock_settlement 校验、执行身份仲裁、双名回退、费用/币种仲裁）",
    "limits": "未读生产运行时状态与 OpenD 实时样本；投影 verify 入口以 tests 目录为准（test_ledger_projection.py 等）；18 个文件为支撑结论的核心，未全仓审计",
    "files": [
      {"path": "domain/domain/trade_execution.py", "sha256": "314b2540b44d6f9149695c487146b7c508fa4e931d550ab1fda2115c22576812", "role": "权威命名基准", "finding": "normalize_execution_input 定义 asset_type/quantity_unit/Decimal 字段，为选定命名基准"},
      {"path": "domain/domain/ledger/identity.py", "sha256": "60450bce9ec2a96a47d5c21f85db3f9a6d6dbf2932f96181c4e6010240a2d7f0", "role": "身份", "finding": "ContractKey 期权专属无 asset_type，position_side 混入身份，strike: float"},
      {"path": "domain/domain/ledger/events.py", "sha256": "a2b15f8267a4a95775bccc5525f87099a2884d031e0d57028944bdeba025dd8c", "role": "账本事实", "finding": "TradeEvent 无 asset_type/quantity_unit，contracts int、price/multiplier/fees float"},
      {"path": "domain/domain/ledger/lots.py", "sha256": "ae7640c2bcda4a841a0e2bb076691142b2899ebb047f39096cf23def80e3a3ee", "role": "投影", "finding": "PositionLot 期权专属，contracts_* int、premium_open/multiplier/realized_pnl float，无 asset_type"},
      {"path": "domain/domain/ledger/position_fields.py", "sha256": "9da16c068413cb4953a17c226e26f3a727ad7311f972a9c50d544d7431eff068", "role": "旧记录层", "finding": "PositionLotFields/OpenPositionCommand/PositionLotPatch 为冗余记录层，待退役"},
      {"path": "domain/domain/assigned_stock.py", "sha256": "ae70a89abba0ab53f923d92df6079d093d1891da16a224e6660b2dea8631fb83", "role": "股票 lot", "finding": "shares/stock_cost_basis_total/stock_lot_id 独立词汇，stock_cost_basis_total 为含费总额，每股为派生"},
      {"path": "domain/domain/combo_reconciliation.py", "sha256": "374e01cadaffe062493de7cf9f384216d2cae3c5e35a4c28a2954ce7aeb284ef", "role": "策略投影", "finding": "_Lot 字段发散：contracts_original、multiplier: str、strike: str"},
      {"path": "domain/domain/combo_identity.py", "sha256": "88492f9afa2e1df018245ef8f887a3c178b3580cb9fea5299a5e0c5024e9f14c", "role": "Combo 身份", "finding": "combo_identity.v2 版本化 schema，为既有碎片例证"},
      {"path": "domain/domain/wheel.py", "sha256": "a11575db649b52f2e577ba6e4b544ac907f2ad691be524f62f4cde17d3c62178", "role": "策略候选", "finding": "net_premium/net_income 双名，WHEEL_EVENT_SCHEMA v1/v2"},
      {"path": "src/application/trades/order_fee_sync.py", "sha256": "cdff3dbef158d9bee0f851185e693aeef2c5d2d3e77079326561154f2f83ba76", "role": "订单费用归组", "finding": "订单级费用按 order_id 归组，为 Order 归组层现状"},
      {"path": "src/application/wheel/read_model.py", "sha256": "0c3105eb2d683e41b8757524da1e4734b8da064849842bb9045ab8e488100867", "role": "Wheel 读模型", "finding": "batch_generation_hash/active_call_lot_ids 遗留别名 shim"},
      {"path": "src/infrastructure/futu_gateway.py", "sha256": "8bfc59f2fd72647b283547ce5d5377d47f6f9a8c763c479af8a93812babb458b", "role": "外部网关", "finding": "order_list/deal_list 分离，order 与 deal 两个数据面"},
      {"path": "src/application/trades/deal_identity.py", "sha256": "facbf828900e7058e75c9914d516b26489ff2f3ab4e5591956454b22edd177b7", "role": "执行身份仲裁", "finding": "completed_ledger_execution_events 含 identity_conflict/applied_association_conflict/economic_conflict/split_incomplete 五类冲突判定"},
      {"path": "src/application/ledger/writer_lifecycle_support.py", "sha256": "8c2d7ea28d17a78bb584b36cf27b152f6d45a86a2d3064c823f1e5414a6f99ac", "role": "stock_settlement 校验", "finding": "1060-1140 完整校验族：futu_account/symbol/price/quantity/side/time 六类 mismatch，expected_side 映射 + shares==multiplier×contracts 不变式"},
      {"path": "domain/domain/lifecycle_allocation.py", "sha256": "44f6489af83050eb835251c598bf7334bea5279559e59f2e1c06b8f998556512", "role": "跨界换算", "finding": "allocate_stock_settlement :94 shares=Decimal(contracts)*multiplier"},
      {"path": "domain/domain/risk_capacity.py", "sha256": "e7c7430215b03c995c1da5410dc043b366fc5787d6617a996c0fe93bdc1dd7b0", "role": "跨界换算", "finding": ":318/:472/:599-604 contracts×multiplier 换算份额容量/现金担保/可卖合约"},
      {"path": "domain/domain/engine/candidate_engine.py", "sha256": "0e4559a83b9cedd1de387fdf3c9460ff501934619faf87d4b373139724a53410", "role": "跨界换算", "finding": ":569/:610 gross_premium/assignment_notional 由 ×multiplier 派生"},
      {"path": "domain/domain/portfolio_assignment_scenario.py", "sha256": "0fb2ed7ba992ad594ba7a5cc518f4b155b98470ccc501e29289265544da05643", "role": "跨界换算", "finding": ":553 shares=contracts*multiplier 指派场景投影"}
    ]
  },
  "walkthroughs": [
    {
      "scenario": "首个任务：期权成交→投影，asset_type 判别",
      "input": "一笔 CSP short put 期权 open 成交，经 normalize_execution_input",
      "steps": [
        "适配器把外部 deal 归一化为 ExecutionInput(asset_type=option, quantity_unit=contract)",
        "转 TradeEvent(asset_type=option) 持久化进 SQLite 账本",
        "投影推出 PositionLot(asset_type=option)",
        "投影 verify 回归核对 lot 字段"
      ],
      "result": "期权 lot 字段为 contracts_opened/open/closed + premium_open，asset_type=option",
      "exit": "lot 落库，投影 verify 通过"
    },
    {
      "scenario": "已知第二场景：股票 lot 一等化",
      "input": "一笔股票 assignment（被指派接货）成交",
      "steps": [
        "股票成交归一化为 ExecutionInput(asset_type=stock, quantity_unit=share)",
        "转 TradeEvent(asset_type=stock) 持久化",
        "投影推出 PositionLot(asset_type=stock)",
        "股票 lot 与期权 lot 同构，lot_id 统一"
      ],
      "result": "股票 lot 字段为 shares_opened/open/closed + cost_basis_total，asset_type=stock",
      "exit": "股票 lot 成为一等 PositionLot，无 shares/cost_basis 独立词汇"
    },
    {
      "scenario": "订单归组层",
      "input": "一笔订单对应多笔成交 + 订单级费用",
      "steps": [
        "order_fee_sync 按 broker_account_id + external_order_namespace + external_order_id 归组",
        "订单级费用摊到各 Execution",
        "无独立 Order 实体持久化"
      ],
      "result": "费用正确归到各成交",
      "exit": "费用同步行为与迁移前一致"
    }
  ],
  "acceptance": [
    {"id": "A1", "input": "期权、股票成交各一", "criterion": "TradeEvent/PositionLot 均含 asset_type ∈ {stock,option}，股票 lot 不再用 contracts/premium 字段", "method": "读 events.py/lots.py 字段定义；跑 tests/test_ledger_projection.py、test_assigned_stock_projection.py", "environment": "本地 venv", "mock_boundary": "字段结构可静态断言；投影行为需真实事件样本或 fixture", "prerequisites": "tests/fixtures 现有样例", "owner": "实现者", "evidence_status": "planned"},
    {"id": "A2", "input": "Combo/Wheel 的 lot 读取路径", "criterion": "_Lot 不再定义 contracts_original/multiplier:str/strike:str；Wheel 候选不再 net_premium/net_income 双名", "method": "读 combo_reconciliation.py/wheel.py；跑 tests/test_combo_reconciliation_domain.py、test_wheel_strategy.py", "environment": "本地 venv", "mock_boundary": "静态删除字段 + 回归断言", "prerequisites": "tests/fixtures 现有样例", "owner": "实现者", "evidence_status": "planned"},
    {"id": "A3", "input": "存量无 asset_type 的旧期权 trade_events", "criterion": "兼容读缺省推导 option，投影出的 lot 与迁移前等价", "method": "跑 tests/test_ledger_migration.py、test_position_projection_migration.py、test_resumable_projection_state.py", "environment": "本地 venv", "mock_boundary": "需存量事件 fixture；无真实生产数据则用构造样本", "prerequisites": "迁移 fixture", "owner": "实现者", "evidence_status": "planned"},
    {"id": "A4", "input": "各权威结构字段", "criterion": "金额/价格 Decimal、乘数 int、时间 ms、数量 int+unit，无 float 承载金额", "method": "静态检查 + tests/test_architecture_guards.py + 类型校验", "environment": "本地 venv", "mock_boundary": "类型断言；金额精度需 Decimal 构造样本", "prerequisites": "tests/fixtures 现有样例", "owner": "实现者", "evidence_status": "planned"},
    {"id": "A5", "input": "一笔多成交订单 + 订单级费用", "criterion": "order_fee_sync 仍按 order_id 归组摊派，结果与迁移前一致", "method": "跑 tests/test_order_fee_sync.py、test_order_fee_settlement.py、test_order_fee_namespace.py", "environment": "本地 venv", "mock_boundary": "费用归组逻辑可纯函数化", "prerequisites": "订单/成交/费用 fixture", "owner": "实现者", "evidence_status": "planned"},
    {"id": "A6", "input": "投影与读模型路径", "criterion": "读模型以 PositionLot.to_dict() 为准，PositionLotFields/OpenPositionCommand 不再作为权威", "method": "读 src/application/ledger/read_model.py；跑 tests/test_ledger_module_facades.py、test_position_projection_facade_inventory.py", "environment": "本地 venv", "mock_boundary": "读模型字段映射回归", "prerequisites": "tests/fixtures 现有样例", "owner": "实现者", "evidence_status": "planned"},
    {"id": "A7", "input": "设计文档 §10.3 的 5 家族约 25 落点", "criterion": "①contracts×multiplier 收敛为单一换算函数；②stock_settlement 的 expected_side 映射 + shares==multiplier×contracts 不变式收敛为单一校验器；③执行身份/经济冲突仲裁收敛为单一仲裁器；④无 get(a) or get(b) 双名回退残留（兼容读集中到归一化层）；⑤费用/币种仲裁收敛到费用语义层", "method": "读设计文档 §10.3 落点文件（lifecycle_allocation.py/writer_lifecycle_support.py/deal_identity.py/trade_execution.py 等）；grep 校验无业务层散落乘法与双名回退；跑相关回归测试", "environment": "本地 venv", "mock_boundary": "静态收敛断言 + 回归；跨界换算需 assignment 定向用例", "prerequisites": "tests/fixtures 现有样例", "owner": "实现者", "evidence_status": "planned"}
  ],
  "temporal": {
    "status": "pass",
    "reason": "涉及 SQLite 账本持久状态：旧无 asset_type 事件 → 新代码投影 → 重投影/repair 的连续状态",
    "scenarios": [
      {"sequence": "旧事件(无 asset_type)入库 → 新代码兼容读缺省推导 option → 投影 → repair 重投影", "final_state": "lot 统一 asset_type 字段，字段值与迁移前等价", "must_not_recur": "迁移后新事件不得再出现无 asset_type；旧记录层字段不得复活"}
    ]
  },
  "research": {
    "status": "not-applicable",
    "reason": "方向性判断（单一事实源、asset_type 判别、订单归组层）来自源码事实核查与已确认决定，不依赖外部最佳实践资料"
  },
  "blockers": [],
  "review": {
    "status": "pass",
    "reviewer": "self",
    "method": "self-review",
    "rationale": "反例评审：①S1/A1 资产判别方向单向（股票被塞进期权形状），反向不存在，无需补反向验收；②A2 聚焦数量/价格字段删除，leg_role/market_date 等策略元数据属归位而非删除，边界清楚；③A3'迁移前等价'的具体断言留 Devflow，需求只要求存量语义不丢；④§3/§4 的 SQLite event_json、Inbox payload_json 序列化边界是既有技术约束，非新增实现；⑤三个口径、命名基准、Order 归组、不搞 v2 均已确认，无建议混入已确认；⑥research 不依赖外部竞品资料；⑦S6/A7 新增跨界校验收敛，落点由设计文档 §10.3 的 5 家族约 25 处支撑（数量换算/stock_settlement 校验/执行身份仲裁/双名回退/费用币种仲裁），收敛方向与既有 S4 命名类型单一一致、不新增实体，反向（保持散落）不构成可承诺结果，无需补反向验收。无实质缺口，裁定 pass",
    "content_sha256": "31b2819e467a5d84f6ba9cfa592edb050211e88c84f76a0c22cdd70eab27a12e"
  }
}
```

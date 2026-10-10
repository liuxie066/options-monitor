# 订单 / 成交 / 持仓领域模型统一需求

- 状态：已确认的产品需求合同；代码语义收敛及 R2 旧库写窗口退役已实现，目标环境状态另行核验。
- 日期：2026-09-16。
- 根任务：全库只有一个权威的期权/股票订单、成交、持仓字段定义。
- 需求 owner：本文（产品需求合同）。技术实现参考另见 [ORDER_DOMAIN_MODEL_DESIGN.md](ORDER_DOMAIN_MODEL_DESIGN.md)（字段表/映射/迁移路径，非需求真源）。
- 实现目标仓库：options-monitor。

## 1. 目标、边界与成功信号

### 1.1 目标与困难

目标用户是 options-monitor 各模块的维护者。订单/成交/持仓使用一套权威结构，相关模块（ledger、trades、Combo、Wheel、assigned_stock、费用同步）共用字段和领域语义；新功能继续扩展现有 owner。

早期的资产形状、订单归组和策略字段碎片是本需求的背景；不能作为当前源码仍有同样缺陷的证明。当前入口见 §2，目标与实现差异见技术设计。

### 1.2 非目标

不做：自动交易链路或投影算法本身的语义改动；期权/股票双账本（用 `asset_type` 判别而非两套骨架）；交割（assignment/exercise）的 deliverable 核算新功能；对 Futu/OpenD 等外部契约的改造；回测或研究平台。统一不新增持久化实体，只做「归位、定名、定类型」。

### 1.3 成功信号

- **S1（资产判别统一）**：期权与股票的成交事实、持仓投影共用一套 `asset_type ∈ {stock, option}` 判别结构，股票不再被塞进期权形状，`shares`/`cost_basis` 与 `contracts`/`premium` 不再两套词汇并存。
- **S2（订单归位）**：订单明确为入口归组层（非持久化实体），订单级费用按 `order_id` 归组摊到各成交，不再有以订单为主键的独立领域结构。
- **S3（策略层收敛）**：Combo/Wheel 策略投影改读权威 `PositionLot`，不再自造 lot 数量/价格字段与双名。
- **S4（命名与类型单一）**：同一语义全库只有一个字段名、一种类型（金额 Decimal、乘数 int、时间 ms、数量 int（期权）/ Decimal（股票）+ unit）；旧记录层（`PositionLotFields`/`OpenPositionCommand`）退役。
- **S5（无 v2 平行结构）**：统一通过原地优化现有 `ContractKey`/`TradeEvent`/`PositionLot` 完成，旧期权事件兼容读缺省推导 `option`，不新建 v2 类/版本化 schema。
- **S6（跨界校验收敛）**：期权↔股票的跨界校验/计算不再多点各自实现，收敛为「单一校验器 + 单一仲裁器」的少量权威函数，业务层只消费类型化结果（5 家族约 37 行号引用见设计文档 §10.3）。

## 2. 当前实现入口

| 责任 | 当前 owner |
| --- | --- |
| 成交输入、资产/单位、精确金额与执行身份 | `domain/domain/trade_execution.py` |
| 合约身份与方向归一化 | `domain/domain/ledger/identity.py`、`domain/domain/trade_contract_identity.py` |
| 账本事件与持仓投影 | `domain/domain/ledger/events.py`、`domain/domain/ledger/lots.py`、`domain/domain/ledger/projection.py` |
| 应用边界与只读投影 | `src/application/ledger/api.py`、`src/application/ledger/read_model.py` |
| 旧库只读盘点/验证 | `src/application/ledger/lot_identity_migration.py` |

历史事件兼容读与旧表结构兼容是两件事。R2 普通开库只接受最终结构，旧库不由普通开库自动修复。字段目标和实际序列化形状分别核对，不把此表视为全部验收已通过。

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

> 验收表保留产品要求和验证入口；历史证据不代替当前版本的结果。

> 2026-09-22 状态：D1–D4 生产迁移已在 R1 窗口完成，证据目录为 `<deploy-home>/migration-evidence/lot-identity-20260921T191339Z`；required follow-up rebuild 后 head generation 一致。R2 源码删除了旧库写窗口：它删除普通运行路径的双形状 SQL，普通开库只接受最终 `lot_id` 结构并关闭迁移写窗口。历史 `trade_events` 的兼容读继续保留，不等同于兼容旧数据库结构。

> 已裁决的数据语义不变：金额 codec 不经过中间 float；股票数量以十进制字符串保留、期权拒绝小数合约、历史 `contracts` JSON 键继续兼容。股票开仓成本只纳入已确认费用，实际零费用也要有证据；费用缺失或估算时，成本及依赖它的已实现收益为 null，数量仍发布；平仓费用缺失时已实现收益同样为 null，费用确认后通过重放恢复。

| ID | 验收目标 | 输入 / 条件 | 通过判据 | 证明方法 | 环境 | 模拟边界 | 前置条件 | 证据状态 |
|---|---|---|---|---|---|---|---|---|
| A1 | 资产判别统一（S1） | 期权、股票成交各一 | `TradeEvent`/`PositionLot` 均含 `asset_type ∈ {stock,option}`，股票 lot 不再用 `contracts`/`premium` 字段 | 读 `domain/domain/ledger/events.py`/`lots.py` 字段定义；跑 `tests/test_ledger_projection.py`、`test_assigned_stock_projection.py` | 本地 venv | 字段结构可静态断言；投影行为需真实事件样本或 fixture | `tests/fixtures` 现有样例 | 核对当前测试结果 |
| A2 | 策略层收敛（S3） | Combo/Wheel 的 lot 读取路径 | `_Lot` 不再定义 `contracts_original`/`multiplier: str`/`strike: str`；Wheel 候选不再 `net_premium`/`net_income` 双名 | 读 `combo_reconciliation.py`/`wheel.py`；跑 `tests/test_combo_reconciliation_domain.py`、`test_wheel_strategy.py` | 本地 venv | 静态删除字段 + 回归断言 | 同上 | 核对当前测试结果 |
| A3 | 旧数据兼容（S5） | 存量无 `asset_type` 的旧期权 `trade_events` | 兼容读缺省推导 `option`，投影出的 lot 与迁移前等价 | 跑 `tests/test_ledger_migration.py`、`test_position_projection_migration.py`、`test_resumable_projection_state.py` | 本地 venv | 需存量事件 fixture；无真实生产数据则用构造样本 | 迁移 fixture | 核对当前测试结果 |
| A4 | 命名类型单一（S4） | 各权威结构字段 | 金额/价格 Decimal、乘数 int、时间 ms、数量 int（期权）/ Decimal（股票）+unit，无 float 承载金额 | 静态检查 + `tests/test_architecture_guards.py`（import 边界）+ 类型校验 | 本地 venv | 类型断言；金额精度需 Decimal 构造样本 | 同上 | 核对当前测试结果 |
| A5 | 订单归组不变（S2） | 一笔多成交订单 + 订单级费用 | `order_fee_sync` 仍按 `order_id` 归组摊派，结果与迁移前一致 | 跑 `tests/test_order_fee_sync.py`、`test_order_fee_settlement.py`、`test_order_fee_namespace.py` | 本地 venv | 费用归组逻辑可纯函数化 | 订单/成交/费用 fixture | 核对当前测试结果 |
| A6 | 旧记录层退役（S4） | 投影与读模型路径 | 读模型以 `PositionLot.to_dict()` 为准，`PositionLotFields`/`OpenPositionCommand` 不再作为权威 | 读 `src/application/ledger/read_model.py`；跑 `tests/test_ledger_module_facades.py`、`test_position_projection_facade_inventory.py` | 本地 venv | 读模型字段映射回归 | 同上 | 核对当前测试结果 |
| A7 | 跨界校验收敛（S6） | 设计文档 §10.3 的 5 家族约 37 行号引用 | ①`contracts × multiplier` 收敛为单一换算函数；<br>②`stock_settlement` 的 `expected_side` 映射 + `shares==multiplier×contracts` 不变式收敛为单一校验器；<br>③执行身份/经济冲突仲裁收敛为单一仲裁器；<br>④无 `get(a) or get(b)` 双名回退残留（兼容读集中到归一化层）；<br>⑤费用/币种仲裁收敛到费用语义层 | 读设计文档 §10.3 落点文件（`lifecycle_allocation.py`/`writer_lifecycle_support.py`/`deal_identity.py`/`trade_execution.py` 等）；grep 校验无业务层散落乘法与双名回退；跑相关回归测试 | 本地 venv | 静态收敛断言 + 回归；跨界换算需 assignment 定向用例 | 同上 | 核对当前测试结果 |
| A8 | 列退役不产生「不可用」库（D1/D2） | 一个旧形状 store，含非空 `position_lots` | 重建后列合同闭合，无 `column_contract_open`，head 不落 `untrusted`；R2 普通开库只接受最终结构 | 生产证据目录的迁移与 required follow-up rebuild 读回；`tests/test_lot_identity_migration.py`、`tests/test_ledger_lot_identity_schema_guard.py` | 本地 venv + 生产迁移读回 | 旧形状只通过受控迁移入口 | 设计文档 §12.3 | R1 production verified; R2 local verified |
| A9 | 重建的原子性与校验（D1/D2） | 重建中途注入失败 | 行数、读回等值、`foreign_key_check` 任一不通过则整体回滚，旧表原样保留 | 生产证据目录的校验读回；`tests/test_lot_identity_migration.py` 的失败注入与原样读回用例 | 本地 venv + 生产迁移读回 | 生产形态不可注入 | 同上 | R1 production verified; R2 local verified |
| A10 | payload 重写可回滚（D3/D4） | 一次遍历重写全部 `position_lots.fields_json` | 重写后读模型输出等价；窗口 `.backup` 副本可逐行比对，`integrity_check=ok`，哈希与幂等读回成立 | 生产证据目录的 backup、逐行/哈希、完整性与幂等证据；`tests/test_lot_identity_migration.py` | 本地 venv + 受控窗口 | 写窗口已关闭 | §7.1 第 2 条 | R1 production verified; R2 local verified |
| A11 | 迁移形态与门控（全批次） | 升级与普通开库 | 数据改写仅由受控命令触发；R2 普通开库对旧/部分结构只读拒绝 | 生产证据目录；`tests/test_option_positions_cli.py`、`tests/test_ledger_lot_identity_schema_guard.py` | 本地 venv + 生产升级读回 | `apply` 只保留只读 preview，写入口禁用 | 设计文档 §9.5 M1/M5 | R1 production verified; R2 local verified |

## 7. 存量数据处理

存量 SQLite ledger 中无 `asset_type` 的旧期权事件，通过兼容读缺省推导 `option` 处理；不迁移、不改写历史事件，只在新代码读取时补缺省。迁移后新事件一律显式 `asset_type`。旧记录层（`PositionLotFields`/`OpenPositionCommand`）随读模型切换退役，不保留双写。

以上「不迁移、不改写」的定界适用于**代码语义收敛批次**（已随 #311 合入 main）。**列退役批次（D1–D4）另行改写存量**，见下节。

### 7.1 历史列退役批次的存量处理要求（D1–D4）

以下为已关闭迁移窗口的处理约束，用于恢复旧备份时追溯；当前版本的写入口已退役。

1. **生产方式不得依赖「升级即自动改库」。** 历史开库 bootstrap 缺少 dry-run 与备份，不能承载破坏性迁移；全部改写必须由操作者显式触发并可先备份——已裁定 **D1–D4 一律走「声明即止 + 显式命令迁移」**（设计文档 §9.5 M1），并必须提供 preview/dry-run（§9.5 M5）。
2. **必须有可回滚的窗口流程。** 备份用 SQLite `.backup` API（禁止裸 `cp`）、`integrity_check` 必须为 `ok`、源库原样保留作为回滚证据；窗口内停**所有**账本写入方。流程沿用 `docs/DEPLOY_LINUX_MAC.md:403-438`，不新造。
3. **结构改动与列分类合同必须同批改。** `POSITION_LOTS_COLUMN_CLASSIFICATION`（`repository_common.py:99-108`）是开库合同的一部分；漏改会让重建成功但库停在 `untrusted`，即「改成功了却不可用」。
4. **迁移的权威仍是 `fields_json`。** D1 退役的 `position_lots.expiration` 是派生镜像，D2 改的是身份列名；两者的数据真源都在 payload，迁移不得引入第二个真源。

执行设计（机制事实、形态对照、重建配方、逐项落点、⑥ 的前置判定）见设计文档 §12；**执行决策（形态/D2 改法/⑥ 顺序/身份同一性/dry-run/落地顺序）已收口于设计文档 §9.5 M1–M6**。

## 8. 证据边界

产品决定已确认。`PositionLot` 当前仍内嵌 `contract_key`，与早期技术设计的扁平身份目标存在差异，见技术设计 §1/§4.3。跨界收敛需按真实消费者及测试核验；不以旧行号或计数作为当前完成证据。

## 9. 批准记录

| 决定 | 用户原话（摘录，无编造时间戳/消息 ID） |
|---|---|
| 统一领域模型，一个权威设计 | 「先做归一化的领域模型设计，明确期权订单包含哪些字段、股票订单包含哪些字段，全库应该只有一个权威设计」 |
| 各模块共用一套，不各自搞 | 「接下来我们把订单和交易的数据结构确定下来，各个相关模块都使用同一套设计，不要各自搞一套」 |
| 不搞 v2、原地优化 | 「不要在代码里搞v2，直接优化，下一次继续解决三个待确认」 |
| Order = 入口归组层 | AskUserQuestion 选「仅入口归组层（推荐）」 |
| 命名以 ExecutionInput 为基准 | AskUserQuestion 选「沿用 ExecutionInput（推荐）」 |
| 三个口径落定 | 「继续解决三个待确认」后于本会话定稿（§5 第 4/5/6 条） |
| 跨界校验收敛为 5 家族；两文档审查精简 | 「扩大范围再找找，还有没有类似wheel校验的代码块没有被考虑进来？」→「要补，要统一领域模型」→「看看现在的prd有没有可以优化，简化的地方」「看看设计文档」→「全部执行」 |
| 开列退役批次，先落需求与设计 | 「开迁移批次」→ AskUserQuestion 选「先落需求+设计文档」 |
| 迁移批次执行决策五条（形态/D2 改法/⑥ 顺序/身份同一性/dry-run） | 「你的建议是啥」→「按建议，落进 §9 已定决策」 |

## 10. 当前迁移与兼容边界

- 代码语义收敛批次已合入主线，R1 迁移窗口记录见 §6；这些记录不证明任意恢复库或目标主机的现状。
- `inventory` / `verify` 及 `apply` 的 preview 为只读历史诊断；普通构建的 `--apply` 写入已禁用。
- 历史事件兼容读继续保留；旧 `position_lots` / `wheel_events` 结构须由历史受控迁移处理，普通 repository 不修复。
- 技术设计 §9.4–§12 保留历史迁移依据，当前操作说明归 [Ledger Architecture](LEDGER_ARCHITECTURE.md)、[Option Positions Repair](OPTION_POSITIONS_REPAIR.md) 和 [Guardrails](GUARDRAILS.md)。

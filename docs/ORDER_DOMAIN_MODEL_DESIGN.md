# 期权 / 股票订单统一领域模型设计

> 状态：设计定稿（未实现）。目标：全库只有一个权威的订单/成交/持仓字段定义。
> 已定决策：① Order 不作为持久化实体，仅作「成交的入口归组层」；② 权威命名以 `ExecutionInput`（`trade_execution.v1`）为基准；③ 实现采用**原地优化，不引入 v2 版本化**（直接改现有 `ContractKey`/`TradeEvent`/`PositionLot`，旧数据用兼容读缺省推导）；④ 三个原待确认口径已在 §9 落定。
> 本文只定义「字段与语义」，不改代码；实现另开计划。
> 修订记录（Improve Design 四路评审后合批写回）：补 `event_type` 账本事件轴、`position_side` 定为派生（不存储）、Order 归组四元主键、`fees`/`currency`/`multiplier` 归属与不变式、`PositionLot` 身份落点、策略元数据唯一 home、`premium_open` 每张口径、§10.3 落点计数更正、术语统一为 `ExecutionInput`。

## 1. 摘要

项目里订单/成交/持仓的「领域模型」分散在 **约 20+ 套**互不相同的表示中，字段命名与类型对不齐。根因不是「没有模型」，而是：

1. **事实层与投影层是「期权专属」的**：`ContractKey`/`TradeEvent`/`PositionLot` 只有 `option_type`/`strike`/`expiration_ymd`/`contracts`/`premium_open`，没有 `asset_type`。股票被硬塞进期权形状，股票持仓另起 `shares`/`cost_basis`/`stock_lot_id` 一套词汇。
2. **「订单（Order）」没有正位**：权威账本从 `ExecutionInput` 起就是成交粒度，`external_order_id` 只是可空引用；真正以订单为主键的只有费用同步、订单回查、通知文本「建议挂单」三处，各写各的字段。
3. **策略层各自重声明**：Combo 的 `_Lot`（`contracts_original`、`multiplier: str`）、Wheel 的候选（`net_premium`/`net_income`、`stock_lot_id`）在账本之上又各造一套字段。

本设计：**以 `ExecutionInput` 为唯一权威事实结构，把事实层与投影层推广为 asset_type 判别，并把「订单」明确定为入口归组层**——所有模块共用这一套，不再各自搞。

## 2. 设计原则

1. **单一事实源**：`trade_events -> deterministic projection -> position_lots` 仍为唯一权威链路（沿用 `docs/LEDGER_ARCHITECTURE.md`）。
2. **单一定义点**：订单/成交/持仓的数据结构只在 `domain/domain/` 定义一次，别处只 import 不重写（见 §6.1）。
3. **Order 是归组层，不是事实**：订单只承载「委托意图 + 状态 + 分组」，不参与 lot 投影；`ExecutionInput`（成交事实）才是进账本的经济事实。
4. **asset_type 判别，而非双账本**：期权/股票共用一套骨架，用 `asset_type ∈ {stock, option}` 区分特有字段。
5. **精简（parsimony）**：不新增持久化实体，只做「归位、定名、定类型」。
6. **命名与类型单一**：同一语义只允许一个字段名、一种类型（见 §7）。
7. **原地优化，不搞 v2**：统一时直接改现有 `ContractKey`/`TradeEvent`/`PositionLot`，旧数据用兼容读（缺省推导 `option`），不新建并行的 v2 类或版本化 schema。

## 3. 权威模型总览

```text
外部成交/订单 (Futu order/deal、OpenD、文件、手动文本)
        │  各自 adapter 归一化到 ExecutionInput（订单只作可空引用 + 入口归组）
        ▼
ExecutionInput (trade_execution.v1)  ───────────  唯一事实，asset_type 判别
        │  → TradeEvent（账本事件，ExecutionInput 的持久化形态）
        ▼
PositionLot  ──────────────────────────────────  投影层（持仓手，asset_type 判别）
        │  策略投影 / 元数据挂载
        ▼
Combo / Wheel 投影 & 元数据  ───────────────────  策略层（只读投影，不重复声明 lot 字段）
```

- **订单 = 入口归组**：`external_order_namespace`/`external_order_id` 是可空引用，不参与 lot 投影。

## 4. 权威字段定义（以 ExecutionInput 命名）

### 4.1 ExecutionInput（成交事实，唯一权威结构）

> 对应现状：`domain/domain/trade_execution.py` 的 `normalize_execution_input()` 产物（`trade_execution.v1`）。本设计以它为准，**不改名、不重排**，只补 `asset_type` 语义与类型收紧。
> `TradeEvent`（`domain/domain/ledger/events.py`）是其**账本持久化形态**：`ExecutionInput → TradeEvent` 的映射见 §5。注意 `TradeEvent` 另有账本侧字段（`event_type`/`fees`/`source`/`target_lot_id`/`target_event_id`/`lot_id`）不在 `ExecutionInput` 里，由入账层补充（§5）。

#### 4.1.1 `broker_account_ref`（账户）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `broker_account_id` | str | 是 | OM 稳定内部身份 |
| `broker_id` | str | 是 | 券商（小写，如 `futu`） |
| `external_account_id` | str | 是 | 券商物理账户 |
| `environment` | str | 是 | `REAL`/`SIMULATE` |
| `account_label` | str | 否 | 现有 `lx`/`sy` 路由标签 |

#### 4.1.2 `instrument_ref`（合约，asset_type 判别）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `asset_type` | enum `stock`\|`option` | 是 | 资产判别 |
| `symbol` | str(规范) | 是 | 股票=证券代码；期权=underlying |
| `market` | str | 是 | `us`/`hk` |
| `currency` | str | 是 | `CNY`/`HKD`/`USD` |
| `source_code` / `source_security_type` | str\|None | 否 | 来源证券代码/类型 |
| `option_type` | enum `put`\|`call` | 仅 option | 期权类型 |
| `strike` | Decimal | 仅 option | 行权价 |
| `expiration_ymd` | date `YYYY-MM-DD` | 仅 option | 到期日 |
| `multiplier` | int | 仅 option | 乘数（默认 100） |
| `deliverable` | {`quantity`,`amount`,`multiplier`,`ratio`} | 仅 option 可选 | 交割规格 |

> **归属与不变式**：`currency` 以 `instrument_ref.currency` 为权威，成交本体 `currency` 为派生/复制，两者必须一致（冲突即 `invalid:currency:instrument_mismatch`，现状已校验；落库基线 `541c19c8` 在 Futu deal 归一化新增 `symbol_currency(symbol)` 派生来源，属 §10.3 家族 ⑤ 的收敛范围）。`multiplier` 是期权合约属性，权威存储只在 `instrument_ref`（§10.2），`TradeEvent`/`PositionLot` 侧只读投影、不重复存储。

#### 4.1.3 成交本体（Execution，资产无关）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `data_type` | enum `execution`\|`order_summary` | 是 | 粒度；`execution` 入账，`order_summary` 为边界哨兵（订单级汇总行，不入账，归一化层以 `unsupported:data_type:execution_required` 拒收） |
| `external_id_namespace` | str | 是 | 成交 ID 命名空间（如 `futu_deal`） |
| `external_execution_id` | str | 是 | 券商成交号 / Deal ID |
| `external_order_namespace` | str | 否 | 订单命名空间（**订单引用**） |
| `external_order_id` | str | 否 | 券商订单号（**订单引用**） |
| `side` | enum `buy`\|`sell` | 是 | 买卖方向 |
| `position_effect` | enum `open`\|`close`\|`void`\|`adjust` | 是 | 开平 |
| `quantity` | int(option) / Decimal(stock) | 是 | 数量；期权=张（强制整数），股票=股（允许小数股，现状 `integer=asset_type=="option"`） |
| `quantity_unit` | enum `share`\|`contract` | 派生 | 数量单位，由 `asset_type` 派生（`stock→share`、`option→contract`）；显式给必须等于派生值，否则 `invalid:quantity_unit`。不变式 `quantity_unit==share ⟺ asset_type==stock` |
| `price` | Decimal | 是 | 成交价 |
| `currency` | str | 是 | 币种（派生自 `instrument_ref.currency`，必须一致，见 §4.1.2 归属注） |
| `occurred_at_utc` | ISO-8601 UTC | 是 | 成交时间（外部边界） |
| `evidence_refs` | [str] | 否 | 证据引用 |
| `source_time` / `source_timezone` | str\|None | 否 | 来源时点/时区 |

> **`ExecutionInput` 的管辖边界**：它只覆盖「券商 deal 入账」这一条事实源。账本里另有一条 schema 同构的第二事实源——`assignment`/`exercise`/`expire_close`/`repair`/`verification` 等生命周期事件不经 `ExecutionInput`（无 `external_execution_id`），由 `commands`/`lifecycle` 直接构造 `TradeEvent` 入库。二者共用 `TradeEvent` 形状，只有 deal 走 `data_type=execution`（§10.1）。
> **`position_side`（long/short）是派生投影字段，不存储**：由单一 `derive_position_side(position_effect, side)` 生成（`open+buy→long`、`open+sell→short`、`close+buy→short`、`close+sell→long`）；`void`/`adjust` 不改变目标 lot 方向（记 null 或随父 lot）。旧事件兼容读从 `contract_key.position_side` 缺省推导（§9.2）。

### 4.2 Order（入口归组层，**非持久化实体**）

> 订单不作为账本实体持久化。它只以两种形态存在：
> 1. **可空引用**：`ExecutionInput.external_order_namespace` / `external_order_id`（见 4.1.3）；
> 2. **入口归组**：费用摊派时以四元组 `(broker, account, futu_account_id, order_id)` 为组（现状 `order_fee_sync.py` 的 `_identity` 已在做；`broker` 为券商、`account` 为 `lx`/`sy` 路由标签、`futu_account_id` 为券商物理账户、`order_id` 为券商订单号）。`external_order_namespace` 只是校验/输出元数据，**不进归组主键**。

订单级的委托意图与状态（`order_type`/`limit_price`/`ordered_quantity`/`status`/`filled_quantity`）如需暴露，只在适配器层组装，**不进账本**。券商订单状态、委托量 vs 成交量的对账属于 `src/application/trades/` 的入口职责，不形成新的持久化事实源。

### 4.3 PositionLot（持仓手，asset_type 判别）

> 对应现状：`domain/domain/ledger/lots.py`（期权）+ `assigned_stock_events`（股票）。本设计把股票 lot 提升为与期权 lot 同构的一等实体。
> **身份载体**：`PositionLot` 直接含 `asset_type`（判别器）与身份字段 `broker`/`account`/`symbol`/`market`/`currency`（复用 §4.1.2 `instrument_ref` 结构；option 额外 `option_type`/`strike`/`expiration_ymd`）。现状内嵌的期权专属 `contract_key` 改由该身份载体承接；股票 lot 身份 = `broker`/`account`/`symbol`/`market`/`currency`，不再依赖期权专属 `ContractKey`。

#### 4.3.1 共用生命周期

`lot_id` / `open_event_id` / `opened_at_ms` / `status`(open\|close) / `realized_pnl`(Decimal) / `last_event_id` / `close_event_ids` / `position_side`（**派生**，§4.1.3）。策略元数据（`strategy`/`leg_role`/`strategy_group_id`/`source_stock_lot_id`/`source_wheel_branch_id`/`strategy_snapshot`）**不进 `PositionLot` 权威结构**，挂在策略投影侧（§7.5）。

#### 4.3.2 期权数量与成本（asset_type=option）

`contracts_opened` / `contracts_open` / `contracts_closed`（int）、`premium_open`（Decimal，**每张开仓价** = 开仓 event 的 `price`；总权利金不存储，派生 `premium_open × contracts × multiplier`）、`multiplier`（int，只读投影自 `instrument_ref`）。

#### 4.3.3 股票数量与成本（asset_type=stock）

`shares_opened` / `shares_open` / `shares_closed`（Decimal，允许小数股）、`cost_basis_total`（Decimal，含费总额，口径见 §9.1）。除这三态数量 + `cost_basis_total` 外，`assigned_stock` 现状字典其余字段（`assignment_price`/`assignment_notional`/`stock_cost_per_share`/`covered_call_pnl`/`sale_event_ids`/`_fee_facts`/`_sale_rows` 等）一律为 wheel 投影专有，不进权威 `PositionLot`。

## 5. 现有表示 → 权威映射

| 现状字段 | 归属 | 权威字段 | 动作 |
|---|---|---|---|
| `TradeEvent.event_id` | 账本 | `ledger_event_id`（= event_id，避免与外部执行身份 `execution:v1:…` 撞名） | 对齐 |
| `TradeEvent.event_time_ms` | 账本 | `occurred_at_utc`→毫秒 | 边界转换（§6.2） |
| `TradeEvent.event_type` | 账本事件轴 | `event_type`（open/close/expire_close/assignment/exercise/adjust/void/repair/verification，五类） | 保留不动（一等字段，不被 `position_effect` 取代） |
| `TradeEvent.fees`(float) | 账本经济 | `fees`(Decimal) | 改类型；来源=订单级费用归组挂载（§8.1 ⑨），非 `ExecutionInput` 原始字段 |
| `TradeEvent.source`/`target_lot_id`/`target_event_id`/`lot_id` | 账本关联 | 保留为账本侧字段（不在 `ExecutionInput`） | 定名 |
| `TradeEvent.contracts` | 账本(option) | `quantity` + `quantity_unit=contract` | 归一 |
| `TradeEvent.price`(float) | 账本 | `price`(Decimal) | 改类型 |
| `TradeEvent.multiplier`(float) | 账本 | `multiplier`(int) | 改类型 |
| `ContractKey.underlying_symbol` | 身份(option) | `instrument_ref.symbol` | 归一 |
| `ContractKey.position_side` | 身份→成交 | 派生 `position_side`（`derive_position_side(position_effect, side)`，不存储） | 移出身份 |
| `PositionLotFields.position_id` | 旧记录 | `lot_id` | 退役 |
| `PositionLotFields.opened_at`/`expiration`(ms) | 旧记录 | `opened_at_ms`/`expiration_ymd` | 归一 |
| `OpenPositionCommand.premium_per_share` | 旧命令 | `price` | 退役 |
| `assigned_stock_events.shares`/`quantity` | 股票 | `quantity`(share) / `shares_*` | 归一 |
| `assigned_stock_events.stock_lot_id` | 股票投影 | `lot_id` | 改名 |
| `order_fee_sync` 的 `order_id`/`external_order_id`/`dealt_quantity` | 入口归组 | `external_order_id` + `Order` 归组 | 归位 |
| combo `_Lot.contracts_original` | 策略投影 | `contracts_opened` | 改名 |
| combo `_Lot.multiplier`/`strike`(str) | 策略投影 | `multiplier`(int)/`strike`(Decimal) | 改类型 |
| combo `_Lot.position_side`/`market_date`/`leg_role` | 策略投影 | lot 元数据 | 归位 |
| wheel `net_premium`/`net_income` | 策略候选 | `price` | 去双名 |
| wheel `stock_lot_id`/`option_record_id` | 策略投影 | `lot_id`/`source_stock_lot_id` | 归位 |
| wheel `branch_generation_hash`→`batch_generation_hash` | 遗留别名 | `batch_generation_hash` | 清退旧名 |
| wheel `active_option_lot_ids`→`active_call_lot_ids` | 遗留别名 | `active_call_lot_ids` | 清退旧名 |
| wheel `remaining_stock_cost_basis`/`realized_*_net_pnl`/`shares_remaining` | 策略投影 | 保留为 wheel 投影专有字段 | 明确边界 |
| PM `holdings`/`positions`（openapi） | 外部契约 | `PositionLot`(stock) 读模型 | 适配层转换 |

## 6. 各模块如何共用一套（硬约束）

1. **单一定义点**：`ExecutionInput`（成交事实，`trade_execution.v1`）、`TradeEvent`（账本事件）、`ContractKey`（合约身份）、`PositionLot`（持仓投影）只在 `domain/domain/` 定义。`src/` 一律 `from domain.domain.… import …`，**禁止**自定义 order/trade 类（Combo `_Lot`、Wheel 候选自造字段、Inbox 裸 dict 均删除）。`Order` 无独立 dataclass，只作为 `external_order_namespace`/`external_order_id` 可空引用 + 归组键存在。
2. **适配器进、投影出**：只有「适配器」把外部数据转成 `ExecutionInput`；只有「投影」从 `TradeEvent` 推出 `PositionLot`。中间层与策略层不碰原始字段名。
3. **序列化只在边界**：dataclass 是内部唯一形态；dict/JSON 只出现在 SQLite `event_json`、Inbox `payload_json`、文件 JSONL 三处，格式 = schema 版本 + 权威字段。`schema_version` 保留为稳定常量 `trade_execution.v1`，永不递增到 v2；「不搞 v2」禁止的是平行 v2 类/第二套 schema，不是这个稳定标签。
4. **方向/开平单一路径**：`side`/`position_effect` 统一走 `normalize_trade_side`/`normalize_position_effect`（`trade_contract_identity.py`），策略层不得自造别名映射。

## 7. 命名与类型规范

### 7.1 身份与键
- 写作目标身份统一 `lot_id`（废弃 `record_id`/`position_id`/`stock_lot_id`/`option_record_id`）。
- 聚合/展示键 `position_key`（派生值，非写入目标）。

### 7.2 时间
- 事实与投影统一 **epoch 毫秒 int**（`*_ms`）；`occurred_at_utc`（ISO）只允许出现在外部输入/输出边界。
- 到期日统一 `expiration_ymd`（`YYYY-MM-DD`），废弃 `expiration`(ms) 并存。

### 7.3 数量
- 统一 `quantity` + `quantity_unit`（`share`|`contract`），由 `asset_type` 决定。
- 类型：期权数量 int（张，强制整数）；股票数量 Decimal（股，允许小数股，现状 `integer=asset_type=="option"` 即此契约）。
- 投影三态数量按单位命名：option `contracts_opened/open/closed`，stock `shares_opened/open/closed`；废弃 `contracts_original`。

### 7.4 金额
- 权威层金额/价格统一 `Decimal`（JSON 用十进制字符串）；废弃 float 承载的 `price`/`premium_open`/`fees`/`multiplier`。
- `multiplier` 统一 int（默认 100）；废弃 combo `_Lot.multiplier: str`。

### 7.5 策略元数据
- `strategy`/`leg_role`/`strategy_group_id`/`source_stock_lot_id`/`source_wheel_branch_id`/`strategy_snapshot` **只挂在策略投影侧**（不进 `PositionLot` 权威结构），作为元数据。
- Wheel 的 PnL 汇总字段（`remaining_stock_cost_basis`、`realized_*_net_pnl`、`shares_remaining`）明确为 wheel 投影专有，不进事实层。

## 8. 迁移路径（分阶段，另开实现计划）

1. **定界**：冻结本文字段名，`ExecutionInput` 为唯一入账入口。
2. **事实层 asset_type 判别**：`ContractKey` 加 `asset_type`，`TradeEvent` 加 `asset_type`/`quantity_unit`；`position_side` 移出身份。兼容读旧期权事件（缺省推导 `option`）。
3. **股票 lot 一等化**：`assigned_stock_events` 的股票 lot 提升为 `PositionLot`（`asset_type=stock`）。
4. **清退旧记录层**：`PositionLotFields`/`OpenPositionCommand`/`option_positions` read model 退役，`fields_json` 以 `PositionLot.to_dict()` 为准。
5. **策略层归位**：Combo `_Lot`/Wheel 候选改读 `PositionLot`，删自造字段与遗留别名。
6. **类型收紧**：金额/乘数/到期日按 §7 统一，逐套加校验，跑投影 verify 回归。

### 8.1 实现范围模块清单（每块含成功标准）

> 与 §8 六步的关系：§8 是**按顺序**的迁移步骤，本节是**按架构层**的实现范围切分。每块的「成功标准」是可验证判据（绑定文件/测试），devflow 按块拆工作项。
> 路径约定：裸 `ledger/` 在 ① ② ③ 指 `domain/domain/ledger/`，在 ④ ⑨ ⑩ 指 `src/application/ledger/`；其余按表内全名。⑦ 的 `wheel_trade_companions.py` 在 `src/application/ledger/`。

#### ① 领域身份与事实层
- 涉及：`ledger/identity.py`、`ledger/events.py`、`ledger/lots.py`
- 改动：加 `asset_type`、`position_side` 移出身份、类型收紧
- 成功标准：`ContractKey`/`TradeEvent`/`PositionLot` 均含 `asset_type`；`ContractKey` 不再含 `position_side`；`strike`/`price`/`premium_open` 为 Decimal、`multiplier` 为 int。→ 判据：`test_trade_contract_identity.py`、`test_ledger_projection.py` 通过；grep 无 `strike: float`/`price: float` 定义。

#### ② 投影引擎
- 涉及：`ledger/projection.py`、`ledger/projection_state.py`、`ledger/economics.py`、`ledger/invariants.py`
- 改动：投影按 `asset_type` 判别；`position_key` 兼容读；`position_side` 读点迁移；含家族 A 数量换算收敛（§10.3）
- 成功标准：同批旧期权事件（无 `asset_type`）新投影与迁移前等价；新股票事件产出 `asset_type=stock` lot。→ 判据：`test_ledger_projection.py`、`test_resumable_projection_state.py`、`test_position_projection_migration.py` 通过。

#### ③ 旧记录层退役
- 涉及：`ledger/position_fields.py` + 消费方 `ledger/{commands,manual_trades,preflight,publisher,results}.py`、`positions/workflows.py`
- 改动：`PositionLotFields`/`OpenPositionCommand` 退役，读模型改 `PositionLot.to_dict()`；`PositionLotPatch` **保留**（是投影核心 `lots.py:apply_adjust` 的 adjust 载荷解码依赖），只迁入 `lots.py`/投影核心、不改语义，不退场
- 成功标准：消费方不再 import/使用 `PositionLotFields`/`OpenPositionCommand`；读模型以 `PositionLot.to_dict()` 为准；`PositionLotPatch` 仍供投影核心使用。→ 判据：grep 无 `PositionLotFields`/`OpenPositionCommand` 消费引用；`test_ledger_module_facades.py`、`test_position_projection_facade_inventory.py` 通过。

#### ④ 账本写入/序列化层
- 涉及：`ledger/{event_codec,repository_schema,repository_trade_schema,repository,writer_common,writer_trade_events,writer_lifecycle_*,bootstrap,migration,lifecycle,maintenance,interventions,queries,api}.py`
- 改动：构造点传 `asset_type`/`quantity_unit`；旧事件兼容读缺省 `option`；含家族 B stock_settlement 校验收敛（§10.3）
- 成功标准：新写 `TradeEvent` 持久化含 `asset_type`/`quantity_unit`；旧无 `asset_type` 事件重投影不漂移。→ 判据：`test_ledger_migration.py`、`test_ledger_event_codec.py`、`test_trade_event_ledger_long_lifecycle.py` 通过。

#### ⑤ 执行归一化层
- 涉及：`trade_execution.py`、`domain/services/source_adapters.py`、`trades/{normalizer,intake}.py`
- 改动：`ExecutionInput` 为基准，补 `asset_type` 语义 + 类型收紧；含家族 C/D 执行身份仲裁与双名回退收敛（§10.3）
- 成功标准：`ExecutionInput` 的 `asset_type`/`quantity_unit` 与 §4 一致，金额/时间类型收紧。→ 判据：`test_trade_execution_input.py` 通过。

#### ⑥ 股票层（assigned_stock 一等化）
- 涉及：`assigned_stock.py`、`ledger/current_decision_assigned_stock.py`、`ledger/repository_assigned_stock.py`、`positions/assigned_stock_quotes.py`、`portfolio_context_builder.py`、`futu_portfolio_context.py`
- 改动：股票 lot → `PositionLot(asset_type=stock)`，`avg_cost`(每股) → `cost_basis_total`(总额)；含家族 A/B 跨界换算与 stock_settlement 收敛（§10.3）
- 成功标准：股票 lot 以 `asset_type=stock` 进入 `PositionLot`，用 `shares_opened/open/closed`+`cost_basis_total`，不再有 `stock_lot_id` 独立身份。→ 判据：`test_assigned_stock_projection.py`、`test_ledger_assigned_stock_queries.py` 通过。

#### ⑦ 策略层（Combo/Wheel 收敛）
- 涉及：`combo_reconciliation.py`（domain+src）、`combo_membership.py`、`combo_identity.py`、`combo_yield_lifecycle.py`、`wheel.py`、`strategy_membership.py`、`src/application/wheel/{read_model,scanning,candidate_snapshot,capacity,workflows}.py`、`wheel_trade_companions.py`
- 改动：`_Lot` 字段改名、`net_premium`/`net_income` 去双名、别名 shim 清退、改读权威 `PositionLot`
- 成功标准：`_Lot` 无 `contracts_original`/`multiplier:str`/`strike:str`；Wheel 候选无 `net_premium`/`net_income`；`batch_generation_hash`/`active_call_lot_ids` 别名清退。→ 判据：`test_combo_reconciliation_domain.py`、`test_wheel_strategy.py`、`test_wheel_scanning.py` 通过。

#### ⑧ 方向语义层（position_side 读点迁移）
- 涉及：`option_close_reason.py`、`option_lifecycle.py`、`performance/{models,weighted_reducer,attribution}.py`、`performance/adapters.py`
- 改动：`contract_key.position_side` → lot/event side
- 成功标准：全库无 `contract_key.position_side` 读点；`position_key` 聚合改来源、字符串不变。→ 判据：grep 无残留读点；`test_ledger_economics.py` 通过。

#### ⑨ 交易/费用层（Order 归组 + deliverable）
- 涉及：`trades/order_fee_sync.py`、`ledger/{order_fee_semantics,order_fee_migration}.py`、`trades/{resolver,file_intake,receipt}.py`
- 改动：Order 归组确认（行为不变）；`deliverable` 保持可空；含家族 E 费用/币种仲裁收敛（§10.3）
- 成功标准：订单级费用按 `order_id` 归组行为与迁移前一致；`deliverable` 可空且账本不消费。→ 判据：`test_order_fee_sync.py`、`test_order_fee_settlement.py`、`test_trades_resolver_*.py` 通过。

#### ⑩ 读模型/CLI 边界
- 涉及：`ledger/{read_model,publisher,views}.py`、`positions/inspection.py`、`agent_tools/positions.py`、`assistant/renderer.py`、`interfaces/cli/{option_positions,wheel}.py`
- 改动：`position_id`/`record_id` → `lot_id`/`position_key`，展示字段改名
- 成功标准：读模型与 CLI 以 `lot_id`/`position_key` 为准，无 `position_id`/`record_id` 残留。→ 判据：`test_position_projection_publication.py`、`test_ledger_publisher.py` 通过。

#### ⑪ 外部基础设施（边界确认）
- 涉及：`infrastructure/futu_gateway.py`、`futu_history_deals.py`、`external_services.py`
- 改动：order/deal 数据面契约不变
- 成功标准：`futu_gateway` 无行为改动。→ 判据：`test_trades_futu_detail_lookup.py` 通过。

#### ⑫ 测试层（回归）
- 成功标准：上述全部测试 + 完整 suite 回归通过，投影 fingerprint 一致。→ 判据：`om-pre-push-checks` 通过。

## 9. 已定决策（原待确认项）

### 9.1 股票 `cost_basis` 口径 → 存 lot 总额，派生每股

- **权威字段**：`cost_basis_total`（Decimal，含费用）= `assignment_notional + assignment_fees`，对应现状 `assigned_stock.py:1381` 的 `stock_cost_basis_total`。
- **派生字段（不存储）**：`cost_basis_per_share = cost_basis_total / shares_opened`，对应现状 `_lot_basis_per_share_with_fees`（`assigned_stock.py:844`）。
- **依据**：外部 Futu 给的是每股 `avg_cost`，适配层已在 `portfolio_context_builder.py:266` / `futu_portfolio_context.py:712` 转为 `known_cost_total = avg_cost × shares`；费用总额是精确事实，存总额能无损承载费用，每股由总额/股数精确派生，避免「每股先舍入再反推总额」丢费用精度。
- **边界**：每股 `avg_cost` 只出现在适配层入口，进账本即转总额。

### 9.2 `position_side` 移出身份 → 三步迁移

1. **先加（向前兼容）**：新增单一派生函数 `derive_position_side(position_effect, side)`（§4.1.3）；旧事件缺省从 `contract_key.position_side` 读。`position_side` **不新增存储字段**（派生投影值，`ContractKey.position_side` 暂时保留）。
2. **再迁（读点迁移）**：全库 30+ 处 `contract_key.position_side` 改读派生 `position_side`（`lot` 侧派生值 / `event` 侧派生值）；`position_key` 聚合改用 lot 侧派生 side 拼接（字符串不变，仅来源变更）。
3. **后删（移除）**：从 `ContractKey` 去掉 `position_side`，`position_key` 概念下沉到 lot 层（`contract_key` 回归纯合约身份 = broker/account/underlying/option_type/strike/expiration；lot 键 = 合约身份 + 派生 side）。

> 方向已定（§5：`position_side` 移出身份）；上表只排顺序，每步独立可提交。「派生不存储」的理由：`side`+`position_effect` 对 open/close 可唯一推出 `position_side`，存一份会与 S4 单一定义点冲突；`void`/`adjust` 不改变目标 lot 方向。

### 9.3 `deliverable` → 保持可空，账本不消费

- 保持 `instrument_ref.deliverable` 可空；当前账本不消费它，遇到即 `unsupported_contract_deliverable`（`resolver.py:294`）/ `unsupported`（`position_snapshot.py:120`），防止静默误处理。
- 待将来实现交割（assignment/exercise）核算时，再单独定「交割类事件是否必填」，不在本次归一化范围内。

## 10. 实现注意项（跨界语义 + 证据仲裁边界）

> 这三条不是已定决策，而是实现时必须显式处理、否则会被 `asset_type` 判别「简单带过」的边界。来源：wheel 启动时对指派（assignment）订单的校验现状（`wheel_trade_companions.py` / `wheel.py` / `cash_facts.py`），扩大到全库后，同类病根在**跨界（期权↔股票）与执行入账边界**上共有 5 个家族、约 37 个行号引用，见 §10.3。

### 10.1 assignment 的期权 → 股票跨界语义

- **问题**：assignment/exercise 事件既带期权 `contracts`（张）又带股票 `shares`（股），是 `asset_type` 判别的跨界点；当前靠 `contracts × multiplier = shares` 硬连，散落在 `wheel_trade_companions.py:451`、`wheel.py:874`、`cash_facts.py:114` 三处各自实现。
- **处理原则**：assignment 事件在事实层定义为**期权侧事件**（`asset_type=option`、`quantity_unit=contract`）；股票交割结果作为**股票结算事实**显式挂载（`stock_settlement` 从 `raw_payload` 提升为一等字段，带 `asset_type=stock`/`quantity_unit=share`）。两侧单位由 `quantity_unit` 显式化，`shares = contracts × multiplier` 收敛为单一投影/校验函数，不再三处重复。
- **非目标**：完整交割规格（deliverable 的 ratio/amount）仍不消费（§9.3），wheel 校验不依赖它。

### 10.2 multiplier 证据可信度仲裁

- **问题**：`_multiplier_evidence`（`wheel_trade_companions.py:255-279`）的 `conflict`/`source`/`broker_settlement_pair`/`unproven` 四态仲裁（文档旧稿误作 `proven`，实为 `source`/`broker_settlement_pair`），回答「assignment 乘数 vs 源期权乘数不一致时听谁的」——这是**数据来源可信度**问题，不是字段命名问题。
- **处理原则**：字段统一（multiplier 单一 int 一等字段，§8.1 ①）只能减少「多处各存一份」导致的源头不一致，**不能消除仲裁逻辑**。目标是把 multiplier 收敛为「单一权威存储、多处只读」，使仲裁退化为「单一事实」。**权威存储归属**：`multiplier` 是期权合约属性，权威只在 `instrument_ref`（§4.1.2）；`TradeEvent.multiplier`/`PositionLot.multiplier` 只读投影、不重复存储；`stock_settlement` 场景引用源期权的 `multiplier`（家族 A）。
- **权威优先级（已确认）**：**futu 是 multiplier 最高权威信息源，发生冲突时优先信任 futu**——broker 直给 > broker settlement 反推 > bootstrap > manual；`conflict` 时以 futu 为准。仲裁逻辑保留，但优先级固定为 futu 最高，不因字段统一而改变。
- **测试**：在 `test_wheel_assignment_*.py`、`test_assigned_stock_projection.py` 之外补 assignment 跨界换算的定向用例（§8.1 块映射见 §10.3 统一结论）。

### 10.3 同类校验 / 计算家族全景（wheel 病根的完整落点）

> 下面 5 个家族**不是 wheel 独有**，是跨界与入账边界的系统性重复。统一领域模型要收敛的是 5 个家族，不是 wheel 一个函数。每个家族都是「多点各自实现/各自回退/各自仲裁」→ 收敛为「单一权威存储 + 单一校验器 + 单一仲裁器」。

#### 家族 A：期权 → 股票数量换算（`contracts × multiplier = shares`）散落

- **根因**：`multiplier` 取值源头每处不同（`lot.multiplier` / `event.multiplier` / `raw_payload` 反推 / 默认 100），同一乘法各写各的，口径偏差即静默错账。
- **落点（17 个行号引用）**：`lifecycle_allocation.py:94`、`portfolio_assignment_scenario.py:553`、`position_fields.py`（`:184-187` / `:501-505` / `:803` `_short_call_locked_shares`）、`risk_capacity.py`（`:318` / `:472` / `:599-604`）、`candidate_engine.py`（`:569` / `:610` / `:638`）、`close_advice.py`、`fee_calc.py`、`lots.py:148-150`、`economics.py`、`projection.py:476`、`assigned_stock.py`。
- **收敛**：单一 `shares = contracts × multiplier` 投影/校验函数；multiplier 单一权威存储、多处只读（§10.2）。

#### 家族 B：stock_settlement 校验族（一等结算事件校验散落 5 处）

- **根因**：`stock_settlement` 仍被当 `raw_payload` 里的散字段读；`expected_side` 映射表 + `shares == multiplier × contracts` 不变式被复制到多个文件各自实现。
- **落点**：`writer_lifecycle_support.py:1060-1140`（最完整：futu_account/symbol/price/quantity/side/time 六类 mismatch）、`order_fee_migration.py:711`、`close_reason_reconciliation.py:1037`（source_conflict）、`order_fee_sync.py:614`、`lifecycle_reconciliation.py:1401/1414`（side/quantity mismatch）。
- **收敛**：`stock_settlement` 提升为一等字段（§10.1）；`expected_side` 映射 + 数量不变式收敛为单一校验器，其余文件只消费「类型化冲突」结果。

#### 家族 C：`trade_execution` 身份 / 经济冲突仲裁链

- **根因**：`ExecutionInput` 作为唯一经济事实，其「来源身份、已存身份、经济内容、应用关联」冲突仲裁分散在 deal_identity 与 4 个 repository/writer 里，判定规则有重叠。
- **落点**：`deal_identity.py:106-160`（`identity_conflict` / `applied_association_conflict` / `economic_conflict` / `split_incomplete` / `legacy_execution_evidence_required` 五类）、`repository_trade_events.py:47/61`、`repository_assigned_stock.py:180`、`repository_trade_schema.py:30`（`identity_metadata_mismatch`）、`writer_trade_events.py:1645`（`applied_association_conflict`）。
- **收敛**：单一执行身份仲裁器（读身份 → 比经济 → 验关联），repository/writer 只抛「类型化冲突」，不再重复判定。

#### 家族 D：双重名称 fallback 链（`get(a) or get(b)`）

- **根因**：同一字段在 `event` / `key` / `raw` 三源之间、以及新旧命名之间回退；统一命名（§7）前，这些回退是「哪一份字段被塞进哪一层」的隐藏契约。
- **落点**：`trade_execution.py:767-777`（event/key/raw 三源回退链）、`combo_identity.py`（`strategy_group_id/group_id`、`contracts/contracts_open`）、`cash_facts.py:205`（`side/stock_side`）、`assigned_stock.py:214-223`（symbol/side/expiration/source 四组）、`combo_reconciliation.py`（五组）、`wheel.py`（三组）。
- **收敛**：权威字段单一命名。分两类回退分别收敛——①适配器输入别名回退 → 集中到 `source_adapters` 归一化层；②存量旧行兼容读（如 `TradeEvent.from_dict` 的 `get("underlying_symbol") or get("symbol")`，`events.py:95-108`）→ 留在账本 `event_codec`/读路径（与 §8 兼容读缺省 `option` 同源），不得塞进 `source_adapters`。业务层不再各写回退。

#### 家族 E：费用 / 币种归属冲突仲裁

- **根因**：费用归属订单哪一侧、币种是否一致——与 multiplier 仲裁同构，只是字段不同。
- **落点**：`order_currency_mismatch`（`order_fee_migration.py:470`、`order_fee_sync.py:568/605`）、`source_deal_fee_inputs_conflict` / `source_deal_fee_evidence_conflict`（`writer_trade_events.py:587-714`、`order_fee_migration.py:833`）。
- **收敛**：费用按 `order_id` 归组单一归属（§8.1 ⑨）；币种仲裁收敛到费用语义层，与 multiplier 仲裁同一套「证据优先级」框架。

> **统一结论**：5 个家族合计约 37 个行号引用（A 17 + B 5 + C 5 + D 6 文件 + E ~4），指向同一个动作——把「多点各自实现 / 各自回退 / 各自仲裁」收敛为「单一权威存储 + 单一校验器 + 单一仲裁器」。这正是 §4 统一领域模型要交付的收敛，而非 wheel 一处的修补；§8.1 的 12 块按层拆工项时，应把上述 5 个家族作为「跨层校验收敛」的验收红线挂到对应块（A→②投影/⑥股票、B→④写入/⑥股票、C→⑤执行归一化、D→⑤执行归一化、E→⑨费用层）。

## 11. 术语对照（存量 → 权威）

> 字段级存量 → 权威映射见 §5；本节只列概念级对照。

| 存量术语 | 权威术语 |
|---|---|
| order / deal / fill / execution / trade | `ExecutionInput`（成交事实；订单=入口归组引用） |
| contract_key / instrument | `instrument_ref`（asset_type 判别） |
| position_lot / option_position / assigned_stock | `PositionLot`（asset_type 判别） |

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

### 9.4 本轮 deferred：列退役（已定策略 = 只做读模型层）

> **策略（已确认）**：涉及 DB schema 的改名/退役，**本轮只落「读模型/payload 层」这一半**，**列退役记 `deferred-with-owner`**，不在本轮做存量账本数据迁移。理由：列退役需要重建表/索引/触发器 + 改写已持久化的 `fields_json`，属存量数据迁移动作，与「统一领域模型」的代码语义收敛可分离，且必须在受控迁移窗口内单独执行。
> 每条 deferred 记录「本轮已做到哪 / 还差什么 / 前置依赖」，供后续迁移窗口直接接续。

#### D1. `position_lots.expiration` 列退役（§7.2）

- **权威字段**：`expiration_ymd`（`YYYY-MM-DD`）；ms `expiration` 退役。
- **本轮已做（读模型层）**：`read_model.build_position_lot_view` / `_position_row_from_view` 不再产出 ms `expiration` 字段；`views.RiskPositionView` 删除 `expiration` 字段，展示唯一到期口径为 `expiration_ymd` + 派生 `expiration_date`/`days_to_expiration`。存量 `fields_json` 里的 ms `expiration` 仍被**读穿**（`read_model.py:128-131` 的 `expiration_timestamp_to_ymd` 兼容读）以支撑存量行。
- **未做（DB 层，deferred）**：
  1. 删列：`position_lots.expiration`。
  2. 索引：`idx_position_lots_account_expiration`（`repository_projection_schema.py:315-318`，另见 `repository_projection.py:164-166` 的 `IF NOT EXISTS` 重建）需改为不含 `expiration`。
  3. 不可变守卫触发器：`OLD.expiration IS NOT NEW.expiration`（`repository_projection_schema.py:662`）需移除该比较项。
  4. SQL 消费点：`repository_projection.py:31/39/41/59/63` 的 `SELECT/SET ... expiration`。
  5. 写入点：`publisher.py` 的 `:804` `out["expiration"] = int(expiration_ms)`（当前它喂 `_position_lot_contract_scalars` → `effective_expiration(fields)` 派生该列）。
  6. 存量迁移：改写已持久化 `fields_json`（去 ms `expiration`）+ 重建受影响表。
- **前置依赖**：受控迁移窗口（⑥ 股票层一等化**不是**硬前置，见 §9.5 M3；股票/期权两态在 `av` 层的口径已由 `publisher.py:668+` 的股票发布路径确定）。
- **Owner**：领域模型统一后续迁移批次（DB schema 迁移）。

#### D2. `position_lots.record_id` 列改名 → `lot_id`（§7.1）

- **权威命名**：`lot_id`（§7.1 统一，废弃 `record_id`/`position_id`/`stock_lot_id`/`option_record_id`）。
- **本轮已做（读模型层）**：读模型与 CLI 侧统一以 `lot_id` 为准——`views.py:31-37/76-79`、`read_model.py:172` 均为 `lot_id or record_id` 兼容读，对外只暴露 `lot_id`。
- **未做（DB 层，deferred）**：
  1. 列改名：`position_lots.record_id` → `lot_id`（**主键列**，全库约 1716 处引用）。
  2. 索引：`idx_position_lots_account_expiration` / `idx_position_lots_account_record`（`repository_projection_schema.py:318/325`）。
  3. 不可变守卫触发器：`OLD.record_id IS NOT NEW.record_id`（`repository_projection_schema.py:658`）及三处 `AFTER UPDATE OF record_id, ...`（`:670/690/712`）。
  4. 全库引用改写（repository / writer / migration / CLI / 测试）。
- **前置依赖**：按 §9.5 M2 拆两步——加列 + 回填 + 读侧优先**无窗口依赖**；删 `record_id` 与 D1 同批（**同一次重建**，故不再有「分批做会重复重建」的问题）。
- **Owner**：领域模型统一后续迁移批次（DB schema 迁移）。

#### D3. ③b `fields_json` 以 `PositionLot.to_dict()` 为准（§8.1 ③ 后半 / §8 步骤4）

- **权威形状**：持久化 `fields_json` 直接等于 `PositionLot.to_dict()`。
- **不变量（派生性）**：`fields_json` 必须是 `trade_events` 的**纯函数**——存在一次全量重放，使产物与存量在规范化后逐行相同。**任何「只活在 payload 里的事实」是模型缺陷，不是迁移时要处理的历史数据**；故本项不只是「改形状」，还要先消除这类事实（含 `note`-KV 承载的 `exp`/`strike`/`multiplier`、以及以 payload 键派生又自任载体的 `source_event_id`）。
- **判据**：一次全量重放与存量的逐行对照，对照面须覆盖 payload 的键与值、以及各派生列（`account`/`expiration`/`strike`/`multiplier`/`source_event_id`）；`updated_at_ms` 是墙钟值，明确排除在比较面外。
- **本轮已做**：`PositionLot.to_dict()` 已是 `position_lots` 规范化读路径的来源（§9.2 步骤③ 完成后，`contract_key`/`position_side`/`position_key` 形状已定），且 §7.3/§7.4 的 Decimal 序列化（`canonical_decimal_text`）已落。
- **未做（deferred）**：`publisher._base_fields_for_lot` + `_apply_lot_state_fields` 目前仍按**读模型字段集**组装 `fields_json`（含 `expiration`/`position_id`/`cash_secured_amount`/`underlying_share_locked`/`note`/`strategy_snapshot` 等读模型字段），未改为 `PositionLot.to_dict()` 的纯 lot 形状。
- **前置依赖**：股票 lot 的 `shares_*`/`cost_basis_total` 形状已稳定（`publisher.py:668+`/`:681`），不再等 ⑥ 落定；与 D1/D2 同属存量迁移批次（同一窗口，§9.5 M3/M6）。
- **Owner**：领域模型统一后续迁移批次（`fields_json` 形状迁移）。

#### D4. 存量 `fields_json` 里的 `position_id` 清洗（§7.1）

- **本轮已做（代码层，已完成）**：`position_id` 已从代码中全量退役——`build_position_id` 与其 `_fmt_strike` 辅助函数删除；`build_position_lot_fields` 与 `PositionLotPatch` 不再产出该字段；`publisher._apply_lot_state_fields` 加了 `out.pop("position_id", None)`，因此遗留行**每次重发布时**都会被清掉（`_base_fields_for_lot` 会从 open 事件的存量 `fields` 快照播种，故必须显式 pop）。读侧与展示侧（`read_model`/`views`/`maintenance`/`results`/`decision_snapshot`/`positions/maintenance_receipt`/`positions/maintenance`）已全部改读 `position_key`；投影 fingerprint 已随之刷新（见 §9.4 末）。
- **未做（deferred）**：存量 SQLite 中**从未被重新发布过**的行，其 `fields_json` 仍可能残留 `position_id`。彻底清洗需要一次性遍历 `position_lots` 重写 `fields_json`。
- **前置依赖**：与 D3 同批（都是 `fields_json` 重写，一次遍历做完）。
- **Owner**：领域模型统一后续迁移批次（`fields_json` 存量清洗）。

> **本轮切片落点（供接续）**：§8.1 ⑩ 的 `position_id`/`record_id` → `lot_id`/`position_key` 判据中，**`position_id` 侧的代码残留已清零**（`grep -rn 'position_id' src/ domain/` 只剩 §7.1 退役注释与 `out.pop`）；**`record_id` 侧只做了读模型层**（`views.py`/`read_model.py` 的 `lot_id or record_id` 兼容读保留，DB 列见 D2）。

### 9.5 存量迁移批次执行决策（§12.7 的裁定结果）

> 来源：§12.7 的五个未决项，2026-09-18 裁定「按建议」。本节是**已定决策**；§12 保留机制事实、形态对照与重建配方，不再重复理由。
> 本节的形态与顺序决策**尚未实施**，实施需另行授权（含生产迁移窗口与生产写入）。

#### M1. D1–D4 一律走 B（操作者门控），不走 A

- **决策**：四种动作全部采用 §12.2 的 **B 形态**（声明即止 + 显式命令迁移），不采用 A（开库自动重建）。
- **理由**（按分量排序）：
  1. **A 把「代码已装」与「数据已改」压成同一时刻**：`service_upgrade.py` 切 `current` symlink（`:2353`）→ 重启（`:664-680`）→ 重启后首次开库即迁移（§12.1），中间没有分离点，任何一次重启都会执行改写。B 让两者可分离。
  2. **失败模式不对称**：A 重建失败 → bootstrap 中止 → **库打不开、服务停摆**，只能回滚 release；B 的 `apply` 失败 → 库仍以旧形状可用，操作者仍在窗口内。
  3. **回滚**：A 之后回滚 release，旧代码遇到新形状库 → `column_contract_open` → `status='untrusted'` → tail 发布 `RuntimeError`（§12.1）；B 在 `apply` 之前回滚，库未被触碰。
  4. **本仓对该场景的既有选择就是 B**：分页 schema 拒绝在普通启动时扫描回填（`repository_trade_schema.py:924-926`），并提供 `om option-positions projection-migration` 门控分段命令。
- **A 的先例不构成反例**：`wheel_events` v1→v2（`repository_core.py:191-255`）重建的是**可由事件全量重建**的表，且改的不是主键身份，与「删 `position_lots` 列 / 换主键列名」不同量级。
- **附带收益**：只有 B 能提供 preview/dry-run（见 M5）。

#### M2. D2 拆两步：先加列并存；主键列删除与 D1 合并为同一次重建

- **决策**：D2 不一次重建到位。
  - **第一步（不需要窗口）**：`ALTER TABLE position_lots ADD COLUMN lot_id TEXT` → 回填 `UPDATE ... SET lot_id = record_id` → `CREATE UNIQUE INDEX ... ON position_lots(lot_id)` → 读侧一律改走 `lot_id`。
  - **第二步（进窗口）**：删除 `record_id`（含主键切换），并与 D1 的删列合并为**同一次重建**。
- **理由**：
  1. 本仓无 `RENAME COLUMN` 先例，惯用做法是「留旧列、加新列、回填」（`_add_column_if_missing`，`repository_common.py:298-301`）。
  2. **加列是开库路径唯一安全的动作**（§12.1），因此 D2 第一步不重建、不需要窗口，可随普通发布落地。
  3. 把约 1716 处引用改写（纯代码、可分批、可回滚）与破坏性 DDL 解耦；一次到位等于把两个风险叠进一个不能重来的窗口。
  4. **§9.4 D2 的前置依赖「D1 同批（同表重建，分批做会重复重建）」在本路径下不成立**：加列那步不重建，`position_lots` 全程只重建一次（即 D1 删 `expiration` 与 D2 删 `record_id` 合并的那次）。
- **D2 在代码层无语义内容的佐证**：`repository_common.py:213` `record_id = record.lot_id`（`_position_lot_storage_values` 是唯一写入口），`PositionLotRecord.lot_id` 早已叫 `lot_id`；以 `record_id` 命名的只有 SQL 列名、SQL 语句与守卫触发器三项。故 D2 的风险低于原估。

#### M3. ⑥ 不是硬前置；代码收敛先落，落库半边排进同一窗口

- **决策**：⑥（股票层一等化）**不作为 D1–D4 的正确性前置**。顺序为：⑥ 的身份引用收敛（与 M2 第一步合并）先作为普通代码变更落地 → ⑥ 的落库变更（`wheel_events.stock_lot_id` 退役）排进同一个迁移窗口。
- **依据**：
  - D1 记的「需先确认股票/期权两态在 `av` 层的口径」已成立：`publisher.py:668+` 的股票发布路径已产出 `asset_type: "stock"`、`quantity_unit: "share"`、`shares_*`、`cost_basis_total`（`:681`），D3 依赖的股票 lot 形状已稳定（§12.5）。
  - 但 ⑥ 自身含存量形状变化：`wheel_events.stock_lot_id` 是**另一张表上的持久化列**（`repository_core.py:64`，部分索引 `:96-97`，v2 重建读它 `:129-260`），必须有自己的重建。
- **同窗的理由**：窗口的固定成本是停**所有**写入方 + 备份 + 流程，重建一张表与两张表的边际成本很小，而每次重建都有独立的行数/读回/`foreign_key_check` 校验与独立回滚点。
- **可裁点**：若要把爆炸半径压到最小，可拆成两个窗口；代价是停机 + 备份 + 流程走两遍。**默认按同窗执行。**

#### M4. `record_id` 与 `stock_lot_id` 是同一身份（已查实）

- **结论**：同一身份空间的同一取值，只是两个名字——表列叫 `record_id`，wheel/assigned-stock 层叫 `stock_lot_id`。
- **证据链**：
  1. `position_lots.record_id` 的值即域模型 lot 身份：`repository_common.py:213` `record_id = record.lot_id`。
  2. 股票 lot id 生成点：`domain/domain/assigned_stock.py:283` `_assigned_stock_lot_id` → `f"assigned-stock-{event_id}"`；同一形状另见 `domain/domain/wheel.py:819`、`current_decision_assigned_stock.py:649`、`wheel_trade_companions.py:456`。
  3. 该值确实以 `record_id` 落进 `position_lots`：`tests/test_trades_state_reconcile.py:740` 断言 `applied_record_ids == ["assigned-stock-lot-a"]`，其来源是 `intake.py:657` 的 `op["record_id"]`，fixture 喂入字段名为 `target_stock_lot_id`。
- **两个限定**：
  1. 不是单一形状，而是**一个身份空间里的多个前缀族**：`assigned-stock-*`（配股承接）之外还有 `assigned-stock-sale-*`（`src/application/positions/workflows.py` 的 `:130` / `:133` / `:150`）。退役必须覆盖两族。
  2. 以上是**代码路径**结论。「每条 `wheel_events.stock_lot_id` 都能在 `position_lots` 找到对应行」属数据问题，交窗口内 `verify` 回答，**不以代码推断代替**。
- **对 M2/M3 的推论**：D2 的改名与 ⑥ 的身份退役是同一件事在两个层上的表现 → 代码半边必须合并做（否则同一片调用方要改两遍），落库半边见 M3。

#### M5. 必须提供 preview/dry-run

- **决策**：B 形态下提供三个子命令，形状沿用 `om option-positions projection-migration {inventory,status,verify,apply,activate,deactivate}`。
  - `inventory`：只读。报告形状差异 + 受影响行数（`position_lots` 待删列、`fields_json` 待清洗条数、`wheel_events` 待重建行数）。
  - `verify`：只读**真 dry-run**。在库上计算「重建后应当成立」的等价断言但不写入，报告差异。
  - `apply`：显式执行，单事务，回执含前后行数、读回结果、`PRAGMA foreign_key_check`。
- **窗口内用法**：备份 → `verify`（**go/no-go 依据**）→ `apply` → `integrity_check` → 重启 → 观察。
- **说明**：这是选 B 而非 A 的主要收益之一；A 形态下没有可插入 dry-run 的位置（§12.1）。

#### M6. 落地顺序

| # | 动作 | 进窗口 |
|---|---|---|
| 1 | D2 第一步（加 `lot_id` + **登记列分类合同** + 建唯一索引 + **写侧双写**）+ ⑥ 身份引用收敛（`stock_lot_id`/`record_id` → `lot_id`，覆盖两族前缀） | 否（普通发布） |
| 2 | `inventory` / `verify` / `apply` 子命令 + 测试（**含存量回填**） | 否（普通发布） |
| 3 | 窗口：D1 删 `expiration` + D2 第二步（PK 换 `lot_id`、删 `record_id`）+ D3/D4 `fields_json` 重写 + `wheel_events.stock_lot_id` 退役 | **是（一个窗口）** |
| 4 | `status` 复核 + 回执存档 | 窗口后 |
| 5 | **收紧发布**：`CREATE TABLE` / `POSITION_LOTS_COLUMN_CLASSIFICATION` / 守卫触发器 / 索引 DDL 切到新形状 | 窗口后（见 §13.5 R7） |

**第 1 步不含回填**（原表把「回填」写在第 1 步，与 §13.1 的非目标冲突）：全表 `UPDATE` 属改写型动作，其落点是第 2 步门控命令里的 `apply`，先例见 `repository_projection.py` 的 `:17`-`:25`（唯一生产调用点在 `position_projection_migration.py` 的 `:553`，在 `apply` 事务内）。第 1 步只做**纯增量、幂等、不改写既有数据**的动作。
**第 5 步是 Improve Design 评审补出的**：列合同是代码里硬编码的精确集合、又是发布路径的硬门，若第 3 步的 DDL / 合同 / 守卫改动与 `apply` 同批上线，则窗口前的旧形状库会立刻 `column_contract_open` → `untrusted` → tail 发布 `RuntimeError`；若窗口后不发布收紧版，新形状库又回不到闭合。**窗口期间服务运行在「两形状并存」的发布上**，这一点必须随第 5 步一并写明。

- **残留不确定**：`position_lots` 实际行数未知（读生产数据未获授权），故「重建一次 vs 两次」的绝对成本待第 3 步前的 `inventory` 报出后定；这**不影响 M1–M5 的形态选择**。
- **路径旁证**：`tests/test_trades_state_reconcile.py:715-742` 的 fixture 已把 `assigned-stock-sale-*`（事件）与 `assigned-stock-*`（lot）两族并置，可作为 M4 第 1 条限定的现成样例。

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

## 12. 存量迁移批次（§9.4 的 D1–D4）执行设计

> §9.4 记录「本轮做到哪 / 还差什么 / 前置依赖」，本节记录**怎么执行**：迁移如何到达生产、用哪种形态、按什么配方改库、哪些合同必须同步改、怎么验证与回滚。
> **本节的形态与顺序决策已于 2026-09-18 收口，见 §9.5（M1–M6）**；本节保留机制事实、形态对照与配方，作为 §9.5 的依据。

### 12.1 迁移如何到达生产（机制事实）

- **本仓没有迁移框架，也没有 schema 版本号。** `PRAGMA user_version` 全仓 0 处；没有 `schema_version` 表（同名命中都是 JSON 载荷信封）。版本靠**形状探测**：`_wheel_events_schema_is_v2`（`repository_core.py:120`）、`_trade_event_pagination_schema_ready`、`_position_projection_column_contract_is_closed`。
- **改库发生在开库路径上。** `RepositoryCoreMixin.__init__(initialize=True)` → `_init_db()`（`repository_core.py:492-899`）：整个 bootstrap 在**一个事务**内、持写锁（`_writer_connection(begin_immediate=True)`，`:467-482`），末尾 `:899` 提交。所有 `_ensure_*` 都在其中，所以**一次重建要么随 bootstrap 一起提交，要么完全不发生**。
- **开库对旧库只做加法。** `_add_column_if_missing`（`repository_common.py:298-301`）只加列，从不改类型、不改约束、不改名。**改名会同时表现为 missing 与 unclassified**，经 `_position_projection_column_contract`（`repository_projection_schema.py:22-43`）判为就绪原因 `column_contract_open`，head 写为 `status='untrusted'`（`repository_projection_tail.py:438`），tail 发布随后 `RuntimeError`（`position_projection_runtime.py:872`）。**库仍能打开，但降级为 untrusted。**
- **生产升级流程不碰账本库。** `service_upgrade.py:2527` 的步骤是：物化 release → 备 venv → 门禁 → 写运行时配置 → 切 `current` symlink（`:2353`）→ 重启服务（`:664-680`）。**没有迁移步骤、没有备份步骤、没有完整性检查。** 所以**升级重启后的第一次开库就是迁移触发点**。
- **这条路径上没有 dry-run 门，也没有自动备份。** 账本库备份是本仓文档明确交给操作者的动作：`docs/DEPLOY_LINUX_MAC.md:408-438`、`docs/OPTION_POSITIONS_REPAIR.md:134`、`docs/LEDGER_ARCHITECTURE.md:1094-1095`。

### 12.2 三种可选形态

| 形态 | 本仓先例 | 触发 | 操作者门控 | 失败代价 |
|---|---|---|---|---|
| **A 开库自动重建** | `wheel_events` v1→v2（`repository_core.py:191-255`）、通知 outbox v1→v2（`repository_lifecycle_schema.py:42-108`） | 升级重启后首次开库 | 无 | 重建在事务内失败则整个 bootstrap 中止，**库打不开** |
| **B 声明即止 + 显式命令迁移** | 分页 schema（`repository_trade_schema.py:924-926`；`docs/LEDGER_ARCHITECTURE.md:1160-1161`「旧库只声明新增列，不在普通启动时扫描回填」）+ `om option-positions projection-migration {inventory,status,verify,apply,activate,deactivate}` | 操作者显式执行 | 有（可 preview、可先备份） | 库正常打开，功能以可诊断方式不可用（如 `TradeEventPaginationUnavailable`） |
| **C 加列回填（旧列保留）** | 本仓改名的惯用做法；**全仓无 `RENAME COLUMN` 先例** | 开库自动 | 无 | 旧列与新列并存，退役目标未达成 |

**原倾向（已被 §9.5 M1 取代）**：D3/D4 走 B；D1/D2 走 A 还是 B 曾列为待裁定。理由是两类动作的损失面不同：D3/D4 遍历重写**每一行的 payload**，是本批次唯一「内容被改写」的动作，而部署路径不提供备份——本仓对该形态的既有答案就是 B。D1/D2 是结构删除，虽与 A 的先例同类，但先例跑的是**无损新增列回填**，删列与改主键列名是**有损**的，挂到「升级重启即自动执行」上与 A 的先例不同量级。

> **已裁定（§9.5 M1）**：D1–D4 **一律走 B**。上段对两类动作损失面的区分保留为理由的一部分，但结论不再区分 A/B——决定因素是「A 把代码已装与数据已改压成同一时刻」这条机制事实，对四种动作同等成立。

### 12.3 重建配方

D1/D2 的重建应照搬 `wheel_events` v1→v2 的校验型顺序（`repository_core.py:191-255`），全程在 bootstrap 事务内：

1. 形状探测（`PRAGMA table_info` + 期望索引集），已是新形状直接返回；
2. 读出全部行并在 Python 侧归一化；
3. `DROP TABLE IF EXISTS <t>_<purpose>`（临时名，保证幂等）；
4. 建新表；
5. 逐行 `INSERT`；
6. **校验行数一致**；
7. **校验读回等值**；
8. `PRAGMA foreign_key_check(<新表>)`；
9. `DROP TABLE <旧表>`；
10. `ALTER TABLE <新表> RENAME TO <旧名>`；
11. **重建守卫触发器与索引**。

任一步校验失败即 `RuntimeError`，由外层事务回滚——不会留下半迁移的表。需要派生值回填时，另参考 `repository_lifecycle_schema.py:42-108` 的 `INSERT INTO ... SELECT` + 字面回填变体。

### 12.4 逐项落点（行号已按当前树核对，§9.4 的旧行号已漂移）

**D1 `position_lots.expiration` 退役**

| 类别 | 落点 |
|---|---|
| 建表 | `repository_core.py` 的 `:522`（`expiration INTEGER`）、`:529`（`_add_column_if_missing`） |
| 索引 | `repository_projection_schema.py:318`、`repository_projection.py:164-166`（两处都要改） |
| 守卫 | `repository_projection_schema.py:657-662`（`lot_changed` 里的 `OLD.expiration IS NOT NEW.expiration`）、`:670`（`AFTER UPDATE OF ... expiration ...`） |
| 消费/写入 | `repository_projection.py:31/39/41/59/63`、`publisher.py:804`（`out["expiration"] = int(expiration_ms)`） |
| **必须同步改的合同** | `POSITION_LOTS_COLUMN_CLASSIFICATION`（`repository_common.py:99-108`）的 `"expiration": "projection-affecting"`——不改则重建成功后库仍停在 `untrusted` |
| 存量 | 无。该列是从 `fields_json` 派生的镜像，权威一直在 payload |

**D2 `position_lots.record_id` → `lot_id`**

| 类别 | 落点 |
|---|---|
| 建表主键 | `repository_core.py:518`（`record_id TEXT PRIMARY KEY`） |
| 索引 | `repository_projection_schema.py:318/325` |
| 守卫 | `repository_projection_schema.py:658`，以及 `:670/690/712` 三处 `AFTER UPDATE OF record_id, ...` |
| 合同 | `POSITION_LOTS_COLUMN_CLASSIFICATION` 的 `"record_id": "integrity/identity"` |
| 全库引用 | 约 1716 处（§9.4 计数） |

本仓**没有 `RENAME COLUMN` 先例**，惯用做法是「留旧列、加新列、回填」。要真正退役这个名字只能走重建（B，见 §9.5 M1）。

> **已裁定（§9.5 M2）**：拆两步——先「加 `lot_id` + 回填 + 读侧优先」随普通发布落地，`record_id` 的删除（含主键切换）留到窗口内，并与 D1 的删列合并为**同一次重建**。§9.4 D2 的前置依赖「D1 同批（同表重建，分批做会重复重建）」因此不再成立：加列那步不重建。

**D3 `fields_json` 以 `PositionLot.to_dict()` 为准**

- 现状：`publisher._base_fields_for_lot` + `_apply_lot_state_fields` 按**读模型字段集**组装，字段清单见 `publisher.py:613`。
- 改动：写侧改为纯 lot 形状；**读侧必须留兼容读窗口**，否则存量行读不出。
- 存量：需要一次遍历重写（与 D4 同批）。

**D4 存量 `fields_json` 的 `position_id` 清洗**

- 代码侧已完成：`publisher._apply_lot_state_fields` 的 `out.pop("position_id", None)`，遗留行在每次重发布时被清掉。
- 剩下的是**从未被重新发布过的行**，只能靠一次遍历。
- 与 D3 同批：同一次遍历完成「形状对齐 + 残留清洗」。

### 12.5 ⑥（股票层一等化）对本批次是不是硬前置

**判定：不是硬前置，但有一处必须确认。**

- D1 记的理由是「`expiration` 在股票 lot 上本就为空，需先确认股票/期权两态在 `av` 层的口径」。而**两态 payload 形状已经落地**：`publisher.py:668+` 的股票发布路径产出 `asset_type: "stock"`、`quantity_unit: "share"`、`shares_*`、`cost_basis_total`（`:681`），其 docstring 还记录了这个形状存在的理由——按期权形状组装会让股票事件在写事务深处以 `option_type` 报错。D3 所需的「股票 lot 的 `shares_*`/`cost_basis_total` 形状」已经稳定。
- ⑥ 剩下的主要工作是**退役 `stock_lot_id` 独立身份**（`src/`+`domain/` 匹配行 426 处 / 出现 560 次，`tests/` 另 238 处），落在 wheel 层；其中 `wheel_events.stock_lot_id` 是**另一张表上的持久化列**（`repository_core.py:64`，带部分索引 `:96-97`，v2 重建会读它 `:129-260`）。
- 所以 ⑥ **自身也含存量形状变化**，不是「先做完就不必进窗口」的纯代码批次。

**曾列待确认的一点（已查实，见 §9.5 M4）**：`position_lots.record_id` 与股票 lot 的 `stock_lot_id` **是同一身份**——`repository_common.py:213` `record_id = record.lot_id`，股票 lot id 由 `_assigned_stock_lot_id` 生成为 `assigned-stock-{event_id}`，且该值确实以 `record_id` 落进 `position_lots`（`tests/test_trades_state_reconcile.py:740`）。故 D2 的改名与 ⑥ 的身份退役是同一件事在两个层上的表现，**代码半边合并做**，落库半边并入同一窗口（§9.5 M3），默认同窗、可拆两个窗口。

### 12.6 窗口、备份与回滚

生产侧按本仓既有文档执行，本批次不新造流程：停**所有**账本写入方（文档明写「只停 timer 不够」，`DEPLOY_LINUX_MAC.md:403-406`）→ 用 SQLite `.backup` API 落 staged 副本（**禁止裸 `cp`**：WAL 未 checkpoint 会静默覆盖目标，`:408-435`）→ `PRAGMA integrity_check` 必须为 `ok` → `mv -n` 就位、**源库原样保留作为回滚证据**（`:437-438`）→ 迁移后回读校验并留存可核验凭据。

### 12.7 本节的未决项（已于 2026-09-18 全部收口）

裁定结果见 **§9.5**，本节不再保留未决状态，仅列出对应关系：

| # | 原未决项 | 裁定 | 落点 |
|---|---|---|---|
| 1 | 形态选择：D1/D2 用 A 还是 B（§12.2） | D1–D4 **一律走 B** | §9.5 M1 |
| 2 | D2 的改法：一次重建到位 vs 加列并存（§12.4） | **拆两步**；加列先落，删列与 D1 合并成同一次重建 | §9.5 M2 |
| 3 | ⑥ 与本批次的顺序（§12.5） | **非硬前置**；代码收敛先落，落库并入同一窗口（可拆） | §9.5 M3 |
| 4 | `record_id` 与 `stock_lot_id` 是否同一身份（§12.5） | **是同一身份**（已查实） | §9.5 M4 |
| 5 | 是否提供 preview/dry-run | **必须提供** `inventory`/`verify`/`apply` | §9.5 M5 |

本案的落地顺序见 §9.5 M6，可执行切片与验证见 §13。**上述决策尚未实施**；实施（含生产迁移窗口与生产写入）需另行授权。

## 13. 本批次实现计划（切片、复用归属与验证）

> 本节经 devflow Improve Design 四路 Panel 独立评审后改写（2026-09-18）。原稿的三类缺陷已订正：
> ① 切片 1 只加列、未要求同步登记列分类合同；② 切片 1 的「写侧继续写旧列 / 读侧优先取新列 / 回填在哪跑」
> 三处互相矛盾；③ 切片 2 的「持久化边界集合」只有散文、无法据以验收。
> 四路 reviewer 的原始结论不并入本文档，只并入**经主 agent 逐条回代码核验**的证据。

### 13.1 目标 / 非目标 / 成功信号

**目标**（§9.5 M6 的四个落点）：

1. **D2 第一步**（不进窗口）：`position_lots` 增 `lot_id` 列 → 登记分类合同 → 建唯一索引 → 写侧双写 → 读侧可用。
2. **身份名收敛（代码半边）**：把 lot 身份的**声明名**统一到 `lot_id`，含 `record_id` 与 `stock_lot_id` 两个旧名。
3. **迁移子命令**：`inventory` / `verify` / `apply`，`verify` 是只读 dry-run，作为窗口 go/no-go 依据；必须能识别 D3 计划丢弃的 payload 键。
4. **一个窗口**（未授权，不在本批次内执行）：D1 + D2 第二步 + D3/D4 + `wheel_events.stock_lot_id` 退役。

**非目标**（精确化，避免与切片 1 冲突）：

- **不在开库路径做破坏性 / 改写型迁移**：`_init_db()` 不得承担删列、主键切换、payload 重写。
  **显式豁免**：纯增量、幂等、不改写任何既有数据的 DDL（即 `_add_column_if_missing` 的既有用法）属本仓既定的加列惯例——
  `repository_core.py` 的 `:529` / `:530` / `:531` 就是这样给 `position_lots` 补 `expiration` / `strike` / `multiplier` 的。
  本批次**允许**该形态，但这是**声明的豁免**，不是默认；切片 1 必须写明它豁免于哪条非目标。
- **不在开库路径回填**：全表 `UPDATE` 属改写型动作，落点在切片 3 的门控命令（既有先例见 §13.2 第 7 行）。
- **不做开库自动重建**（§9.5 M1 裁定 B 形态）。
- **不改 §13.6 边界集合里的任何持久化名**——那是代码层收敛的硬边界，不是遗漏。
- **不碰生产**：不写生产库、不碰生产配置、不发布 / 升级 / 部署（§9.5 M6 第 3 步，需另行授权）。
- **不引入 schema 版本号**（`PRAGMA user_version` 当前 0 命中），本批次靠内容比对。

**成功信号**（每切片独立可验，且必须可回读）：

| 切片 | 成功信号 |
|---|---|
| 1 | 列已存在**且已登记列分类**；**列合同闭合**（`missing` 与 `unclassified` 皆空）、head 不落 `untrusted`；唯一索引在**非空**库上确实建成；二次开库幂等（不重复加列、不报错）；**写侧双写**使新写入行的 `lot_id` 非空；读路径对既有消费点输出等价 |
| 2 | 声明名收敛为 `lot_id`；**持久化形状零变化**；剩余命中**等于** §13.6 边界集合（**不是 0**） |
| 3 | `verify` 对 D3 计划丢弃的 payload 键给出**负例**（旧 payload 中非空即 fail）；`verify` **不得**走 checkpoint 复用短路；`apply` 在本地构造的旧形状 store 上**端到端跑通一次**（含失败回滚） |

### 13.2 复用清单（owner 归属 + 检索证据）

检索方式：`scripts/reuse_scan.py --root . --query lot_id --query record_id --query stock_lot_id --query position_lots --query projection-migration --no-text`，产物 `/tmp/reuse_scan_batch2.txt`（`schema: devflow.reuse_scan.v1`，`git_sha: 34ccf7fd…`，与本文档所在树同为 `0c97c5da…`；`python_seen/parsed = 1036/1036`，`structure_truncated: false`，`text_truncated: false`）。计数在 `src/` + `domain/` 上跑，**两种口径都给出**，因为二者都对、但含义不同：`grep -roE "\b<name>\b"` 计**出现次数**，`grep -rnwE "<name>"` 计**匹配行数**。切片 2 的验收用**行数**（对格式变化更稳）。

| # | 概念 / 名称 / 实现 | 归属 | 理由与证据 |
|---|---|---|---|
| 1 | `lot_id`（lot 身份名） | **复用** 既有领域名 | 领域记录早已叫 `lot_id`：`repository_common.py` 的 `:213` `record_id = record.lot_id`。出现 719 次 / 行 605 / 65 文件，无需新概念 |
| 2 | `record_id` → lot 身份的旧名 | **复用** 既有 owner 定义 | 6 个 owner 已核验全是 lot 身份：`results.py` 的 `LedgerWriteResult` / `BrokerTradeOperation` / `ExpiredCloseDecision`、`lot_resolver.py` 的 `LotCloseCandidate` / `LotCloseMatch`、`positions/workflows.py` 的 `ManualCloseResolvedMatch`。本项是**改名**，不新增概念（出现 1140 / 行 853 / 77 文件） |
| 3 | `stock_lot_id`（股票 lot 旧名） | **复用** 同一身份 | §9.5 M4 已查实与 `record_id` 同身份；最热为 `domain/domain/wheel.py`（75 次）、`src/application/wheel/workflows.py`（68 次）（出现 560 / 行 426 / 30 文件） |
| 4 | 加列惯用法 | **复用** `_add_column_if_missing` | `repository_common.py` 的 `:298`-`:301`；见 §13.1 的**显式豁免**——它是开库路径上的**增量**动作，与本批次禁止的破坏性迁移不同量级 |
| 5 | 唯一索引建法 | **明确不复用** `_create_index_if_table_empty` | 该 helper 在表非空时**静默返回 False 且不建索引**（`repository_trade_schema.py` 的 `:936`-`:939`）。切片 1 必须用 `CREATE UNIQUE INDEX IF NOT EXISTS`，形态照 `repository_projection.py` 的 `:155`-`:172`（其 docstring 明写面向 already populated store） |
| 6 | `strategy_group_identities` 等持久化名 | **不适用（排除）** | 见 §13.6 与 §13.5 R1。本批次不改 |
| 7 | 门控回填形态 | **复用** `backfill_position_lot_contract_columns` | `repository_projection.py` 的 `:17`-`:25`，唯一生产调用点在 `position_projection_migration.py` 的 `:553`（**在门控 `apply` 事务内**）。这是「回填不进开库路径」的既有先例 |
| 8 | 迁移子命令形态 | **复用** `om option-positions projection-migration` 的**约定**，**不复用子命令名** | 约定三条：只读 `inventory`、`--manifest` 必填、写操作走 `_add_local_write_flags(..., high_risk=True)`（`src/interfaces/cli/option_positions.py` 的 `:481`-`:524`）。但 `inventory` / `verify` / `apply` 三名字**已被投影迁移占用**且语义是 checkpoint/tail，`activate` / `deactivate` 还在生产用于开关 checkpoint 模式 → 本批次挂**新父组**（如 `lot-identity-migration`），不改现有语义 |

**空命中核对**（证明无需新概念名）：`lot_id_column_backfill` 0 命中、`position_lot_lot_id` 0 命中、`strategy_group_identities_lot_id` 0 命中。故 §13.3 不引入任何新名字。

### 13.3 实现切片（3 个，均为可独立验证的行为增量）

**切片 1 — D2 第一步：加列 + 登记合同 + 建唯一索引 + 写侧双写**

- 行为增量：库中多出一列可读的 lot 身份，**新写入的行也有值**，且列合同保持闭合。
- **必须同批做**（缺一即失败）：
  1. `ALTER TABLE position_lots ADD COLUMN lot_id TEXT`（复用 `_add_column_if_missing`，见 §13.1 豁免）。**实现期订正（落点）**：这条 DDL 与第 3 条的索引必须写在 `_ensure_position_projection_schema`（`repository_projection_schema.py` 的 `:302`）里，**不能**只写在 `_init_db` 的内联块。理由是两个调用点共享该函数：`_init_db`（`repository_core.py` 的 `:906`，仍在该次开库的同一事务内，commit 在 `:908`）与**门控迁移 `apply`**（`position_projection_migration.py` 的 `:549`，同样在事务内）。实测把 DDL 写在 `_init_db` 会让门控 `apply` 在冻结旧库上直接崩：`sqlite3.OperationalError: table position_lots has no column named lot_id`，触发者是 `tests/test_position_projection_migration.py` 的 `_legacy_store`——它用 `executescript` 手写旧形状 `position_lots`，**从不经过 `_init_db`**，`repository_projection_migration.py` 的 `:650` 另一处 `SELECT` 也走同一路径。订正前 13 个迁移用例红；
  2. **把 `"lot_id"` 登记进 `POSITION_LOTS_COLUMN_CLASSIFICATION`**（`repository_common.py` 的 `:97`），分类取 `integrity/identity`（与 `record_id` 同类）。列合同是**精确集合相等**判定：`unclassified = actual - set(expected)`（`repository_projection_schema.py` 的 `:33`），**加列与删列是同一个失败模式** → 合同不闭合 → `column_contract_open`（`repository_projection_tail.py` 的 `:355`）→ head 写 `untrusted`（`:438`）→ tail 发布 `RuntimeError`（`position_projection_runtime.py` 的 `:872`）。同步更新硬编码该集合的测试（`tests/test_position_projection_publication.py` 的 `:143`）；
  3. `CREATE UNIQUE INDEX IF NOT EXISTS idx_position_lots_lot_id ON position_lots(lot_id)`，**不得**用 `_create_index_if_table_empty`（§13.2 第 5 行）。**实现期订正（边界）**：该索引**不**加进 `position_projection_indexes_ready` 的 `required` 集合（`repository_projection_tail.py` 的 `:225`-`:230`），也**不**加进 `build_position_projection_indexes` 的 `definitions`（`repository_projection.py` 的 `:152`-`:172`）。前者是信任 / 状态门（被 `repository_projection_tail.py` 的 `:366` 与 `position_projection_runtime.py` 的 `:1052` 消费），加进去等于本切片新增一道 M6 窗口前不存在的门；后者返回的 `indexes_created` 会进迁移清单（`position_projection_migration.py` 的 `:556`），加进去会改动 `apply` 的上报值。两处都不是切片 1 的成功信号所必需——索引由 `_ensure_position_projection_schema` 在同一事务内以 `IF NOT EXISTS` 建成；
  4. **写侧双写**：`_position_lot_storage_values`（`repository_common.py` 的 `:212`-`:240`）返回 `lot_id`，且 `repository_projection_tail.py` 的 `:126`-`:132` INSERT 与 `:163`-`:172` UPDATE 的列清单加上它。**不加这一步，每条新 lot 的 `lot_id` 恒为 NULL**，「逐行相等」只在回填那一瞬成立，且 SQLite 唯一索引把 NULL 视为互不相同，所以不报错、静默漂移；
  5. **读路径不破坏既有消费点**：`position_lot_row_to_record`（`sqlite_row_codec.py` 的 `:12`-`:32`）目前只发 `"record_id"` 键，而 `queries.py` 的 `:993` / `:998` 以该键建索引（键消失即静默变空）。过渡期**两个键都发**，把 `read_model.py` 的 `:174`、`views.py` 的 `:33` / `:36`、`repository_projection_tail.py` 的 `:93` 列为必检。**实现期订正（调用面）**：该函数有 **7 个调用点**（`repository_projection_tail.py` 的 `:318` / `:756` / `:778` / `:789` / `:808`、`sqlite_row_codec.py` 的 `:63`、`position_projection_migration.py` 的 `:647`），任一喂给它的 `SELECT` 若不列 `lot_id`，`row["lot_id"]` 会 `IndexError`。故**代码**取双重保险：6 处 `SELECT` 全部补上 `lot_id`，**且**该函数用 `"lot_id" in row.keys()` 容错（`sqlite_row_codec.py` 的 `:27`-`:31`），两种合法回退——旧行的 `lot_id` 仍为 NULL、更窄的 `SELECT`——都退回 `record_id`。回退在本切片内不产生差异（两值同源相等），它保证的是「新增一列」不会把任何既有窄 `SELECT` 变成崩溃点。第一次实现时正是漏了 `position_projection_migration.py` 的 `:650`（当时用 `head` 截断了调用点检索），11 个迁移用例红。**同类的第二条读路径**：`read_only_evidence.py` 的 `_read_position_lots` 是**独立读取实现**（`mode=ro` + `query_only=ON`，不发 `fields_json` 之外的列回填），它**不能**自己加列，且必须能读**早于本批次**的库；`tests/test_trade_receipt_readback.py` 的 `:53` 断言它与 `repo.list_position_lots()` **逐字相等**，所以两条读路径必须同时双键、否则等价性当场破裂。订正方式照 §13.2 的既有惯用法（`position_projection_migration.py` 的 `:271`-`:273`）：探测列存在性，缺失时发 `NULL AS lot_id`（`read_only_evidence.py` 的 `:94`-`:99`），使**对外形状稳定**而存储差异被吸收；该契约的消费点 `auto_intake.py` 的 `:341` 只读 `row["fields"]`，故加键无副作用。同时更新 `tests/test_trade_receipt_readback.py` 的 `:56` 那条形状 pin 为三键；
- **不做**：主键、守卫触发器（`repository_projection_schema.py` 的 `:683` / `:703` / `:725` 的 `AFTER UPDATE OF` 列表）在本切片内**不动**——它们仍挂在 `record_id` 上，动它就等于提前执行 D2 第二步（窗口项）。附带事实：回填不在 `AFTER UPDATE OF` 列表里、不 bump `lots_generation`。
- **回填不进本切片**：存量行的回填落在切片 3 的门控 `apply`（先例见 §13.2 第 7 行）。

**切片 2 — 身份名收敛（代码半边）**

- 行为增量：领域 / 应用层对 lot 身份的**声明名**统一为 `lot_id`，旧名仅存活在 §13.6 的持久化边界上。
- 关键约束：**持久化形状零变化**——§13.6 列出的每一处列名 / 索引名 / 触发器 / payload 键 / 内容哈希种子 / 事件身份种子，一律不得改动。
- 规模与分批：`record_id` 在 src+domain 为 853 行 / 77 文件（出现 1140 次），tests 另 491 行 / 57 文件；`stock_lot_id` 为 426 行 / 30 文件、tests 238 行 / 26 文件。最热文件 `src/application/ledger/commands.py`（95）、`manual_trades.py`（54）、`preflight.py`（51）、`maintenance.py`（48）、`combo_membership.py`（46）。~~故本切片**按 owner 分批提交**，每批后独立验证。~~ **实现期订正（见下）**：owner 分批**不成立**，本切片**只能是单次原子改名**。
- **成功信号必须精确**：~~剩余命中**等于** §13.6 边界集合~~，**不是 0**。把目标写成 0 会直接诱使实现者去改列名或 payload 键。**实现期订正（见下）**：§13.6 的枚举**不全**，「等于 §13.6 集合」**不可满足**；判据须改为**集合包含 + 逐桶枚举**。

**切片 2 实现期订正（六条）**

1. **「按 owner 分批提交」不可执行，本切片只能是单次原子改名。** 静态检测出 **60 对跨 owner 的 def ↔ 关键字调用**；一次对照改名的实测直接产出 `TypeError: build_wheel_event() got an unexpected keyword argument 'stock_lot_id'`。改名域是一个**连通分量**，任何一个 owner 的分片都至少一侧落在别的 owner 里。落地形态因此改为：**一次原子改名 + 一次提交**（提交仍待授权）。评审批次也须相应改为**按边界类分片**，而不是按文件或按 owner。
2. **§13.6 枚举不全，「剩余命中 = §13.6 集合」不可满足。** 实测剩余命中 **64 处**，分属五个桶，**没有一处落在 §13.6 的字符串清单里**：
   - `ARGPARSE_DEST` 20——`args.<dest>`；CLI flag 是已发布接口，argparse **由 flag 拼写推出属性名**（`--record-id` → `args.record_id`），改属性名等于改 flag；
   - `CARRIER_KEY` 20——`dict(k=)` / `Namespace(k=)` / `.update(k=)` 把标识符**物化成字符串键**，以及被 `**` 展开的 dict 字面量的键；
   - `EXCLUDED` 11——`source_record_identity` / `record_id_non_null`，**本就不是 lot 身份**，在被排除集内；
   - `TEST_LABEL` 11——`test_*` 函数名，是散文；
   - `BOUNDARY_COLUMN` 2——`scripts/benchmark_data_storage_projection.py` 的 `:1684`-`:1685`，直读 `position_lots.record_id` 列得到的局部变量。
   故正确判据是 **剩余命中 ⊆（§13.6 ∪ 界面名 ∪ 载体键 ∪ 测试标签 ∪ 非 lot 名）**，且**每一桶逐条枚举、零条未归类**。§13.5 R2 的意图（目标**不是** 0）仍然成立，但它防的是「把剩余压成 0」，而不是「与 §13.6 相等」。
3. **八个边界类，NAME 改名在其中六个上不是形状中性的。** ① `dict(k=v)` / `Namespace(k=v)` / `X.update(k=v)`——标识符**本身**就是字符串键；② `**kwargs` 转发进一个**签名**——键必须跟随被改名的形参，否则 TypeError；③ argparse `dest`（同第 2 条第 1 桶）；④ 源码文本反射锚（`inspect.getsource()` + 断言名字在源码里）；⑤ 对**持久化键元组**做 `getattr(self, key)`（`PositionLotPatch`）；⑥ **语义混同**：`record_id`（期权 / 持仓 lot）与 `stock_lot_id`（被指派股票 lot）是**两个不同的 lot**（4 个 scope）；⑦ `**kwargs` 收进 dict（`def f(**extra): return {**extra}`）；⑧ `getattr(obj, "name")` / `hasattr` / `setattr`——字符串里命中的是一个**声明**，改名后**静默返回默认值**而不是报错。子类还有：`@pytest.mark.parametrize` 的 argname **字符串**；`to_dict()` 里 `dataclasses.asdict(self)` 的字段名；以及一个**远距离的 ①**——dict 字面量先绑局部、之后再 `**` 展开（`agent_tools/positions.py` 的 `_wheel_common` 返回值），任何 AST 局部规则都看不见它。
   - **类 ⑧ 是本次最大单一根因**（22 + 2 例失败）：`getattr(match, "record_id", "")` 在 `LotCloseMatch` 字段收敛后**返回空串**，表现为 `ValueError: position lot not found:`。它可被**定向证明**：AST 扫描确认 `src/` 与 `domain/` 中**不再有任何类声明 `record_id` 字段**，故对象分支上的旧拼写只能取到默认值。据此修 `commands.py`（4 处）、`position_fingerprint.py`、`writer_lifecycle_support.py`（2 处）；这些 `getattr` 的**对象分支**跟随声明改名，而**紧邻的 Mapping 分支**（`record.get("record_id")`）保持存储键——两类分支必须分别处置。
   - **类 ⑥ 的代价须明说**：统一改名成 `lot_id` 后，`record_id` 与 `stock_lot_id` 的区分**只能靠上下文**。这是本切片语义上最需要 Review 独立复核的一点。
4. **若干字符串编辑是被解释器**逼出来的**，故闸门契约只能是「经审核的允许清单」，不能是「零字符串改动」。** 例：`_wheel_common` 返回的 dict 被 `**` 打进九个 wheel 工作流，其键必须是**被调用方声明的形参名**（已是 `lot_id`），否则 `TypeError: create_wheel_call_intent() got an unexpected keyword argument 'stock_lot_id'`。同理 `manual_trade_operations._application_args()` 是一道**显式的桥**：工具 schema 的已发布键 `record_id`（`MANUAL_*_MODEL_FIELDS`）保留，进入应用层前重键为 `lot_id`。把契约写成「零字符串改动」会逼实现者去改这些**必须**改的键，反而制造真 bug。
5. **改名强制刷新 projector fingerprint。** `domain/` 内任何编辑都会使 `src/application/ledger/projector_implementation.py` 的 `EXPECTED_PROJECTOR_IMPLEMENTATION_FINGERPRINT` 失效：本切片 `0310d83e…` → `cdc0f1f648148bcd7dcadf168f83cc31bfe91325b288c5942a4bb6b9820278e8`（**重新生成**，非手改）。未刷新时表现为大量 `CurrentDecisionProjectionError: projector implementation is unavailable`，会把真实回归**掩盖成一片红**——这也是 §13.4 第 2 行必须先跑全量的理由。
6. **一处新引入的拼写分裂（留待裁决）。** `src/application/agent_tools/positions.py` 的 `:705` 现在**发出** `call_lot_id`，而 `src/application/wheel/workflows.py` 的 `:1601` 对同一概念**发出** `call_record_id`。两者各自正确（前者是被 `**` 展开的载体键，必须跟随形参 `call_lot_id`；后者是 §13.6 保留的 payload 键，`repository_common.py` / `combo_reconciliation.py` / `wheel/workflows.py` 一致按 `call_record_id` 读），但这是本切片**唯一新增**的拼写分裂。另注：`domain/domain/wheel.py` 的 `:1416` 键 `call_lot_id` 与同文件的 `:2043` / `:2053` 键 `call_record_id` 的并存是 **HEAD 既有**（HEAD 即 12 处 `call_lot_id` 对 5 处 `call_record_id`），非本切片引入，本切片的统一规则予以保留。

**切片 2 实现期实测（全部本地，无生产读写）**

- **闸门一（改名合法性）**：HEAD 与工作区**逐 token 位置比对**——改名从不增删改行，故 HEAD 的第 i 个 token 与工作区的第 i 个 token 是同一个 token，`(HEAD名 → 现名)` 映射因此是精确的。101 文件 / 337 个含改名域的 scope，断言该映射在改名域上**单射**（HEAD 的 `record_id` 与 HEAD 的 `stock_lot_id` 必须落到两个**不同**的名字）→ `INJECTIVITY OK`。字符串漂移 18 处**全部**在审核清单内（0 处未审核）；结构编辑文件（5 个：加了 `_application_args()` 桥、`to_dict()` 键重映射等）边界字面量 9 处**全部**在 vetted 清单内。
- **闸门二（判据）**：剩余 64 处逐桶归类，**0 处未归类**（口径即上方订正 2 的五桶）。
- **闸门三（类 ⑧）**：静态找出全部 `getattr/hasattr/setattr/delattr(obj, "<name>")`，仓内**仅 1 处**（`combo_membership.py` 的 `:524`，既存的**双读** `getattr(raw, "record_id", None) or getattr(raw, "lot_id", None)`，对两种形状都正确，已审核）→ `no renamed attribute reached through a string`。
- **闸门四（类 ① 的远距离形态）**：942 个被 `**` 展开的 dict 字面量键，**0 个**不是被调用方形参。
- **静态检查**：`ruff check src domain tests scripts` → `All checks passed!`。
- **全量回归**：`3 failed, 7286 passed, 2 skipped`；三项失败与基线**逐名相同**（`test_inbound_control.py::test_upgrade_worker_launcher_passes_env_file_pointer_to_systemd`、`test_service_credential_materializer.py::test_materializer_rejects_symlinked_encrypted_source`、同文件 `::test_materializer_cleanup_refuses_unexpected_entries`），`passed` 与基线同为 7286（**纯改名不新增用例**），**零新增失败**。
- **切片 3 踩过的两个 tripwire 已复跑**：`tests/test_dependency_graph_generator.py` + `tests/test_position_projection_facade_inventory.py` → `5 passed`。本切片只改了一行 import（`from typing import Any, cast` → `Any, Mapping, cast`，stdlib），**未增删任何仓内 import 边**，故无需重生成 `docs/DEPENDENCY_GRAPH.md`——这与切片 3 的情形不同，不可照搬结论。

**切片 3 — `inventory` / `verify` / `apply`（新父组）+ 测试**

- 行为增量（**不是**复述现有能力）：三条成功信号必须落在**今天尚不存在**的行为上——
  1. `inventory` 报告 D1/D2 待删列与 D3/D4 待清洗的**条数**，并输出 D1 三个合同标量（`expiration` / `strike` / `multiplier`）的**载体分布**（结构化字段 / `note` KV / 仅列），供 §13.5 R6 使用；
  2. `verify` 具备**内容侧**判定：逐行比对旧 `fields_json` 全量键 → 新 `fields_json` 全量键的差集，凡 D3 计划丢弃的键在旧 payload 中非空即判 fail。**禁用** checkpoint 复用短路——`projection_verify.py` 的 `:213`-`:245` 在 `--mode auto` 下只要指纹命中就直接合成 `items=[{"status":"matched"}…]` 并返回 `ok: True`、`mode_used: "checkpoint_reuse"`，**完全不重放**；一个永远返回 ok 的 `verify` 也能通过形状式断言；
  3. `apply` 承载 D1–D4 的重建配方与**存量回填**（复用 §13.2 第 7 行的门控回填形态，带 `WHERE lot_id IS NULL`）。
- 约定复用（**不复用名字**）：只读 `inventory`、`--manifest` 必填、`high_risk` 写标志（§13.2 第 8 行）。
- 挂新父组，不改现有 `projection-migration`（其 `apply` 语义是「落一个 disabled checkpoint」，非破坏性；两条 `apply` 同名同参但语义相反，是本批次唯一的操作者风险面）。
- **实现期订正（第 2 条的判定式，四条；本条订正把「丢弃键非空即 fail」换成可用的三分类）**：
  1. **原文的判定式不可用，它会在健康库上全红。** 已发布 payload 的面**远宽于** D3 的目标形状：`PositionLot.to_dict()`（`domain/domain/ledger/lots.py` 的 `:259`-`:284`）只有 17 个公共键 + 4 个仅股票的键，而库里存量 `fields_json` 还带 `broker` / `account` / `symbol` / `option_type` / `side` / `contracts` / `expiration` / `strike` / `premium` / `quantity_unit` / `position_id` / `cash_secured_amount` / `strategy_snapshot` 等一大批。实现在**未改动的健康 store** 上按原文跑，`lost` 桶非空（`close_price` / `close_reason` / `close_type` / `closed_at` / `last_close_event_id`），即原文会把一个**没有做错任何事**的库判 fail。故实现取三分类，且**只有 `lost` 桶非空才判 fail**：
     - `carried`——目标形状在**别处**重新表达了同一事实，必须同时给出载体名（`CARRIED_DROPPED_KEYS`，逐条带载体：`broker`→`contract_key.broker`、`account`→`contract_key.account`、`symbol`→`contract_key.underlying_symbol`、`option_type`→`contract_key.option_type`、`side`→`position_side`、`contracts`→`contracts_opened` / `contracts_open` / `contracts_closed`、`opened_at`→`opened_at_ms`、`strike`→`contract_key.strike`、`expiration` / `expiration_ymd`→`contract_key.expiration_ymd`、`premium`→`premium_open`、`position_id`→`position_key`、`quantity_unit`→`asset_type + shares_*`、`last_close_event_id`→`close_event_ids / last_event_id`、`source_event_id`→`position_lots.source_event_id` 列）。载体名是**断言的一部分**：没有载体的 `carried` 与 `lost` 无区别，只是换个名字骗过判定；
     - `reconstructible`——丢掉的那个键，其值可由**同一条数据路径**上的其它事实重算，必须同时给出推导出处（`RECONSTRUCTIBLE_DROPPED_KEYS`）。闭仓档位一族全部来自 `publisher._close_fields`（`publisher.py` 的 `:819`-`:862`），它把 `close_type` / `close_reason` / `close_price` / `last_close_event_id` / `last_action_at` / `closed_at` / `auto_close_exp_src` / `auto_close_grace_days` **逐个**直接写在**闭仓 trade event** 上（`event.event_id` / `event.price` / `event.event_time_ms` / `payload["close_type"]` / `payload["close_reason"]`），故是重算而**不是**丢失；`cash_secured_amount` / `underlying_share_locked` / `event_source_type` / `event_source_name` / `strategy_snapshot` 同理；
     - `lost`——既无载体又不可重算。**这是唯一判 fail 的桶**，实测在真实 store 上为 `{}`；
  2. **`note` KV 有两个互相不认识的写侧格式，且本仓自己的读侧看不见其中一个。** `publisher._base_fields_for_lot`（`publisher.py` 的 `:726`-`:731`）写**空格分隔**：`source={source_name} event_id={lot.open_event_id} order_id={order_id} multiplier_source={multiplier_source}`；`merge_note`（`src/infrastructure/feishu_bitable.py` 的 `:524`-`:533`）用 `;` 连接。而本仓的 `parse_note_kv`（`:511`-`:521`）**只按 `,` / `;` 切**，所以对一条 publisher 写出的 note，它只看得见**一对**：`("source", "test event_id=… order_id= multiplier_source=")`——其余键对它**不存在**。这不是本批次的改动，是既存事实，但它直接决定 `verify` 的 note 侧判定必须自己分词。实现用 `_segment_pairs` + `_NOTE_KV_KEY` 按**空白**做 KV 分词，且只在「整段全是 KV」时才当 KV 处理，否则整段归 prose（避免把自由文本里的 `a=b` 误判成事实载体）；
  3. **note 侧判定收在 `NOTE_KV_DISPOSITIONS` 的双类上**：`structured`（`exp` / `strike` / `multiplier` / `option_type` / `side` / `status` / `premium_per_share`）要求同名结构化字段**非空**，否则报 `note_kv_only:<key>`；`external`（`source` / `event_id` / `order_id` / `multiplier_source` / `auto_close_at` / `auto_close_reason` / `close_reason` / `auto_close_grace_days` / `auto_close_exp_src`）是有出处的运行元数据，不判 fail。未登记的键一律报 `note_kv_unmapped:<key>`——**不静默放行**，这条对应 §13.5 R6 的「D3 必须先做 note→结构化回填」；
  4. **原文对 checkpoint 复用短路的刻画是错的（高估了它）。** 原文说它「只要指纹命中就直接合成 matched」。实际前置条件在 `projection_verify.py` 的 `:205`-`:215`：需要一个 checkpoint **文件**（`<base>/current/projection_verify.checkpoint.json`，`_load_checkpoint` 的 `:79`-`:80`），且其 `projection_contract_version`、`event_fingerprint` **与** `position_lots_fingerprint` **三者全中**；而该文件只在一次 `ok` 的**全量重放之后**才写（`next_checkpoint = _build_checkpoint(...) if report["ok"] else None`，`:280`）。所以它证明的是「**自某个已验证点以来未变**」，**不是**「存储 payload 等于一次新鲜重放」——它的比较是**存储态对存储态**。这个区别对判定仍然致命（cookie 一变它就会把该红报成 ok，见 §13.5 R4 实测订正②），但原话「完全不重放」会被读成「无条件命中」，据此写的负例（例如在干净库上塞一个 checkpoint 就期待短路）**不会触发**，实测确认不触发。实现因此**不依赖**任何自报布尔：`verify` 从不调用复用入口（结构性禁用，测试用 monkeypatch 把 `verify_position_projection` / `verify_position_lot_projection` 替换成 `raise` 来证明它从未被走到），`mode_used` 恒为 `"full_replay"`；
  5. **`projection_checkpoints` 这个表名有两义，勿混。** `position_projection_checkpoints`（SQLite 表）是 Phase 3A 由 `activate_position_projection_checkpoints`（`position_projection_migration.py` 的 `:951`）启用的**运行期 tail checkpoint**，它**不是**上面那个短路的前置条件，两者不可放在一起读。`_checkpoint_state` 只把它的行数当**可观测事实**上报（`runtime_tail_checkpoint_rows`），**不**据此推断短路可用性。
- **实现期订正（第 3 条的执行范围，三条；原计划让 `apply` 一次做完 D1–D4）**：
  1. **D1 / D2 重建不能由本批次执行**（原文把它排在 `apply` 内）。机制是 §13.5 R4 + R7：列合同是**精确集合相等**（`repository_projection_schema.py` 的 `:31`-`:34`，`missing` 与 `unclassified` 必须**同时**为空），**删列与加列是同一个失败模式**，一次落 D1 的重建会把窗口前**所有**旧形状库推成 `column_contract_open` → head `untrusted` → tail 发布 `RuntimeError`。承载 DDL 的那次发布属 §9.5 M6 第 3 步窗口（未授权）。故 `apply` 把 `switch_primary_key_to_lot_id_and_drop_record_id`（D2 第二步）与 `drop_expiration_column`（D1）写进步骤账本、`status: "deferred"`、`reason: "column_contract_precedes_rebuild"`——**登记而不执行**（先例是 §13.2 第 7 行那种「登记待窗口」形态）；
  2. **D3 对存量行的重写不能由本批次执行**（原文把它排在 `apply` 内）。阻断条件是 §12.4 D3 **自己的前置**：读侧尚未收敛（R6 的扁平键 + `note` fallback），改存量 payload 会让所有读扁平 `expiration` / `strike` 的点当场断裂。故 `rewrite_fields_json_to_lot_shape` 同样 `status: "deferred"`、`reason: "read_side_compatibility_window_open"`；
  3. 于是 `apply` **实际执行**的是两件、都是**非破坏、无窗口依赖**的（与切片 1 的加列同族）：D2 的**存量回填**（`UPDATE position_lots SET lot_id = record_id WHERE lot_id IS NULL`，即 §13.2 第 7 行的门控形态；`record_id` 是主键，故赋值不可能产生重复，`idx_position_lots_lot_id` 唯一索引也不会被触发）与 D4 的 `strip_position_id_from_fields_json`（把 `position_id` 从 `fields_json` 取走——注意它的**载体**已在 `CARRIED_DROPPED_KEYS` 里登记为 `position_key`，所以这不是丢事实）。守卫触发器不会因此 bump `lots_generation`：`AFTER UPDATE OF` 列表是 `record_id, account, fields_json, source_event_id, expiration, strike, multiplier`（`repository_projection_schema.py` 的 `:683` / `:703` / `:725`），D4 改 `fields_json` **在里面**、回填不在；实测 `projection_heads_advanced` 与 `required_follow_up` 都进了上报，故一次重发布是**必需**的后续动作。
- **实现期实测（真实 store，端到端）**：本地用**真实写路径**建成含期权 + 股票 lot 的旧形状 store（两个 `persist_manual_open_event` → `run_position_projection_forced_full` → `record_manual_assignment`，再降级为 `lot_id=NULL` + `position_id=LEGACY-{record_id}`）。`inventory` 只读且报出待办条数；`apply` 前的 `verify` 报红且**恰好两条**预期理由；`apply` 执行上条的两件事、D1/D2 重建/D3 三条 deferred；`apply` 后的 `verify` 转绿且 **`lost: {}`**；注入失败后回滚，库**逐字节相同**。

### 13.4 验证计划

| # | 对象 | 验证方式 |
|---|---|---|
| 1 | 切片 1 | 在**非空**的旧形状 fixture 上：断言列存在、**列合同 `missing` / `unclassified` 皆空**、head 不落 `untrusted`、唯一索引确实建成；连续开库两次证明幂等；**写入一条新行后断言其 `lot_id` 非空**；读路径输出与改前等价。**已执行（22/22 通过）**：fixture 由**改造前代码**（pristine `34ccf7fd` worktree）用 `publish_full_position_projection` 建成 `heads=trusted` 的两账户库（`schema_version=171`），再用本批次代码打开。逐条实测：加列后旧 8 列顺序不变；合同两侧皆空；`idx_position_lots_lot_id` 存在且 `unique=1`，旧 `idx_position_lots_expiration` 存活；存量行 `lot_id` 保持 NULL（回填不入侵开库路径）；重开 `schema_version` 173→173 且 `sqlite_master` 全量比对无差异；新行写入即 `lot_id` 非空、被改行的载体被补上、未变行仍走 `unchanged` 短路（`added=0 changed=1 removed=0 unchanged=2`，证明 diff 语义未被这次加列改写）；`list_position_lots` / `get_position_lots_by_ids` / `list_active_position_lots` 三处读路径双键齐发且相等、`get_position_lot_fields` 的 `fields` 形状逐字不变；最后 `publish_full_position_projection` 重新盖章后 stored cookie == live cookie 且各 head 回到 `trusted`。复现脚本：`slice1_build_legacy.py`（pristine worktree 内运行）+ `slice1_verify.py`（本批次 worktree 内运行） |
| 2 | 切片 2 | 断言**持久化形状未变**：§13.6 每条边界的列名 / payload 键名 / 种子键名逐字不变；combo 身份读回保持绿（`tests/test_trades_combo_reconciliation.py`）；decision 投影 schema 保持绿（`tests/test_ledger_current_decision_projection_schema.py`、`tests/test_ledger_current_decision_projection.py`）。~~逐批跑受影响文件~~ **实现期订正：无「逐批」——本切片是单次原子改名（§13.3 切片 2 订正 1），一次全量跑是唯一可行的验收**。**已执行**（101 文件 / 1740 个标识符 token）：① 结构闸门——HEAD↔工作区**逐 token 位置比对**，断言 `(HEAD名 → 现名)` 映射在改名域上**单射**，337 个 scope 全过；字符串漂移 18 处全在审核清单内（0 处未审核）；② 判据闸门——剩余 64 处逐桶归类，**0 处未归类**（五桶口径见 §13.6 订正）；③ 类 ⑧ 闸门——`getattr/hasattr/setattr/delattr(obj, "<name>")` 全仓仅 1 处（`combo_membership.py` 的 `:524`，既存双读，已审核）；④ 类 ① 远距离形态闸门——942 个被 `**` 展开的 dict 字面量键，0 个不是被调用方形参；⑤ `ruff check src domain tests scripts` → `All checks passed!`；⑥ 全量 `3 failed, 7286 passed, 2 skipped`，三项失败与基线**逐名相同**、`passed` 与基线持平（纯改名不新增用例），**零新增失败**；⑦ 两个仓库 tripwire（`tests/test_dependency_graph_generator.py`、`tests/test_position_projection_facade_inventory.py`）→ `5 passed`（本切片未增删仓内 import 边，故**无需**重生成 `docs/DEPENDENCY_GRAPH.md`——与切片 3 结论不同）。**未做**：commit / push / merge，发布、升级、部署，生产配置与生产写入 |
| 3 | 切片 3 | 端到端：构造含期权 + 股票 lot 的旧形状 store → `inventory` → `verify` → `apply` → `PRAGMA integrity_check` → 重开库 → 再 `verify` 报一致；**负例**：构造一行「结构化字段为空、事实只在 `note` KV」，断言 `verify` 判 fail；**失败路径**：中途注入失败 → 整体回滚、旧表原样。测试形态复用 `tests/test_option_positions_cli.py` 与 `tests/test_position_projection_migration.py`。**实现期订正（fixture 条款，已实测）**：原文要求 fixture 含 `assigned-stock-*` 与 `assigned-stock-sale-*` 两族——**该要求不成立**，这两族**不是 `position_lots` 行**：`assigned-stock-{event_id}` 由 wheel 事件构造器在 `domain/domain/wheel.py` 的 `:819` 产出，`assigned-stock-sale-{…}` 在 `src/application/positions/workflows.py` 的 `:130`-`:150` 产出，二者分别活在 `wheel_events` / `assigned_stock_events` 与 decision 读模型里；且 `"assigned_stock"` **不在** `SUPPORTED_EVENT_TYPES`（`domain/domain/ledger/events.py` 的 `:21`-`:26`：`OPEN_EVENT_TYPES = {"open"}`、`CLOSE_EVENT_TYPES = {"close","expire_close","assignment","exercise"}`、`TARGET_LOT_EVENT_TYPES = CLOSE_EVENT_TYPES \| {"adjust"}`、`TARGET_EVENT_TYPES = {"void","repair"}`、`READONLY_EVENT_TYPES = {"verification"}`），按字面构造会直接 `unsupported_event_type`。**股票 lot 的正确来源是股票 `open` 事件**：`publisher._stock_lot_fields` 的触发条件是 `lot_is_stock(lot)`（`domain/domain/ledger/lots.py` 的 `:291`-`:293`），事件用 `event_type="open"`、`contracts=shares`（**必须 > 0**，否则 `contracts_must_be_positive`）、`option_type=""`、`strike=0`、`expiration_ymd=""`、`asset_type="stock"`、`quantity_unit="share"`（先例：`tests/test_ledger_projection.py` 的 `:456`-`:507`）。**实测结果**：fixture 用真实写路径建成（两个 `persist_manual_open_event` → `run_position_projection_forced_full` → `record_manual_assignment` → `degrade_to_pre_migration_shape` 置 `lot_id=NULL` 并注入 `position_id=LEGACY-{record_id}`）；`apply` 前 `verify` 报 `{"field_mismatch": 4}`、`apply` 后 `{"matched": 4}`，全程 `lost: {}`；`PRAGMA integrity_check` = `ok`、lot 行数不变、零 NULL 载体；回滚路径实测库**逐字节相同**。新增 `tests/test_lot_identity_migration.py`（20 例，全绿），含 note KV **两种写侧格式**的参数化负例、monkeypatch 证明 `verify` 从不走复用入口、以及 `_position_lot_contract_scalars` 的一致性 pin |
| 4 | 每切片共同 | 先捕获本修订的 FAILED 列表做基线，再比对**名字**而非计数（本机环境类失败是既存基线，见 `docs/GUARDRAILS.md`）；改动任何 `domain/` 语义文件后必须刷新 projector fingerprint，否则大面积报 `ProjectorImplementationUnavailable`。**切片 1 实测**：全量 `3 failed, 7256 passed, 2 skipped`，FAILED 三项与 pristine `34ccf7fd` 基线**逐名相同**（`test_inbound_control.py::test_upgrade_worker_launcher_passes_env_file_pointer_to_systemd`、`test_service_credential_materializer.py::test_materializer_rejects_symlinked_encrypted_source`、同文件 `::test_materializer_cleanup_refuses_unexpected_entries`），即**零新增失败**；本切片不改 `domain/`，故不涉 projector fingerprint 刷新。**切片 3 实测**：全量 `3 failed, 7276 passed, 2 skipped`，FAILED **三项与前两切片基线逐名相同**（同上三名），`passed` 由 7256 → 7276 即本切片新增的 20 例，**零新增失败**；本切片同样不改 `domain/`。**注意切片 3 一度引入两处新红，均为仓库 tripwire 而非行为回归，已修**：① `tests/test_dependency_graph_generator.py`——新增模块改变了 import 边（`src.application.ledger → src.infrastructure` 10→11），须跑 `scripts/generate_dependency_graph.py` 重新生成 `docs/DEPENDENCY_GRAPH.md` 与 `docs/dependency_graph.mmd`（**生成，不得手改**）；② `tests/test_position_projection_facade_inventory.py::test_full_projection_calls_are_explicitly_classified`——新增的全量投影调用点必须显式登记为 `("src/application/ledger/lot_identity_migration.py", "verify_lot_identity_migration", "project_stored_trade_events_to_position_lots"): 1`，该测试对每个调用点做**精确 `Counter` 相等**判定，任何未登记的调用点都会红 |

### 13.5 风险与未决问题

**R1（硬边界，已裁定，机制已订正）— `strategy_group_identities` 的持久化名不可在本批次改名。**
`funding_put_record_id` / `participation_call_record_id` 同时是 NOT NULL SQL 列（`repository_core.py` 的 `:808` / `:811`）与持久化 payload 键（`repository_strategy_groups.py` 的 `:54` / `:57`），名称真源在 `domain/domain/combo_identity.py`（`:58` 读、`:257` 构造）。
**失败机制（本稿已订正一次）**：不是 `readback != identity` 那条比较——`raw_json` 由**同一个** `identity` dict 序列化（`repository_strategy_groups.py` 的 `:24` → `:26`），改名会**同时**改掉写出去与比回来的两边，因此该比较**不可能**捕捉改名（它是一条 JSON 自往返守卫，真正的比较在 `writer_trade_events.py` 的 `:295`-`:296`，不是 `:286`——`:286` 是 `_assert_combo_membership_exact` 的实参）。
真正的断点在**既有行**上：`existing_identity` 从旧 `raw_json` 读出后被 `validate_combo_identity` 按**新键名**校验（`writer_trade_events.py` 的 `:262`-`:274`），取不到值即抛 `strategy group identity conflict`；读侧更早失败于 `current_decision_combo.py` 的 `:57`-`:62`（抛 `combo identity is invalid`）与 `wheel_assignment_recovery.py` 的 `:85` / `:92` / `:106`。
故故障模式是「**旧 payload 读不出 → 该 group 的新写入失败**」，与结论（本批次不改）一致，但方向与初稿相反。这个订正有意义：R2 的「边界集合」正是要按**是否持久化**来判定，而不是按**是否有全等比较**来判定。

**R2（成功信号陷阱）— 切片 2 没有「改完」的终态。** 窗口之前写侧仍须写 `record_id`（主键、唯一索引、守卫触发器都还挂在它上面）。验收必须写成「剩余命中 = §13.6 集合」，并要求每批提交说明照抄该清单。**该清单现已逐条列举于 §13.6**（初稿只有散文，无法据以验收）。

**R3（规模）— 引用面大。** `record_id` src+domain 853 行 / 77 文件、tests 491 / 57；`stock_lot_id` 426 / 30 与 238 / 26（口径见 §13.2）。切片 2 必须按 owner 分批，否则回归面失控。

**R4（无版本号 + verify 语义）— `verify` 只能靠内容比对。** 无迁移框架、无 schema 版本号（`PRAGMA user_version` 0 命中）。**两处必须写死的语义**：① `verify` 的判定是「逐行内容相等」而非版本标记；② **禁用 checkpoint 复用短路**（§13.3 切片 3 第 2 条）。另注：`verify` 把 `source_state_mismatch` 也算进 reason，而该判定含 `sqlite_schema_cookie`（`position_projection_migration.py` 的 `:830`-`:836`）——切片 1 的 `ALTER TABLE` 会 bump 它，故窗口前必须先确认各 head 已 trusted、无 `source_state_mismatch`，否则健康库也会翻红、go/no-go 门不可用。

**R4 实测订正（切片 1 已量化，且比原文更尖锐）。** 在 pristine `34ccf7fd` 建成的 `heads=trusted`、`schema_version=171` 的库上打开本批次代码，实测结果是：`PRAGMA schema_version` 171 → **173**（加列 + 建唯一索引各 +1），**stored** `position_projection_source_state.sqlite_schema_cookie` 仍为 171，于是 `read_current_position_projection(account="lx")` 立刻返回 `status='data_unavailable'`、`reason='sqlite_schema_cookie_mismatch'`——**而 head 行本身没被动过**（表里仍是 `status='trusted'`）。消费该 cookie 的两处都会拒绝：`_unchanged_runtime_result_if_trusted`（`position_projection_runtime.py` 的 `:683`）不再短路，`_decode_trusted_checkpoint` 的 `position_projection_runtime.py` 的 `:1157`-`:1159` 直接抛 `source/checkpoint SQLite schema cookie mismatch`。**这是每库一次、可自愈的**：一次 `publish_full_position_projection` 之后 stored cookie == live cookie（实测 173 == 173）、各 head 重新可读 `trusted`（实测通过）。

由此得出两条对 M6 的可执行结论，原文没有：① **第 1 步落地后必须让一次完整重发布先跑完**，第 3 步窗口的 go/no-go 只能读这次重发布**之后**的状态；否则一道纯粹因为「加了列」而翻红的门会把 go/no-go 判成 no-go；② 这条自愈路径**恰好**是切片 3 禁用 checkpoint 复用短路的实证依据——cookie 一变，复用分支就是错的，而一个复用短路过的 `verify` 会把它报成 ok。注意自愈只覆盖 cookie：存量行的 `lot_id` 仍为 NULL，直到切片 3 的门控 `apply` 回填。

**R5（未授权边界）— 窗口执行仍未授权。** §9.5 M6 第 3 步需停全部账本写入方、SQLite `.backup` 落副本、`integrity_check`、`mv -n` 就位并保留源库（§12.6），属生产写入，**本批次不执行**。

**R6（已核实，D3 的前置阻断条件）— D3 会同时拿掉扁平键与列回填，`note` 是活跃 fallback。**
`PositionLot.to_dict()`（`domain/domain/ledger/lots.py` 的 `:259`-`:284`）的键集里**既没有 `note`，也没有扁平 `expiration` 与 `strike`**；而 `position_lot_row_to_record` 正是**用列回填这三个扁平键**（`sqlite_row_codec.py` 的 `:15`-`:22`），D1 又删列 → 回填来源一并消失。值仍在 `contract_key.expiration_ymd` / `contract_key.strike`，但**键形状**从扁平 ms 变成嵌套 ymd，所有读扁平键的点会断。
且 `note` 不是装饰性备注：`effective_expiration` / `effective_strike` / `effective_multiplier`（`domain/domain/ledger/position_fields.py` 的 `:253`-`:281`）在结构化字段缺失时**回退到 `note` 的 KV**（返回 `source="note.exp"` 等）。
故 §12.4 D1 表格「存量｜无」是**假设而非事实**，须由切片 3 的 `inventory` 载体分布回答；若存在仅由 `note` KV 承载的行，D3 必须**先**做 note→结构化回填、或保留这些 KV，**再**允许丢 `note`。另：`publisher._base_fields_for_lot` 从开仓事件的 `payload.fields` 快照播种，所以只改 `position_lots.fields_json` 而不处理播种，下一次重发布就会把被删字段**再种回来**——D3 必须写清播种的去留。

**R7（结构，需并入 §9.5 M6，但不执行）— M6 缺「窗口后收紧发布」一步。**
列合同是代码里硬编码的精确集合、又是发布路径的硬门。承载 D1–D4 的 DDL / 合同 / 守卫改动的那次发布一旦上生产：窗口前的旧形状库立刻 `column_contract_open` → `untrusted` → tail 发布 `RuntimeError`；窗口后若不发布收紧版，新形状库又永远回不到闭合。B 形态的立论是「代码已装」与「数据已改」可分离，但 M6 只有「普通发布(1,2) → 一个窗口(3) → status 复核(4)」，缺承载「两形状并存」的那次发布。**本项只主张把该步写进 M6 并写明窗口期服务运行在哪个形状上，不主张执行。**

**R6 实测订正（切片 3 已交付回答能力，但仍未回答生产）**：`inventory` 现已在**只读**路径上给出载体分布（`carriers`：每个标量的 `structured` / `note_kv` / `column_only` / `absent` 计数，外加 `note_kv_keys` 与 `column_present_rows`），故 R6 的问句**已可回答**，机制不再是空的。**但本批次只在一个本地合成 fixture（4 行）上跑过**，实测 `expiration` / `strike` 为 `structured: 2 / note_kv: 0 / absent: 2`（`absent` 的 2 行即两个股票 lot，它们本就无到期日与行权价，非缺口）、`multiplier` 为 `structured: 4`；`verify` 的 `lost` 桶为 `{}`。这个结果**不能**外推到生产：它是 4 行合成库，而 R6 的风险面恰恰是「真实存量里有没有仅由 `note` KV 承载 `exp` / `strike` / `multiplier` 的行」。对生产库跑一次 `inventory` 是**只读**的、可行且未执行；在该读数拿到之前，D3 的 note 前置阻断条件**未解除**。**负例已锁死**：实现期构造「结构化字段为空、事实只在 `note` KV」的行，`verify` 判 fail（`note_kv_only:<key>`），且 note 侧还有两道更宽的兜底——整段非 KV 的自由文本报 `note_prose_only_in_note`、未登记的 KV 键报 `note_kv_unmapped:<key>`，两者都归 `lost`、都判 fail；`note` 键本身只有在**整条 note 全是已登记 KV 且每个 `structured` 类键的结构化对位非空**时才归 `carried`（理由 `note.kv`），即「丢掉它不丢任何事实」——这正是 R6 要求的门，而不是绕过它。

**未决问题**：无。§12.7 的 5 项已由 §9.5 收口；R6 需窗口前由 `inventory` 回答，已登记为 D3 的前置阻断条件。

### 13.6 持久化边界集合（切片 2 的禁改清单）

切片 2 的目标语句是「旧名仅存活在持久化边界上」，而**边界**的判据是「**是否被持久化**」，不是「是否有全等比较」（R1 的订正正说明二者不等价）。下表是逐条清单；切片 2 每批提交说明须照抄，并声明本批未触碰。

| # | 边界（持久化名） | 位置与证据 |
|---|---|---|
| 1 | `position_lots.record_id` 列 / 主键 / 唯一索引 / 守卫触发器 | 建表 `repository_core.py` 的 `:517`-`:527`；触发器 `repository_projection_schema.py` 的 `:683` / `:703` / `:725` |
| 2 | `POSITION_LOTS_COLUMN_CLASSIFICATION` 的键集合 | `repository_common.py` 的 `:97`；被 `tests/test_position_projection_publication.py` 的 `:143` 硬编码 |
| 3 | `position_lots.fields_json` 内的 `record_id` 键 | 消费点 `views.py` 的 `:36`、`read_model.py` 的 `:174` |
| 4 | `strategy_group_identities.funding_put_record_id` / `participation_call_record_id` | 列 `repository_core.py` 的 `:808` / `:811`；payload 键 `repository_strategy_groups.py` 的 `:54` / `:57`；旧行校验 `writer_trade_events.py` 的 `:262`-`:274` |
| 5 | `combo_pair_inferences.put_record_id` / `call_record_id` | 列 `repository_core.py` 的 `:839` / `:841`；`immutable_fields` 跨「旧 raw_json ↔ 新构造」比较在 `repository_common.py` 的 `:406`-`:431`；构造点 `combo_reconciliation.py` 的 `:212`-`:213` |
| 6 | `wheel_event_payload_hash` 的 canonical 键 `stock_lot_id` → 持久化 `payload_hash` | canonical dict 字面键名 `domain/domain/wheel.py` 的 `:442` / `:455`；列 `repository_core.py` 的 `:76`-`:79`（64 位 hex CHECK）；读回比对 `repository_assigned_stock.py` 的 `:279` / `:284`。**改名即改已持久化哈希 → 窗口后首次 append 撞 `wheel event conflict`** |
| 7 | `wheel_events.stock_lot_id` 列 / 部分索引 / 形状探测 | 列 `repository_core.py` 的 `:64`；部分索引 `:96`-`:97`；v2 重建探测 `:129` 与重建字典键 `:146`-`:160` |
| 8 | `assigned_stock_events.event_json` 的 `stock_lot_id` 键 | 该表无此列，身份在 payload 内；写读 `repository_assigned_stock.py` 的 `:170`-`:196`；**同 `stock_event_id` 走整串全等比较**（`:192`-`:195`），任何键改名都会让重复入库抛 `assigned stock event conflict` |
| 9 | `trade_events.event_json` 的 `raw_payload["record_id"]` | 写点 `maintenance.py` 的 `:134`-`:139`（同处也写 `target_lot_id`，多数读点有回退，故属形状风险而非立即报错） |
| 10 | `bootstrap` 的**事件身份**种子键 `record_id` | `bootstrap.py` 的 `:89`-`:92`：`json.dumps({"record_id": …, "fields": …})` → sha1 → 拼进**持久化 `event_id`**。改名会让同一存量行重跑时派生出全新 `event_id` |
| 11 | `current_decision_projections` 的 payload 键与其内容哈希 | `repository_decision_schema.py` 的 payload / `payload_sha256` / `decision_state_fingerprint`；键名读点 `current_decision_assigned_stock.py` 的 `:278` / `:330` / `:365` / `:693` / `:767`、`current_decision_combo.py` 的 `:205`、`views.py` 的 `:159` |
| 12 | 索引 DDL 里的旧列名 | `repository_core.py` 的 `:533`-`:536`（`CREATE INDEX idx_position_lots_expiration ON position_lots(expiration, record_id)`）；D1 后若不换列清单，**新形状空库**开库会 `no such column` |
| 13 | `position_lots.fields_json` 内的存量 `position_id` | 即 D4 的对象；属同一次遍历，不属切片 2 |

**未决（不阻塞本批次）**：第 11 项与第 4/5 项的**重建配方**属窗口内动作；`wheel_events` 重建时 `payload_hash` 的处置（原值搬运 vs 重算回写）必须在 §12.4 / §9.5 M3 内决定——只要第 6 项的 canonical 键名变了，窗口之后的下一次写入就会撞 `wheel event conflict`。本项登记为**窗口前置决策**。

**实现期订正（本表的完备性，2026-09-19）**：本表枚举的是**持久化名（字符串形态）**这一类边界，它**不是**切片 2 剩余命中的全集。切片 2 实现完成后实测剩余命中 **64 处**，**没有一处**落在上表内——它们分属五个桶：`ARGPARSE_DEST` 20（`args.<dest>`，argparse 由 flag 拼写推出属性名，改属性名等于改已发布的 CLI flag）、`CARRIER_KEY` 20（`dict(k=)` / `.update(k=)` 物化的字符串键，含被 `**` 展开的 dict 字面量键）、`EXCLUDED` 11（`source_record_identity` / `record_id_non_null`，本就不是 lot 身份）、`TEST_LABEL` 11（`test_*` 函数名）、`BOUNDARY_COLUMN` 2（`scripts/benchmark_data_storage_projection.py` 的 `:1684`-`:1685`，直读 `position_lots.record_id` 列得到的局部变量）。

故：
1. **本表是「禁改清单」，不是「剩余命中清单」。** 两者的**方向相反**：本表列的是**必须保住**的旧名，五桶列的是**无法或不应改**的旧名。§13.3 切片 2 原写的成功信号「剩余命中**等于** §13.6 集合」把二者当成同一个集合，**不可满足**；
2. 正确判据是 **剩余命中 ⊆（本表 ∪ 界面名 ∪ 载体键 ∪ 测试标签 ∪ 非 lot 名）**，且**每桶逐条枚举、零条未归类**。验收工具须同时输出**桶计数**与**未归类条数**，未归类非零即 fail——只报总数的验收会掩盖一条真漏洞；
3. **五桶中只有第 2 桶（`CARRIER_KEY`）具备真风险**：它是本次唯一一处「标识符与字符串键同名、改名必须同步」的地方，也正是 §13.3 切片 2 订正 4 所说「被解释器逼出来的编辑」。其余四桶在结构上不可能承载持久化名。

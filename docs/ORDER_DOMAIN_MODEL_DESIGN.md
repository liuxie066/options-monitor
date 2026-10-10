# 期权 / 股票订单统一领域模型设计

> 当前需求真源见 [订单领域模型 PRD](ORDER_DOMAIN_MODEL_PRD.md)。成交输入、合约身份、事件与 lot 的共同 owner 已在源码实现；本文保留字段目标及必要历史迁移依据，部署状态以运行证据为准。
> §4–§7 是早期批准的字段与共用目标，不是全部已实现的声明。当前 `PositionLot` 仍内嵌 `contract_key`，与 §4.3 的扁平身份目标存在差异；当前序列化形状以 `domain/domain/ledger/lots.py` 为准。§9.4–§12 和 §13.6 为历史迁移记录，不能据此执行已退役写入口。

## 1. 当前实现入口

- `domain/domain/trade_execution.py`：`trade_execution.v1` 输入归一化、精确金额及执行身份；订单保留为可空引用和入口归组。
- `domain/domain/trade_contract_identity.py`、`domain/domain/ledger/identity.py`：资产/数量单位、方向和合约身份。
- `domain/domain/ledger/events.py`、`domain/domain/ledger/lots.py`、`domain/domain/ledger/projection.py`：共同事件与确定性持仓投影。
- `src/application/ledger/api.py`：非 ledger 模块使用的应用边界。新功能沿现有 owner 扩展，不重建平行模型。

旧事件兼容读保留；R2 普通开库只接受最终 `lot_id` 结构。当前迁移诊断见 §8/§13，历史设计中的文件行号及计数只代表当时检出。

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

普通期权成交缺少开平方向时，应用层可基于同账户、同合约的 OM 持仓分配：先平反向持仓，再开剩余数量。
跨零成交仍是一个 `ExecutionInput`，原始数量和经济事实不变；账本事件组含多个 close 和至多一个 open，
复用 `broker_deal_completion` 证明完整分配。整组预检、事务写入、费用分摊和恢复共用已有所有者，
不新增持久化订单、成交组表或审批状态。`open_close` 仅为处理结果动作，不是原始输入开平值。

### 4.2 Order（入口归组层，**非持久化实体**）

> 订单不作为账本实体持久化。它只以两种形态存在：
> 1. **可空引用**：`ExecutionInput.external_order_namespace` / `external_order_id`（见 4.1.3）；
> 2. **入口归组**：费用摊派时以四元组 `(broker, account, futu_account_id, order_id)` 为组（现状 `order_fee_sync.py` 的 `_identity` 已在做；`broker` 为券商、`account` 为 `lx`/`sy` 路由标签、`futu_account_id` 为券商物理账户、`order_id` 为券商订单号）。`external_order_namespace` 只是校验/输出元数据，**不进归组主键**。

订单级的委托意图与状态（`order_type`/`limit_price`/`ordered_quantity`/`status`/`filled_quantity`）如需暴露，只在适配器层组装，**不进账本**。券商订单状态、委托量 vs 成交量的对账属于 `src/application/trades/` 的入口职责，不形成新的持久化事实源。

### 4.3 PositionLot（持仓手，asset_type 判别）

> 早期设计基线：`domain/domain/ledger/lots.py`（期权）+ `assigned_stock_events`（股票）。当前 `PositionLot` 已区分资产类型；下段保留扁平身份目标，实际仍内嵌 `contract_key`。
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

## 8. 当前迁移与验证边界

代码语义收敛和列退役窗口已经完成。当前普通 repository 不双写旧表形状，也不在开库时执行破坏性重建。历史缺少 `asset_type` 的期权事件兼容读仍保留，不能与旧 SQLite 结构兼容混为一谈。

- `src/application/ledger/lot_identity_migration.py` 保留只读 `inventory`、`verify`；写入 apply 已退役。
- `src/application/ledger/sqlite_row_codec.py` 定义最终列集合；`docs/retired_column_sql_registry.json` 与相应质量测试约束旧列 SQL。
- 验证入口包括 `tests/test_trade_execution_input.py`、`tests/test_ledger_projection.py`、`tests/test_lot_identity_migration.py`、`tests/test_ledger_lot_identity_schema_guard.py`。

恢复迁移前的备份须使用匹配的历史受控流程，历史 R1 配方见 §9.4–§12；当前版本不提供旧窗口的破坏性写功能。

## 9. 已定决策（原待确认项）

### 9.1 股票 `cost_basis` 口径 → 存 lot 总额，派生每股

- **权威字段**：`cost_basis_total`（Decimal，含费用）= `assignment_notional + assignment_fees`，对应现状 `assigned_stock.py:1381` 的 `stock_cost_basis_total`。
- **派生字段（不存储）**：`cost_basis_per_share = cost_basis_total / shares_opened`，对应现状 `_lot_basis_per_share_with_fees`（`assigned_stock.py:844`）。
- **依据**：外部 Futu 给的是每股 `avg_cost`，适配层已在 `portfolio_context_builder.py:266` / `futu_portfolio_context.py:712` 转为 `known_cost_total = avg_cost × shares`；费用总额是精确事实，存总额能无损承载费用，每股由总额/股数精确派生，避免「每股先舍入再反推总额」丢费用精度。
- **边界**：每股 `avg_cost` 只出现在适配层入口，进账本即转总额。

### 9.2 `position_side` 移出身份

`ContractKey` 当前只承载合约身份，不含 `position_side`。方向由 `derive_position_side(position_effect, side)` 派生并由 event/lot 使用；旧事件兼容读集中在边界。`position_key` 是合约身份与方向的派生聚合键，既有持久化字符串及 hash 不因内部字段归位随意改变。

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
> 本节记录历史 R1 迁移窗口的形态与顺序决策；R2 已关闭写窗口，当前操作边界见 §8/§13。

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
  2. 股票 lot id 生成点：`domain/domain/assigned_stock.py:283` `_assigned_stock_lot_id` → `f"assigned-stock-{event_id}"`；同一形状另见 `domain/domain/wheel/intents.py:165`、`current_decision_assigned_stock.py:649`、`wheel_trade_companions.py:456`。
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

本节保留历史落地依据，顺序见 §9.5 M6；当前只读诊断与旧备份边界见 §8/§13。历史配方不是当前操作授权。

## 13. 当前只读诊断与历史边界

已删除完成的三切片任务计划、临时工作树状态和重复测试执行说明。以下保留当前诊断语义及源码注释引用所需的迁移依据。

### 13.2 诊断 owner 与命令归属

原复用清单第 8 项的约定继续有效：lot-identity 的 `inventory` / `verify` 与 projection-migration 的 checkpoint/tail 命令职责不同，不能因为名称相同混用。owner 为 `src/application/ledger/lot_identity_migration.py`，两者均不写入旧库。

### 13.3 验证语义

payload 丢键不自动等于丢事实。`verify` 将非空旧字段分类为已由目标形状承载、可从事件重建或实际丢失；只对丢失失败，事件可重建的判断须实际测量。只在 `note` 中存在的事实、无法映射的 KV 和自由文本不能被当作安全删除。

`verify` 使用新重放与逐行内容比对，不复用 checkpoint 的“自上次验证未变化”短路；后者不能证明一个原本错误的存储符合新投影。

### 13.4 隔离验证

`tests/test_lot_identity_migration.py` 覆盖载体分布、非空丢键分类、真实丢失及只读大小不变。合成 fixture 只证明对应行为，不证明真实旧备份的所有载体。

### 13.5 历史载体与恢复边界

原 R6 的载体问题由只读 inventory 报告结构化值、note KV、列独有值和缺失分布；恢复旧备份前应核对实际内容。历史 schema cookie 改变须通过完整重发布恢复可信投影，不能通过复用旧 checkpoint 掩盖。

### 13.6 历史持久化边界集合

下表记录迁移前必须保护的名与 hash 载体，仅用于追溯当时改名为何需要窗口；退役后的当前列集合及 hash 规则以源码和登记表为准。它不是当前仍有旧列或待执行迁移的声明。

历史切片 2 的目标语句是「旧名仅存活在持久化边界上」，而**边界**的判据是「**是否被持久化**」，不是「是否有全等比较」（R1 的订正正说明二者不等价）。下表是逐条清单；切片 2 每批提交说明须照抄，并声明本批未触碰。

| # | 边界（持久化名） | 位置与证据 |
|---|---|---|
| 1 | `position_lots.record_id` 列 / 主键 / 唯一索引 / 守卫触发器 | 建表 `repository_core.py` 的 `:517`-`:527`；触发器 `repository_projection_schema.py` 的 `:683` / `:703` / `:725` |
| 2 | `POSITION_LOTS_COLUMN_CLASSIFICATION` 的键集合 | `repository_common.py` 的 `:97`；被 `tests/test_position_projection_publication.py` 的 `:143` 硬编码 |
| 3 | `position_lots.fields_json` 内的 `record_id` 键 | 消费点 `views.py` 的 `:36`、`read_model.py` 的 `:174` |
| 4 | `strategy_group_identities.funding_put_record_id` / `participation_call_record_id` | 列 `repository_core.py` 的 `:808` / `:811`；payload 键 `repository_strategy_groups.py` 的 `:54` / `:57`；旧行校验 `writer_trade_events.py` 的 `:262`-`:274` |
| 5 | `combo_pair_inferences.put_record_id` / `call_record_id` | 列 `repository_core.py` 的 `:839` / `:841`；`immutable_fields` 跨「旧 raw_json ↔ 新构造」比较在 `repository_common.py` 的 `:406`-`:431`；构造点 `combo_reconciliation.py` 的 `:212`-`:213` |
| 6 | `wheel_event_payload_hash` 的 canonical 键 `stock_lot_id` → 持久化 `payload_hash` | canonical dict 字面键名 `domain/domain/wheel/events.py` 的 `:49` / `:62`；列 `repository_core.py` 的 `:76`-`:79`（64 位 hex CHECK）；读回比对 `repository_assigned_stock.py` 的 `:279` / `:284`。**改名即改已持久化哈希 → 窗口后首次 append 撞 `wheel event conflict`** |
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

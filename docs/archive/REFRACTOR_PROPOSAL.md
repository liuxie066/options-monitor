# Options Monitor 重构方案（渐进式，保持可运行）

> 目标：**简化系统，提高 IO 的准确性和稳定性**。
> 
> 原则：每一步完成后项目仍可正常运行（至少通过 smoke + no-send 的真实 pipeline）。
> 
> 源 repo（dev）：`/home/node/.openclaw/workspace/options-monitor`

---

## Context
项目核心流程清晰：获取数据 → 筛选候选 → 生成告警 → 飞书通知。
但两个主文件膨胀严重、职责混杂，导致代码难理解、难维护，且 IO 路径容易产生隐式耦合。

---

## 问题清单

### 问题 1：`run_pipeline.py` 是巨石文件（~1600 行）
- 现象：`process_symbol()` 混合了数据获取、汇率转换、现金计算、扫描调用、结果标签等。
- 影响：改任何环节都要读大量代码，新人难定位。
- 建议：拆分为 4 个模块（职责单一）：
  - `scripts/symbol_processor.py` — 每个 symbol 的 fetch + scan 编排
  - `scripts/summarize.py` — 汇总排名和标签逻辑
  - `scripts/context_loader.py` — portfolio/option context 加载与缓存
  - `run_pipeline.py` 瘦身到 ~300 行，只做顶层编排

### 问题 2：`send_if_needed_multi.py` 过大（~1500 行）
- 现象：一个文件包含 OpenD 健康检查、symlink 切换、现金查询、通知合并、发送决策。
- 影响：多账户问题调试成本高。
- 建议：提取 3 个独立模块：
  - `scripts/opend_health.py` — OpenD 健康检查和降级逻辑（~200 行）
  - `scripts/notification_merge.py` — 多账户通知合并和格式化（~150 行）
  - `scripts/cash_footer.py` — 现金快照查询（~110 行）
  - 主文件瘦身到 ~400 行

### 问题 3：symlink 切换输出目录（隐式状态）
- 现象：通过 symlink 把 `./output` 指向不同账户目录，下游脚本读写的都是该 symlink。
- 影响：IO 路径隐式、难追踪；存在潜在竞态；调试时产物可能串台。
- 建议：消除 symlink 相关逻辑（如 `atomic_symlink()`/`migrate_output_if_needed()`），改为显式参数传递。

### 问题 4：FX 汇率转换逻辑散落多处
- 影响：改汇率逻辑容易遗漏，风控口径不一致。
- 建议：在 `scripts/fx_rates.py` 新增 `CurrencyConverter`，所有模块统一调用。

### 问题 5：重复定义工具函数
- 现象：`safe_read_csv`/`read_json`/`write_json` 等在多个文件重复。
- 影响：修复不一致。
- 建议：新建 `scripts/io_utils.py` 集中 IO 工具。

### 问题 6：两套日志系统
- 现象：多个自定义 `log()` + `RunLogger` 并存。
- 影响：日志不统一，排查困难。
- 建议：新建 `scripts/logging_config.py`，用标准 `logging` 统一；保留 `RunLogger` 用于结构化审计。

### 问题 7：大量 `except Exception: pass` 静默吞错
- 影响：关键上下文缺失时仍继续运行 → 产生不可信产物。
- 建议：统一错误策略：
  - **必须传播（fail-fast）**：配置加载失败、required_data 缺失/不可读、report_dir 不可写、核心 FX 不可用
  - **记录并继续**：单个 symbol 失败、非关键 context 不可用（`logger.warning`）
  - **工具函数替代**：大量 float/parse try/except 统一走 `safe_float` 等

### 问题 8：无类型化数据模型
- 现象：CSV 列多且无 schema，缺列会静默 NaN。
- 影响：上游列名变化，下游可能不报错但结果错误。
- 建议：新建 `scripts/models.py` 定义核心结构（如 `SymbolSummary`/`CashPosition`/`AlertClassification`），至少 Python 内部传递时有 schema 约束。

---

## 实施顺序（每步完成后仍可运行）

> 注：为了避免“改到能跑但 IO 变味”，建议在正式 1~6 之前加一个 **阶段 0：锁行为**。

### 阶段 0（新增）：锁行为（回归命令 + IO 检查点）
- `--smoke --no-send`：scheduler-only
- `--force --no-send`：真实 pipeline（不发消息）
- IO 检查点以 **run_dir** 为准（不要再把 `output/reports` 作为黄金对比路径）。

### 1) 新建基础设施（低风险）
- `models.py`（类型化数据）
- `io_utils.py`（合并重复工具）
- `logging_config.py`（统一日志）

### 2) FX 集中化（低风险）
- `fx_rates.py` + 替换核心调用点

### 3) 拆分 `run_pipeline.py`（中风险）
- 提取：`symbol_processor.py` / `summarize.py` / `context_loader.py`

### 4) 消除 symlink（中风险，但对 IO 稳定性收益最大）
- 改为显式 IO 参数传递：
  - 建议用三分法：`--required-data-dir` / `--report-dir` / `--state-dir`
  - 不建议只用一个笼统 `--output-dir`（容易重新引入隐式约定）

> 说明：本阶段虽然“不动 scan_* 的业务逻辑”，但允许做 **IO 参数化的例外**：
> - `scan_sell_put.py` / `scan_sell_call.py` 支持稳定的 `--input-root`/`--output`

### 5) 错误处理治理（随各步推进，低风险）
- 消灭主链路上的静默吞错，至少保证 IO/配置/FX/required_data 的 fail-fast

### 6) 拆分 `send_if_needed_multi.py`（中风险）
- 提取：`opend_health.py` / `notification_merge.py` / `cash_footer.py`

---

## 验证方式（建议更新）
每步完成后：
1) `py_compile` 覆盖关键脚本
2) `send_if_needed_multi.py --no-send --smoke` 通过
3) `send_if_needed_multi.py --no-send --force` 能跑通（至少产出 run_dir 关键文件）
4) 对比产物以 `output_runs/<run_id>/...` 为准，不再以 `output/reports` 为黄金路径

---

## 关联文档
- 逐步执行清单：`REFRACTOR_PLAN.md`

# options-monitor 渐进式重构计划（6 阶段，逐步收口 IO）

目标：**简化系统，提升 IO 的准确性与稳定性**；每一步完成后项目仍可正常运行（可通过最小回归命令验证）。

> 适用 repo（源头）：`/home/node/.openclaw/workspace/options-monitor`（dev）
> 
> 发布到 prod：使用 `scripts/deploy_to_prod.py`（默认 dry-run，`--apply` 应用）。

---

## 阶段 0（新增）：锁行为（先保证“怎么跑”不漂）

### 为什么
没有最小回归锁定，后续拆分会陷入“改到能跑但 IO 早已变味”的状态。

### 交付（DoD）
- 固化两条回归命令（写入本文件，并能在 dev/prod 跑通）：

1) **Scheduler-only（最小）**
```bash
cd /home/node/.openclaw/workspace/options-monitor-prod
./.venv/bin/python scripts/send_if_needed_multi.py --config config.market_hk.json --market-config hk --accounts lx sy --no-send --smoke
```

2) **真实 pipeline（不发送）**
> 目标：至少跑一轮 pipeline，验证 required_data/report_dir/state 的 IO 路径。
```bash
cd /home/node/.openclaw/workspace/options-monitor-prod
./.venv/bin/python scripts/send_if_needed_multi.py --config config.market_hk.json --market-config hk --accounts lx sy --no-send --force
```

- 增加一个“只看 IO 的检查清单”（不要求内容正确，只要求路径正确且产物存在）：
  - `output_runs/<run_id>/required_data/{raw,parsed}/` 存在
  - `output_runs/<run_id>/state/tick_metrics.json` 存在
  - `output_runs/<run_id>/accounts/<acct>/...` 产物存在（哪怕 EMPTY+reason）

---

## 阶段 1：基础设施（新建）

### 目标
不改行为，仅集中类型、IO 工具、日志配置。

### 新增文件
- `scripts/models.py`：类型化数据（dataclass / TypedDict）
  - `RunContext` / `AccountContext` / `SchedulerDecision` / `PipelineResult`
- `scripts/io_utils.py`：IO 工具
  - `read_json/write_json/atomic_write/ensure_dir`
- `scripts/logging_config.py`：统一日志
  - `get_logger(name)`；统一格式与 level

### DoD
- 阶段 0 的两条命令都通过。
- 除新增文件外，改动仅限少量 import；无逻辑变更。

---

## 阶段 2：集中汇率（fx_rates.py）

### 目标
把散落的 FX 转换统一到一个入口，明确来源/时间戳/失败策略。

### 交付
- `scripts/fx_rates.py`
  - `CurrencyConverter`
  - 明确：
    - 支持的 currency（USD/HKD/CNY…）
    - 数据来源（例如 portfolio-management / OpenD / 固定）
    - 缺失/异常时的策略（fail-fast or best-effort）

### DoD
- 先替换 2~3 个最核心调用点（不要一次全替）。
- 阶段 0 回归命令通过。

---

## 阶段 3：拆分 run_pipeline.py（1600→~600→~300）

### 目标
把“上下文加载 / 单 symbol 处理 / 汇总渲染”拆开，降低耦合与漏改 callsite 的概率。

### 提取模块
- `scripts/context_loader.py`
  - portfolio_context / option_ctx / cash snapshot 等加载
- `scripts/symbol_processor.py`
  - `process_symbol(...)`：fetch→scan→summarize→render
- `scripts/summarize.py`
  - `build_symbols_summary(...)` / digest / changes / alerts 等纯汇总

### 约束
- `run_pipeline.py` CLI 与行为保持一致（只做“搬家/抽函数”）。

### DoD
- 阶段 0 两条命令通过。
- `./.venv/bin/python -m py_compile` 覆盖拆分后的脚本通过。

---

## 阶段 4：消除 symlink（核心 IO 稳定性：显式传路径）

### 目标
彻底取消“切换 ./output symlink 指向 output_accounts/<acct>”这种隐式状态，改为显式参数贯通，防止串台。

### 方案（建议的 IO contract）
对每个账户 pipeline，显式传：
- `--required-data-dir <run_dir>/required_data`
- `--report-dir <run_dir>/accounts/<acct>/reports`（或 `<run_dir>/accounts/<acct>` 统一约定）
- `--state-dir <run_dir>/accounts/<acct>/state`

并且：
- 禁止任何脚本在 prod tick 中默认读写 `./output/{raw,parsed,reports,state}`（除 legacy/manual 模式）。

### DoD
- 删除/停用 symlink 切换逻辑；并发/重入风险显著下降。
- 阶段 0 两条命令通过。

---

## 阶段 5：错误处理治理（消灭静默吞错）

### 目标
把 `except Exception: pass` 这种黑洞替换为统一策略：可预期失败要落 reason，不可预期错误要 fail-fast。

### 统一策略（建议）
- IO/数据缺失：写 `reason` + 标记 `meaningful=false`，但不要假成功。
- 外部依赖（OpenD/网络）：统一 `error_code` + rate limit；必要时告警。
- 编程错误：抛出，cron 失败（以便及时修复）。

### DoD
- 主链路不再静默吞错（至少覆盖 scheduler/pipeline/notify）。
- 错误能定位到：step + symbol + account。

---

## 阶段 6：拆分 send_if_needed_multi.py（1490→~400）

### 目标
把 tick 入口拆成可测试模块，减少“一文件承载所有状态”的复杂度。

### 拆分建议
- `healthcheck.py`：OpenD watchdog / 外部依赖
- `required_data_prefetch.py`：一次性取数
- `account_runner.py`：每账户 scheduler + pipeline
- `merge_notify.py`：合并通知 + 标注
- `cash_footer.py`：现金快照/尾注

### DoD
- cron 入口命令不变。
- 仍然只发一条 merged 通知。
- 阶段 0 两条命令通过。

---

## 明确：哪些“先不动”（保持稳定组件）
- `scripts/scan_sell_put.py`
- `scripts/scan_sell_call.py`
- `scripts/scan_scheduler.py`

> 但为了阶段 4（显式 IO），允许它们只做最小参数化（例如 `--input-root/--output`），不改业务逻辑。

---

## 发布纪律（dev → prod）
- dev 完成某阶段后：
  1) `py_compile`
  2) 阶段 0 回归命令通过
  3) 用 `scripts/deploy_to_prod.py` dry-run 预览
  4) `--apply` 发布到 prod
  5) prod 上再跑一次阶段 0 回归

---

## 附：最小编译检查
```bash
./.venv/bin/python -m py_compile \
  scripts/send_if_needed_multi.py \
  scripts/run_pipeline.py \
  scripts/scan_scheduler.py \
  scripts/scan_sell_put.py \
  scripts/scan_sell_call.py
```

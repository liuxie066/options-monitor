# Tests Guide

本文件是 options-monitor 的测试手册，只说明测试分层、运行方式、维护约定和新增测试规则。

## 测试分层

- 纯函数 / Engine 契约测试：验证筛选、排序、调度、通知交付等确定性逻辑。
- CLI / script 边界测试：验证命令参数、入口兼容和输出文件契约。
- Pipeline / multi-account 测试：验证多账户状态隔离、共享 required data、共享 context 切片复用和 per-account notification batching。
- Schema / guard 测试：验证 DTO、source snapshot、config gate、文档 wording 和部署参数约束。
- 少量 subprocess 测试：依赖 `.venv/bin/python`，本地缺 venv 时会失败。

## 运行方式

推荐先跑聚焦测试：

```bash
python3 -m pytest tests/test_domain_engine_batch4.py tests/test_domain_engine_batch5.py -q
python3 -m pytest tests/test_candidate_engine_contract.py tests/test_candidate_engine_parity.py -q
python3 -m pytest tests/test_pipeline_context_shared_context.py -q
```

运行仓库内置关键回归集：

```bash
./.venv/bin/python tests/run_tests.py
```

运行全部自动发现测试：

```bash
./.venv/bin/python tests/run_tests.py --all
```

基础检查：

```bash
PYTHONPYCACHEPREFIX=/tmp/om_pycache python3 -m py_compile <changed-files>
python3 scripts/guardrails_check.py --check-doc-wording --check-deploy-args
git diff --check
```

## 本地环境注意事项

- `tests/run_tests.py` 中部分用例会调用 `./.venv/bin/python`。
- 部分 `send_if_needed` 用例默认读取本地运行配置；没有本地配置时会在配置读取阶段失败。
- 不要把本地 secrets、真实 token、私钥或个人运行状态提交进测试 fixture。

## 新增测试规则

- 新业务规则优先加纯函数测试，再加 CLI 或 pipeline 集成测试。
- 修改筛选/排序策略时，必须覆盖 Put 和 Call，并说明排序字段是否影响 hard filter。
- 修改通知策略时，必须覆盖：有候选、无候选心跳、通知窗口关闭、quiet hours、缺 target。
- 修改多账户路径时，必须覆盖账户隔离和 shared context 复用，不允许悄悄回退成每账户重复拉取。
- 修改配置行为时，必须同步 guardrails 或 config validation 测试。
- 修改 symbol 归一化 / alias 行为时，必须覆盖跨入口 contract：至少验证 watchlist、持仓写入、trade normalize 或 OpenD 入口会收敛到同一个 canonical symbol。

## 命名约定

- `test_candidate_*`：候选策略与 Engine 契约。
- `test_domain_engine_*`：Engine 决策入口和 orchestration guard。
- `test_multi_tick_*`：多账户 tick、per-account notification batching、状态隔离。
- `test_pipeline_*`：pipeline 上下文、postprocess、阶段计划。
- `test_*_batchN.py`：按阶段演进保留的回归批次，新增断言应尽量放入最相关文件，不要无意义新建批次。

## 维护原则

- 测试应守住外部行为和稳定契约，不要过度绑定临时实现细节。
- 如果必须用源码字符串 guard，断言应针对关键边界，例如入口调用、禁用旧路径、必须传共享目录。
- 对环境依赖型失败，要在提交说明中明确区分“环境缺失”和“代码回归”。

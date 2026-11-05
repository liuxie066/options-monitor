# Bot 工具定义体量归因与精简

## Rebase 到 #1008 后的当前结果

PR #1010 已 rebase 到 `origin/main` 的 `6d2b673cee94d164141fd8f8ad3152466b9de31b`，包含 #1008。以下为当前证据；后面的原始记录保留初始基准。三个精简实现文件 `bot/tools.py`、`agent_tools/candidate.py`、`agent_tools/project.py` 与 rebase 前 `b50269e16` 逐字一致；冲突处理合并了 `.gitattributes` 两侧规则，并重新生成依赖图。

最新 main 新增的 `position_advice_read` 完整保留，因此当前目录为 30 个工具。以 `6d2b673ce` 未精简定义为基线，按下文相同 UTF-8 JSON 口径重测：**81,360 → 49,995 字节，减少 38.55%**。实际 Scene 仍为 10 个工具，三个模型字段 **17,750 → 14,439 字节，减少 18.65%**。全注册表的三个模型字段为 48,558 → 39,494。23 条保留示例全部通过 schema 校验。

| 字段 | 精简前字节 | 占比 | 精简后字节 | 占比 |
| --- | ---: | ---: | ---: | ---: |
| name | 902 | 1.11% | 902 | 1.80% |
| description | 25,794 | 31.70% | 17,076 | 34.16% |
| input_schema | 21,802 | 26.80% | 21,456 | 42.92% |
| default_input | 905 | 1.11% | 905 | 1.81% |
| examples | 2,946 | 3.62% | 1,694 | 3.39% |
| capabilities | 1,894 | 2.33% | 1,543 | 3.09% |
| output_contract | 27,057 | 33.26% | 6,359 | 12.72% |
| 结构开销 | 60 | 0.07% | 60 | 0.12% |

逐项对照最新 main 的 30 个工具及 rebase 前的 29 个工具，工具名、参数名、类型、枚举、必填、范围、嵌套结构和默认值均保留；未因精简改变调用语义。基线指纹仅更新为 #1008 后的目录，Scene 预算按上游既有说明保持 14,500 字节。任务独立环境刷新 #1008 更新的 position-advice 插件后，精确收集 1,476 个唯一 node ID，测试结果 **1476 passed（49.66s）**，没有跳过或删减用例。

```sh
env -u PYTHONPATH OM_PYTHON="$PWD/.venv/bin/python" .venv/bin/python -m pytest -q tests/test_bot*.py tests/bot_eval tests/test_*agent*.py tests/test_*tool*.py tests/test_option_candidates*.py tests/test_runtime_scoped_diagnostics.py tests/test_position_advice_read.py tests/test_covered_call_roll*.py tests/test_short_put_roll_cli.py tests/test_tick_position*.py
```

Ruff、依赖图（731 个生产模块、0 环）、文档/运行配置/敏感产物守卫、固定基准 `6d2b673ce` 的公开面检查通过。现有公开 CLI fixture 回放成功。此次使用 `--force-with-lease` 更新同一分支，不合并 PR；真实模型准确率仍存在下文列出的评测证据缺口。

## 原始口径与结果

基准 `2ff8514b3`，调用 `tool_descriptions(list(AGENT_TOOL_REGISTRY))`。统一用 `len(json.dumps(value, ensure_ascii=False).encode("utf-8"))`，保留默认分隔符。共 29 个 Bot 可用只读工具，78,764 → 48,174 字节，减少 30,590 字节（38.84%）。用户给出的 78,618 字节未在此版本复现。ASCII 转义口径为 80,090 → 49,320，紧凑分隔符口径为 75,449 → 45,958；不把更换序列化口径算作收益，也不把字节数当作实测模型 token 数。

字段归因包含字段名、冒号空格、值和逗号空格；对象与列表剩余开销单列。逐工具表中的字段值使用同一口径。

| 字段 | 精简前字节 | 占比 | 精简后字节 | 占比 | 字段减少 |
| --- | ---: | ---: | ---: | ---: | ---: |
| name | 870 | 1.10% | 870 | 1.81% | 0.00% |
| description | 24,581 | 31.21% | 16,043 | 33.30% | 34.73% |
| input_schema | 21,328 | 27.08% | 20,982 | 43.55% | 1.62% |
| default_input | 884 | 1.12% | 884 | 1.84% | 0.00% |
| examples | 2,861 | 3.63% | 1,609 | 3.34% | 43.76% |
| capabilities | 1,829 | 2.32% | 1,491 | 3.10% | 18.48% |
| output_contract | 26,353 | 33.46% | 6,237 | 12.95% | 76.33% |
| 结构开销 | 58 | 0.07% | 58 | 0.12% | 0.00% |
| 合计 | 78,764 | 100% | 48,174 | 100% | 38.84% |

## 归因与改动

最大来源为输出契约及其在描述中的重复展开。`src/application/bot/tools.py::_agent_description` 原先把 `fact_fields` 全量拼进描述；当前 `output_contract` 实际是契约元数据及字段路径集合，并非完整 JSON Schema。

- examples 从最多三条变为最多一条可见参数示例，去掉包装文案、隐藏的操作员路径，以及只演示 `config_key` 的简单例子。最终 22 个工具有示例，全部通过加入默认值后的输入 schema 校验；`option_candidates` 的嵌套查询示例保留。
- Bot 输出契约仅保留版本、主集合名及去重的顶层结果字段名；描述保留原用途、来源和主集合，去掉重复的完整结果路径。没有工具需要在这个调用前目录中保留完整输出契约；执行、结果投影和准入仍直接解析 `AgentTool` 的完整契约。
- input_schema 仅缩短 candidate 和 project 所属字段的描述；保留搜索字面量与大小写、目录/文件、分页/行号、账户、符号别名、已送达日期及时区条件。逐项比较证明所有参数名、类型、枚举、必填、范围、嵌套结构、其他校验关键词和默认值均未变。
- capabilities 仅去掉全部 Bot 工具共有的 `read_only` 标签，保留其他能力标识。原有纯读准入不变。所有工具原用途描述、卖出方向澄清规则、费用解释和统计时间边界均保留。

## 实际模型请求

`src/application/bot/host.py` 只取 `name`、`description`、`input_schema`，`src/application/bot/runtime.py::request_model` 再转换为原生 function schema。当前 `src/application/bot/om_chat.scene.json` 选择 10 个业务工具，Scene 还覆盖了 `option_positions_read`。因此此版本实际 Host 并非每次发送全部 78 KB。只压缩 examples/output_contract/capabilities 不会直接减少这里的模型请求。

| 测量面 | 前 | 后 | 减少 |
| --- | ---: | ---: | ---: |
| 全注册表完整目录 | 78,764 | 48,174 | 38.84% |
| 全注册表的三个模型字段 | 46,837 | 37,953 | 18.97% |
| 实际 Scene 的三个模型字段 | 16,969 | 13,658 | 19.51% |
| Scene 经真实 adapter 转换的 Chat Completions tools | 17,289 | 13,978 | 19.15% |
| Scene 经真实 adapter 转换的 Responses tools | 17,149 | 13,838 | 19.31% |

后两项调用真实 `request_model`，仅替换 HTTP 客户端并截取其 tools 参数；没有外部模型调用。Host 额外附加的固定工具、提示词和历史消息不在本次收益分母内；表格不是整个请求体或 token 费用降幅。

## 验证与评测边界

- 修改前聚焦基线：333 passed。最终 Bot、Tool Gateway、schema 与相邻消费者：896 passed（11.93s），包含 P1 runner 单测、`tests/bot_eval`、`tests/test_bot_tool_descriptions.py` 新增的 34 项测试。新增测试固定了基线全目录和实际 Scene 的输入约束指纹，只排除 schema description 注解，且保留名为 description 的真实属性及默认值/枚举内容。
- Ruff、依赖图（692 个生产模块、0 环）、公开面守卫、文档/敏感产物/运行配置守卫、补丁空白检查通过。依赖图因新增测试更新。
- 现有 `opening_candidate_snapshot_diagnostics_model_ready` fixture 通过公开 CLI 回放：调用 `runtime_status` 后 answered。以冻结的基线目录与精简后目录分别回放，均成功，工具选择、输入、结果事实和最终文本一致，运行标识不同。回放使用脚本指定调用，不能据此推断真实模型选工具准确率。
- 实际尝试不指定脚本回复的 `bot eval`，退出 2，得到 `MODEL_CONFIG_ERROR / bot_config_not_found`。任务 worktree 缺少可用 Bot 模型配置，未获得真实模型前后准确率。
- 现有 `scripts/bot_p1_eval.py::_scene_provenance` 硬编码 Scene v5，而当前为 v7；收入用例还期望当前 Scene 未暴露的 `option_performance_report`。其单测模拟旧版本，不能用单测通过证明当前 live P1 可运行。`tests/bot_eval/test_answer_quality.py` 也明确只是脚本回复的链路检查。
- `eval_fixtures.py` 的观察注入只在 Host 的 debug/scripted 模式启用；启用真实模型后会进入实际工具执行，现有入口不提供“真实模型 + 固定工具观察”的隔离评测模式。本次未扩展或修复评测系统；保留真实模型准确率这一证据缺口，不声称已证明准确率无下降。
- 独立只读代码审查未发现具体高置信度回归；审查者未单独重跑测试。

字节测量可在基准和目标 checkout 分别复现（下述脚本只打印总量和逐工具字段值；字段计费含末尾逗号空格，完整列表扣除后的结构开销为 58 字节）：

```python
import json
from src.application.agent_tool_registry import AGENT_TOOL_REGISTRY
from src.application.bot.tools import tool_descriptions

size = lambda value: len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
items = tool_descriptions(list(AGENT_TOOL_REGISTRY))
print("total", size(items))
for item in items:
    print(item["name"], size(item), {
        key: size(key) + 2 + size(value) + 2 for key, value in item.items()
    })
```

复现主要命令（在目标 checkout 中，OM_PYTHON 指向已验证的项目解释器）：

```sh
env -u PYTHONPATH "$OM_PYTHON" -m pytest -q tests/test_bot*.py tests/bot_eval tests/test_tool_input_schema.py tests/test_agent_tool_contracts.py tests/test_agent_plugin_contract.py tests/test_agent_runtime_root_alignment.py tests/test_runtime_scoped_diagnostics.py tests/test_portfolio_agent_tool.py tests/test_option_performance_agent_tool.py tests/test_wheel_agent_tools.py tests/test_daily_decision_brief_agent_tool.py tests/test_notification_perception_read_tool.py
env -u PYTHONPATH "$OM_PYTHON" -m src.interfaces.cli.main bot eval --fixture opening_candidate_snapshot_diagnostics_model_ready --text '检查 runtime 状态' --include-events --model-turn-json '[{"tool_calls":[{"name":"runtime_status","arguments":{}}]},{"text":"结论：eval-only runtime 状态正常。"}]'
```

本报告为维护者记录，按任务要求显式纳入分支（目录默认被忽略）。`.gitattributes` 精确排除此文件的 `git archive` 导出；发布源码包由 `.github/workflows/_release-reusable.yml` 的 `git archive` 生成。

## 逐工具、逐字段字节（前→后）

| 工具 | 总字节 | name | description | input_schema | default_input | examples | capabilities | output_contract |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| option_performance_report | 6126→3495 | 37→37 | 1796→1256 | 1726→1726 | 75→75 | 124→72 | 90→77 | 2278→252 |
| option_positions_read | 5384→3941 | 33→33 | 1946→1582 | 1972→1972 | 37→37 | 283→111 | 73→60 | 1040→146 |
| runtime_status | 5272→1775 | 26→26 | 1898→451 | 964→964 | 21→21 | 79→79 | 41→28 | 2243→206 |
| candidate_filter_explain | 4757→2970 | 36→36 | 1547→928 | 1629→1549 | 21→21 | 268→92 | 79→66 | 1177→278 |
| portfolio_query | 3886→3388 | 27→27 | 1630→1630 | 1523→1523 | 37→37 | 177→45 | 71→58 | 421→68 |
| option_candidates | 3673→3434 | 29→29 | 2480→2480 | 572→572 | 21→21 | 101→101 | 57→44 | 413→187 |
| query_cash_headroom | 3656→2135 | 31→31 | 1307→888 | 574→574 | 21→21 | 114→64 | 45→32 | 1564→525 |
| project_files | 3426→2125 | 25→25 | 618→260 | 1478→1225 | 60→60 | 74→45 | 34→34 | 1137→476 |
| daily_decision_brief_read | 2924→2168 | 37→37 | 898→724 | 960→960 | 51→51 | 88→43 | 87→74 | 803→279 |
| healthcheck | 2901→1087 | 23→23 | 1125→366 | 470→470 | 21→21 | 47→16 | 46→33 | 1169→158 |
| candidate_rank_explain | 2813→1836 | 34→34 | 805→530 | 967→954 | 47→47 | 147→71 | 50→37 | 763→163 |
| notification_perception_read | 2790→1507 | 40→40 | 1029→636 | 496→496 | 32→32 | 90→40 | 93→80 | 1010→183 |
| scheduler_status | 2668→1044 | 28→28 | 773→209 | 554→554 | 21→21 | 64→64 | 51→38 | 1177→130 |
| portfolio_assignment_scenario | 2650→1861 | 41→41 | 966→765 | 560→560 | 21→21 | 53→53 | 101→88 | 908→333 |
| close_advice_read | 2554→1159 | 29→29 | 735→271 | 531→531 | 21→21 | 97→97 | 73→60 | 1068→150 |
| scheduled_tasks_read | 2298→806 | 32→32 | 702→310 | 171→171 | 21→21 | 47→16 | 65→52 | 1260→204 |
| operation_timeline | 2294→1291 | 30→30 | 571→270 | 692→692 | 21→21 | 141→40 | 72→59 | 767→179 |
| receipt_read | 1939→1448 | 24→24 | 411→321 | 815→815 | 32→32 | 16→16 | 64→51 | 577→189 |
| project_context | 1883→864 | 27→27 | 501→143 | 139→139 | 21→21 | 18→16 | 40→40 | 1137→478 |
| symbol_config_read | 1806→1212 | 30→30 | 274→136 | 626→626 | 21→21 | 184→115 | 68→55 | 603→229 |
| portfolio_pnl_bridge | 1724→1070 | 32→32 | 479→298 | 369→369 | 21→21 | 96→96 | 95→82 | 632→172 |
| runtime_runs | 1665→1177 | 24→24 | 298→261 | 625→625 | 52→52 | 90→40 | 60→47 | 516→128 |
| decision_history_read | 1658→1227 | 33→33 | 247→192 | 768→768 | 21→21 | 16→16 | 51→38 | 522→159 |
| runtime_logs | 1610→1017 | 24→24 | 313→269 | 432→432 | 39→39 | 138→64 | 60→47 | 604→142 |
| symbol_resolve | 1488→885 | 26→26 | 269→154 | 378→378 | 21→21 | 119→53 | 68→55 | 607→198 |
| trade_attribution_read | 1484→1025 | 34→34 | 209→184 | 622→622 | 21→21 | 16→16 | 31→31 | 551→117 |
| version_check | 1208→720 | 25→25 | 280→152 | 202→202 | 44→44 | 52→52 | 48→35 | 557→210 |
| quality_status | 1177→805 | 26→26 | 209→209 | 337→337 | 21→21 | 75→60 | 66→53 | 443→99 |
| config_validate | 992→644 | 27→27 | 265→168 | 176→176 | 21→21 | 47→16 | 50→37 | 406→199 |

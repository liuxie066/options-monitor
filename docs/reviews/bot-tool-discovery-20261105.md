# Bot tool discovery 可行性与收益测量

2026-11-05；源码基准 `92ddd9e164286ae4fea584ef04d51ef23b88cb23`（PR #1010）。本记录与附件仅供内部复核，不进入运行包。

## 裁决

**本轮不实现 tool discovery，保留现有 `full` 下发行为。** 不增加元工具、配置开关、动态工具权限状态或运行时入口。按任务第 1 步的停止条件交付分析。

理由不是“检索一定更贵”：去掉重复 schema 后，一次搜索并自动激活的方案在代表性夹具工作负载中可减少 **5.02%–8.05%** 输入 token 代理计数；知道精确工具名且仅回短确认的乐观下界可减少 **8.48%–11.44%**。但都增加一次模型往返，业务调用可并行时收益趋近于零或转负。实际调用分布、供应商计费/缓存和真实工具选择准确率均无数据，当前不足以证明在 OM 的实际使用中能稳定省钱。不能用首轮载荷减少比例代替会话收益，更不能声称所有检索设计都不划算。

## 1. 事实、调用分布与测量边界

### 实际下发规模

[`agent_tool_registry.py`](../../src/application/agent_tool_registry.py) 有 **46** 项；`tool_descriptions(list(AGENT_TOOL_REGISTRY))` 过滤后是 **30** 个 Bot 只读定义，默认 JSON 编码 **49,995 字节**。任务背景的 49,223 字节是之前的口径；#1010 rebase 后包含新增工具，见[已有体量分析](bot-tool-definition-size-20261105.md)。

但 [`om_chat.scene.json`](../../src/application/bot/om_chat.scene.json) 只选择 **10** 个业务工具，且 [`scene.py`](../../src/application/bot/scene.py) 进一步收窄 `option_positions_read` 为 events。[`host.py`](../../src/application/bot/host.py) 实际只传 `name/description/input_schema`，合计 **14,439 字节**，用 `cl100k_base` / `o200k_base` 对紧凑 JSON 编码分别计 **2,952 / 2,948 token**。49 KB 不是每轮 Bot 业务工具载荷。

渠道会话可另附加 `bot_memory`，有控制能力时还附加 control preview 工具；共享提示、记忆和对话历史也增加开销。本测量以无历史、无 control preview 的基础 Scene 为主，另列 memory schema 敏感性分析，不将它冒充完整线上请求。

### 实际调用分布：无数据

仅本地只读检查：配置指向的默认 `output_shared/state/inbound_control.sqlite3` 可读，但没有当前 `bot_runs` 或旧 `copilot_runs`；其他可定位的非账户审计 SQLite 也无这两类表。旧控制审计、Agent 会话、smoke/pytest 库无法证明真实 Bot 使用，未计入分布。**调用次数、每会话工具数、模型轮数、token/cache 分布全部为“无数据”，不是 0。** 覆盖仅限配置可定位的本地位置，不能推出所有位置均无历史。

生产审计读取被自动审批拒绝，要求另行授权生产目标；本轮未访问生产机，采用任务允许的代用分析。

后续有真实数据时，应只读展开 `bot_runs.events_json`：发起次数数 `tool_call`，产出结果数 `tool_result`，模型轮次分别数 started/completed/failed；逐个累加 `model_turn_completed.payload.usage`。`host_store.py` 的 `metrics_json.usage` 当前可能只是最后一轮，不能当会话总量，也不能与事件 usage 再次累加。缺失 cache 字段须标“未提供”，不要填 0。

### 代用工作负载

- `RECOMMENDED_FLOW = healthcheck → scan_opportunities → close_advice_read` 三项均不在当前 Scene；其中 `scan_opportunities` 不是 pure read。不能用它们虚构 Bot 使用频率或扩大发现权限。
- [`eval_fixtures.py`](../../src/application/bot/eval_fixtures.py) 的 opening fixture 提供 `runtime_status → candidate_filter_explain` 两份观察，用作两次串行业务调用的代表工作负载。
- close-advice fixture 的 `close_advice_read` 不在当前 Scene；exposure fixture 只有一份 `option_positions_read` 观察。四份 `tests/fixtures/bot/*model_turns.json` 都只给最终文本；其中旧 june 文件也没有当前同名 fixture loader。它们不构成模型工具选择评测。
- 对 10 个工具两两组合（含同一工具重复调用）另做 **55 对无权重敏感性分析**；这不是实际概率分布，不计算“平均会话收益”。

## 2. 成本口径与往返

使用独立安装的 `tiktoken==0.12.0`，同时给出两种 tokenizer 的代理值；**不是 DeepSeek 等实际供应商的计费 token**。计数对象是与 `runtime.request_model` 一致的 Chat Completions messages/tools JSON 子体；不含固定 model/temperature 等参数、供应商内部工具模板、网络开销、重试、推理文本或计费规则。Responses 的包装未模拟。

每轮重新计累计 messages 和当轮 tools，包括系统提示、索引、调用参数、工具结果、重复 schema；业务参数用 `{}` 占位且不执行/验证，只比较预知工具集下的输入结构。对代表工作负载使用真实仓库 fixture 内容；其余组合用相同的短合成观察。共同最终回答假定相同而略去，额外调用输出单独计数。检索精确命中预知目标，无误召回、无漏召回；这是有利于检索的假设，不能据此声称工具选择成功。

基础共享 messages 约 **2,810 / 2,526 token**，已包含每轮 Host 追加的 option-candidate 提示；索引 **315 / 256**，两个示例元工具定义 **100 / 102**。一次请求变小并不代表整个会话同比变小。

| 模式 | 本测量的发现路径 | 两次串行业务调用后的总模型请求数 |
| --- | --- | ---: |
| A `full` | 初始全部 10 个业务 schema | 3 |
| B1 `direct` | 已知精确名称；一轮并批 get_tool_schema，返回完整定义并加载 native schema | 4 |
| B2 `search` | 一轮 find_tools 返回目的/参数摘要，再一轮并批 get_tool_schema | 5 |
| B3 `search_activate` | 一轮 find_tools 返回摘要，Host 在下一请求直接激活命中 schema；不重复返回完整定义 | 4 |
| B4 `direct_activate` | 已知精确名称；get_tool_schema 仅回激活短确认，完整定义只放后续 native schema | 4 |

B1/B4 遇到两个不同工具时实际有两次 schema 元工具调用，只是并批在一轮；同一工具调用两次则只取一次定义。B3 才是一般的“**1 次搜索 + 2 次真实调用**”。B4 是更宽松的成本下界，短确认不满足字面上的“get_tool_schema 返回完整定义”，不把它当已实现方案。B3/B4 均需要修改目前固定 tools/names 的 runtime；本轮只做反事实测算。元工具名称不与 registry 重名。

### 代表工作负载：runtime_status → candidate_filter_explain

| 模式 | cl100k 输入总计 | 对 A 的变化 | o200k 输入总计 | 对 A 的变化 |
| --- | ---: | ---: | ---: | ---: |
| A full | 17,951 | — | 17,082 | — |
| B1 direct | 18,747 | +4.43% | 17,438 | +2.08% |
| B2 search | 23,403 | +30.37% | 21,704 | +27.06% |
| B3 search_activate | 17,049 | −5.02% | 15,707 | −8.05% |
| B4 direct_activate，下界 | 16,428 | −8.48% | 15,128 | −11.44% |

cl100k 的逐轮输入：A=`5819+5935+6197`；B3=`3322+4411+4527+4789`。A/B3 的调用输出 JSON 代理分别为 **64/105**，即另多 **41**；o200k 为 **66/108**。没有把增加的模型调用视作免费。

B1/B2 的完整 schema 同时在结果历史和 native tools 出现；这是这两种协议的成本，不是检索机制必然的成本，因此另测 B3/B4，避免用可去掉的重复否定整个方向。

### 敏感性

- 若两次业务调用可并批，cl100k 的 A 为 **11,744**、B3 为 **12,250（+4.31%）**；o200k 为 **11,163 → 11,241（+0.70%）**。模型请求从 2 次增至 3 次。
- 同一个 `project_context` 连调两次，cl100k 的 A 为 **17,628**、B3 为 **14,002（−20.57%）**；同一个 `option_candidates` 连调两次为 **17,628 → 16,255（−7.79%）**。确实存在更适合按需发现的工作负载，但无数据表明它们占多少。
- 55 对无权重组合中，B3 的 cl100k 输入节省范围为 **−2.93%～20.57%**，53/55 对减少；o200k 为 **0.20%～24.67%**，55/55 对减少。B1/B2 更差，完整结果见 JSON 附件。不得把均匀组合当成真实调用分布。
- memory schema 也要在额外发现轮重发；真实记忆正文、control 工具、历史、错误恢复和关键词检索误召回均未得到真实测量。一次搜索要命中全部后续工具的假设偏乐观。

**实际费用无数据。** 供应商费用应按 `未缓存输入×单价 + 缓存输入×单价 + 输出×单价` 及真实 usage 计算。这里没有实际 provider/model/cache 命中数据，不能套某家单价或保证 full/index 哪边缓存更多。[DeepSeek 缓存说明](https://api-docs.deepseek.com/guides/kv_cache) 也不保证命中；改变工具定义后如何计入前缀须实测。额外一轮模型的延迟至少增加该轮请求和元工具处理时间，本文没有编造秒数。

因此，当前 **10 个工具** 的实际入口不满足“已有稳定、明显净收益”的实现前提。B3 代表性节省仅约 900–1,375 输入代理 token，代价是请求数增加 33%；B2 则明显增加输入。保留 full，待真实使用表明串行少工具会话占主导、且真实模型/缓存计费能保住收益时再评估。

## 3. 验证与交付边界

本轮没有 index 实现，故不声称通过 index/full 真实模型 A/B、关键词命中率、两次未命中恢复、未下发工具错误、元工具只读审计等验收。它们是第 2 步启动后的必需测试，本轮按第 1 步停止条件不进入实现。

| 验证 | 结果与能证明的范围 |
| --- | --- |
| `tests/bot_eval` + tool-descriptions + Python runtime | **90 passed**；其中 `tests/bot_eval` **5/5 passed**，覆盖 source observation/最终正文传递等既有行为；不证明真实模型选择准确率 |
| 现有 Bot/tool 定向集合，未改源码基线与最终复跑 | 两次均为 **593 passed, 28 failed**，失败 node ID 集合完全相同；没有新增或消失的失败。包括缺 market-opend、feishu/wechat 渠道插件等既有问题，未笼统归为同一种环境原因 |
| 扩展工具集合 | 默认收集阶段缺 `om_market_opend` / `om_position_advice_plugin` 导致 **2 errors**；用 `--continue-on-collection-errors` 保留错误后为 **749 passed, 140 failed, 26 errors**，未改源码与依赖下实测；不声称扩展集合全绿，失败明细留本地日志 |
| 反事实 token 回放 | A/B1/B2/B3/B4 均只计数，不调用网络模型或业务工具；精确结果见附件 |
| 真实模型选择准确率、实际 usage、缓存、延迟 | **无数据**；脚本或预注入 fixture 的通过率不能冒充模型准确率 |

生产源码未修改，测试解释器和依赖未修改；没有安装插件或跳过失败。定向集合基线与最终复跑分别用时 9.34s / 9.53s，28 个失败 node ID 逐项相同。回答管线、schema 保真和 Python runtime 的 90 条聚焦测试全部通过（包含 `tests/bot_eval` 的 5 条）。文档/运行配置/敏感产物守卫、Ruff 与 `git diff --check` 通过；实际调用 `build_runtime_bundle.copy_source` 验证三个内部文件均被排除。

复现材料：[`bot-tool-discovery-20261105.py`](bot-tool-discovery-20261105.py)、[`bot-tool-discovery-20261105.json`](bot-tool-discovery-20261105.json)。脚本只读固定源码与仓库 fixture，直接输出测量 JSON；tokenizer 安装到独立目录，不加入 OM 依赖。

```sh
python -m pip install --target /tmp/om-discovery-tokenizer tiktoken==0.12.0
python -I docs/reviews/bot-tool-discovery-20261105.py \
  --root "$PWD" --tokenizer-dir /tmp/om-discovery-tokenizer \
  > /tmp/om-discovery-cost.json
```

上述 tokenizer 的首次加载可能下载公开编码词表；未发送业务数据或调用模型 API。对照脚本要求工作树生产源码与基准一致。

定向测试命令：

```sh
env -u PYTHONPATH PYTEST_ADDOPTS='' "$PYTHON" -m pytest \
  tests/test_bot*.py tests/test_agent_tool*.py tests/test_tool*.py -q --tb=short
```

内部文件在 `.gitignore` 中仅作本次精确路径例外以便随分支交付，并由 `.gitattributes` 的 `export-ignore` 及既有 `build_runtime_bundle.copy_source` 的 `docs/reviews/` 排除规则保持在运行包之外。未修改默认值；最终仍为现有 full 行为，没有新增 `OM_BOT_TOOL_DISCOVERY` 环境变量。

# Getting Started

这份文档只服务一种场景：

> 你要把 `options-monitor` 当作本地 Agent 工具来接入和调用。

如果你只是普通使用者，请先看根目录 [README.md](../README.md)。

---

## 1. 安装

```bash
git clone <repo-url> options-monitor
cd options-monitor
bash scripts/install_agent_plugin.sh
```

如果本地还没有 Python 环境：

```bash
python3 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt
```

`requirements.txt` 已包含 `futu-api`，用于补齐本地 `futu` Python SDK。

---

## 2. 查看 Agent 工具清单

```bash
./om-agent spec
```

这会输出工具 manifest（JSON）。

---

## 3. 初始化运行配置

推荐先启动 WebUI：

```bash
./run_webui.sh
```

默认地址：

```text
http://127.0.0.1:8000
```

首次初始化通常会生成：

- `config.us.json` 或 `config.hk.json`
- `secrets/portfolio.sqlite.json`

默认最小配置下：
- `option_positions` 只需要本地 SQLite
- Feishu 只在你启用 holdings / external_holdings，或希望给 `option_positions` 增加 bootstrap / 镜像时才需要配置

如果你不用 WebUI，也可以手工复制模板，详见根目录 [README.md](../README.md)。

---

## 4. 跑一个最基本的检查

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

如果你配置了 Feishu `option_positions` bootstrap，这个 healthcheck 会额外给出 `option_positions_bootstrap` 状态，区分：
- 未配置 bootstrap
- 已配置且成功
- 已配置但首次读取失败（warn / degraded）

如果要显式指定配置路径：

```bash
./om-agent run --tool healthcheck --input-json '{"config_path":"config.us.json"}'
```

---

## 5. 跑一个只读工具

```bash
./om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"list"}'
```

---

## 6. 常见环境变量

- `OM_OUTPUT_DIR`：覆盖 agent 输出目录
- `OM_AGENT_ENABLE_WRITE_TOOLS=true`：允许部分写操作工具

---

## 7. 下一步看哪里

- Agent JSON 合同：[`AGENT_INTEGRATION.md`](AGENT_INTEGRATION.md)
- 工具说明：[`TOOL_REFERENCE.md`](TOOL_REFERENCE.md)
- 配置字段说明：[`../CONFIGURATION_GUIDE.md`](../CONFIGURATION_GUIDE.md)

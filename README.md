# options-monitor

期权监控与提醒工具，面向 **Sell Put / Covered Call** 工作流。

它解决的核心问题只有 4 件：

1. 扫描你关注的标的期权链
2. 按策略阈值筛选 Sell Put / Covered Call 候选
3. 结合账户持仓与现金判断是否可做
4. 生成提醒、平仓建议和运行结果

这份 README 只保留产品使用需要的信息：
- 安装
- 初始化
- 运行
- 排障入口

更细的配置契约和运维细节见：
- [CONFIGS.md](CONFIGS.md)
- [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)
- [RUNBOOK.md](RUNBOOK.md)

---

## 1. 快速开始

### 1.1 安装依赖

```bash
git clone <repo-url> options-monitor
cd options-monitor
./run_watchlist.sh
```

如果你想手动安装环境：

```bash
python3 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt
```

`requirements.txt` 已包含 `futu-api`，缺少 Futu SDK 时会随安装流程一起补齐。

### 1.2 给 Agent 使用（可选）

如果你是把它作为本地 Agent 工具来用，执行：

```bash
bash scripts/install_agent_plugin.sh
./om-agent spec
```

常用方式：

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

`om-agent` 是面向程序/Agent 的结构化入口；`om` 是面向人工操作的 CLI 入口。

---

## 2. 初始化

### 2.1 启动 WebUI

```bash
./run_webui.sh
```

默认地址：

```text
http://127.0.0.1:8000
```

WebUI 现在按 6 个模块组织：

- 行情设置
- 账户设置
- 选股策略
- 平仓建议
- 消息通知
- 高级设置

首次初始化建议直接在 WebUI 中完成。

---

### 2.2 手工初始化（可选）

如果你不用 WebUI，也可以手工复制模板：

```bash
cp configs/examples/config.example.us.json config.us.json
cp configs/examples/config.example.hk.json config.hk.json
mkdir -p secrets
cp configs/examples/portfolio.sqlite.example.json secrets/portfolio.sqlite.json
```

---

## 3. 配置文件说明

你日常维护的文件通常只有：

- `config.us.json`
- `config.hk.json`
- `secrets/portfolio.sqlite.json`
- `secrets/notifications.feishu.app.json`（如果启用通知）

### 最小配置默认数据来源

- 行情与期权链：OpenD / Futu API
- 持仓与现金：Futu / OpenD
- 期权持仓存储：SQLite

---

## 4. 账户与持仓

- 支持多账户配置
- 持仓与现金默认来自 Futu / OpenD
- Feishu holdings 仍可作为 fallback / external holdings 来源

如果你需要：
- 多账户配置
- 账户级持仓来源
- 每账户 OpenD 持仓连接
- 通知凭证与高级配置

请直接查看：

- [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)
- [CONFIGS.md](CONFIGS.md)

---

## 5. 通知

当前正式通知链路支持：

- 飞书开放平台应用发个人消息

通知凭证默认放在：

- `secrets/notifications.feishu.app.json`

具体字段和配置方式见 [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)。

---

## 6. 常用命令

### 6.1 校验配置

```bash
./.venv/bin/python scripts/validate_config.py --config config.us.json
./.venv/bin/python scripts/validate_config.py --config config.hk.json
```

### 6.2 健康检查

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

如果你要指定配置路径：

```bash
./om-agent run --tool healthcheck --input-json '{"config_path":"config.us.json"}'
```

### 6.3 检查版本更新

```bash
./om version
```

### 6.4 手动跑一次 pipeline

```bash
./om scan-pipeline --config config.us.json
```

只跑某个阶段：

```bash
./om scan-pipeline --config config.us.json --stage fetch
./om scan-pipeline --config config.us.json --stage scan
./om scan-pipeline --config config.us.json --stage alert
./om scan-pipeline --config config.us.json --stage notify
```

### 6.5 多账户运行

推荐：

```bash
./om run tick --config config.us.json --accounts lx sy
```

兼容入口：

```bash
python3 scripts/send_if_needed_multi.py --config config.us.json --accounts lx sy
```

### 6.6 单账户入口

```bash
python3 scripts/send_if_needed.py --config config.us.json
```

---

## 7. 常见排障

### 7.1 配置校验失败
先跑：

```bash
python3 scripts/validate_config.py --config config.us.json
```

优先检查：
- `notifications.target`
- `notifications.secrets_file`
- `trade_intake.account_mapping.futu`
- `account_settings.<account>.type`
- `symbols[]`

---

### 7.2 healthcheck 只看到一个 OpenD endpoint
优先检查：
- 账户映射是否正确
- OpenD 是否真的在线
- 账户级持仓连接配置是否填写完整

详细字段说明见 [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)。

---

### 7.3 两个账户持仓看起来一样
优先检查：
- 是否两个账户都被映射到了同一个 Futu account id
- 是否账户配置实际指向了同一份持仓来源
- 是否仍然回退到了全局持仓配置

具体配置优先级见 [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)。

---

### 7.4 通知保存失败
优先检查：
- `notifications.target` 是否为空
- `secrets/notifications.feishu.app.json` 是否存在
- `app_id / app_secret` 是否完整

---

## 8. 文档导航

- [CONFIGS.md](CONFIGS.md)：配置来源与 canonical config 约定
- [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)：详细配置字段说明
- [RUNBOOK.md](RUNBOOK.md)：运维巡检与应急操作
- [tests/README.md](tests/README.md)：测试分层和运行方式

---

## 9. 风险提示

本工具只做监控、筛选和提醒，不构成投资建议。期权交易风险较高，任何下单都需要自行复核标的、价格、仓位、保证金、流动性和事件风险。

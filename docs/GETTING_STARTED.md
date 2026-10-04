# Getting Started

这份文档从“已经安装好代码”开始，目标是让普通用户把 OM 第一次安全跑起来。

还没安装时先看 [INSTALL.md](INSTALL.md)。

下文使用安装后的全局命令 `om`。如果你在源码 checkout 里工作，`./om` 也可以作为 fallback。

---

## 1. 先做只读检查

```bash
om setup check --format text
```

安装后在交互式终端直接运行 `om` 可按“首次安装”或“日常管理”选择任务；其中诊断项只读，初始化配置需预览并确认。`om help` / `om --help` 显示这两个场景的任务，`om help all` 列出全部顶层命令。下面的 `--format text` 便于人工阅读，不加时保留原有 JSON 输出供脚本使用。

`setup check` 只读。它不会写配置、不会写 env-file、不会启动服务、不会创建定时任务、不会连接 OpenD 或 Feishu。

它会检查：

- repo / venv / Python 依赖是否完整
- 当前配置所选市场的运行快照是否存在且可校验
- env-file 是否可解析，Feishu Bot 和写入开关是否配置
- runtime root 和期权持仓 SQLite 路径
- 本机是否已有 systemd/launchd service 或 timer
- 下一步应该运行什么命令

如果要忽略本地 `.env/options-monitor.env`，做一次隔离检查：

```bash
om setup check --no-local-env-file
```

---

## 2. 初始化配置

首次使用推荐运行 `om setup init`。它会询问输出目录、市场、账户标签、富途账户 ID，以及每个标的的 CSP/CC 策略和行权价边界；标的必填，不写入示例标的。CSP 必填最高行权价，CC 必填最低行权价，另一端边界可选。预览目标文件、实际标的与策略后，输入 `yes` 才写入。非交互场景须指定市场、标的和策略边界，例如先用 `om setup init --market us --us-symbol AAPL --symbol-strategy AAPL=csp --csp-max-strike AAPL=100 --dry-run --output-dir <path>` 预览（替换标的和价格），再以相同参数改用 `--apply`。已有目标文件或并发创建的目标都拒绝覆盖；失败时只清理本次创建且未被修改的文件。若强制中断后留下文件，先核对报出的冲突路径，不要直接覆盖或删除。输出目录应在安装的 release 目录外。

初始化成功会将目录写入 `~/.config/options-monitor/runtime-root`，新终端会自动使用它，并已生成所选市场的运行快照。显式配置路径和有效 `OM_RUNTIME_ROOT`（包括服务设置）优先于该记录；若预览提示存在覆盖，先核对其来源。记录损坏或指向失效目录时命令会报错，不会改读源码目录的配置。若富途账户 ID 留空，按输出中的 `om accounts edit` 命令预览并更新；`om setup check` 也会提示具体账户。秘密放 Keychain/systemd credentials，普通设置和写入开关放 env-file。

初始化后维护监控标的用 `om symbols`，它读取 `config.yaml`，写入前先预览；`--apply` 会经配置事务同时更新 YAML 和生成快照。默认查找有效 `OM_RUNTIME_ROOT`、用户目录记录，再回退源码目录；如需操作另一实例，显式传 `--config-yaml`。不要把生成的 `config.us.json` / `config.hk.json` 传给它。

```bash
om symbols list --market us
om symbols add YOUR_SYMBOL --strategy csp --csp-max-strike YOUR_MAX_STRIKE
om symbols add YOUR_SYMBOL --strategy csp --csp-max-strike YOUR_MAX_STRIKE --apply
om symbols rm YOUR_SYMBOL
```

将 `YOUR_SYMBOL` 和 `YOUR_MAX_STRIKE` 换成自己的标的与 CSP 行权价上限。新增时必须选 `--strategy csp|cc|both`：CSP 必须给 `--csp-max-strike`，CC 必须给 `--cc-min-strike`；CSP 下限和 CC 上限可选，`om symbols add --help` 列出全部参数。`om symbols` 会按 `NVDA` 或 `0700.HK` 等标准标的识别市场；`--market` 可省略，但显式指定时必须一致。只配置一个市场时，`list` 也可省略；配置多个市场时需指定。高级策略覆盖项可用 `om symbols edit YOUR_SYMBOL --set sell_call.enabled=true --set sell_call.min_strike=YOUR_MIN_STRIKE` 预览，或用 `om config symbol set --help` 查看专用参数。`om help` 按场景列出人工命令；供外部 Agent 使用的 JSON 工具另见 [Tool Reference](TOOL_REFERENCE.md)。

只有手动编辑 `config.yaml` 时才需自行重建快照；`om symbols` 和 `om accounts edit --apply --confirm` 已在配置事务中完成重建。手动编辑后，以美股为例运行：

```bash
OM_CONFIG_DIR="$(cat "$HOME/.config/options-monitor/runtime-root")"
om config validate --source yaml --market us --config-yaml "$OM_CONFIG_DIR/config.yaml"
om config build --source yaml --market us --config-yaml "$OM_CONFIG_DIR/config.yaml" --output "$OM_CONFIG_DIR/config.us.json"
om config validate --config-path "$OM_CONFIG_DIR/config.us.json" --market us
om setup check --market us --format text
```

高级初始化参数、assistant 配置构建和单项配置解释见 `om config --help`。旧 JSON authoring 和迁移命令已退役。
`setup check` 默认从当前 `config.yaml` 识别市场，也可显式传 `--market`。基本状态只覆盖所选市场的离线配置和安装条件：缺少快照或未替换富途账户 ID 占位符都会报错。Bot 状态单独显示；此检查不证明 OpenD 已登录、真实凭证可用或通知已送达。

---

## 3. 完成外部接入

### 富途 OpenAPI/OpenD（实时扫描必需）

安装器会安装 OM 的 Python 依赖，但 `setup init` 只记录富途账户 ID，不安装、不启动或登录 OpenD。按富途官方步骤准备 OpenD，并确认所选账户有需要的行情、交易权限；OM 的 OpenD 前置条件见 [部署指南](DEPLOY_LINUX_MAC.md#5-opend--futu-前置条件)。如需调整账户 ID、host 或 port，先查看 `om accounts edit --help`，预览后再应用。OpenD 就绪后按实际市场运行 `om doctor --config-key us` 或 `hk`；`setup check` 的离线通过不能代替连接检查。

### 通知通道（需要收取通知时）

选择 `feishu_app` 或 `wechat_clawbot`，按[配置指南的通知章节](../CONFIGURATION_GUIDE.md#通知)设置路由。WeChat 可用 `om channel wechat-clawbot connect` 扫码并绑定目标；Feishu 的 App ID、接收人等放普通设置，App Secret 用 `om secrets set feishu.bot.app_secret` 隐藏输入。完成后运行 `om channel status` 查看本地通道状态；这不证明消息已送达，真实发送需单独确认。

### Bot LLM（需要 Bot 问答时）

`setup init` 会生成默认 DeepSeek 模型配置，但不会取得 API key，也不证明模型可用。运行 `om assistant model catalog --format text` 查看支持的 provider，使用 `om assistant model add --help` / `om assistant model use --help` 选择模型；密钥按[秘密存储](SECRET_STORAGE.md)用 `om secrets set <逻辑名>` 隐藏输入。默认 DeepSeek 的逻辑名是 `llm.deepseek.api_key`。最后运行 `om assistant model check --active --format text` 检查配置和凭证状态；该检查不调用模型 API。

### 普通 env 与秘密存储

真实凭证不放 runtime config，也不默认放 env-file。macOS 使用 Keychain，Linux systemd 使用逐 unit encrypted credentials；完整逻辑名、CLI 和迁移流程见 [Secret Storage](SECRET_STORAGE.md)。

本地手动运行默认路径：

```bash
.env/options-monitor.env
```

Linux 推荐路径：

```bash
/etc/options-monitor/options-monitor.env
```

Mac launchd 推荐路径：

```bash
$HOME/Library/Application Support/options-monitor/options-monitor.env
```

手动运行时，先复制普通设置示例；需要检查秘密时只看脱敏状态：

```bash
mkdir -p .env
cp -n configs/examples/options-monitor.env.example .env/options-monitor.env
om settings doctor
om secrets status
```

长期服务使用的 env-file 应通过 `om settings doctor --env-file <path>` 单独检查。只有限时兼容场景才显式选择 `OM_SECRET_BACKEND=env`。

`settings doctor` 会脱敏显示来源和缺失项。

---

## 4. 跑系统诊断

```bash
om doctor --config-key us
om doctor --config-key hk
```

也可以直接看运行状态：

```bash
om status --config-key us
om runs --limit 10
```

如果需要把问题交给维护者排查，生成一份脱敏 support bundle：

```bash
om support bundle --config-key us
om support bundle --config-key us --include-healthcheck
```

`support bundle` 会写出一个 JSON 诊断包，默认包含 setup/settings/config/runtime status 快照，并脱敏 secret、token、webhook URL 和长数字账号。默认不跑 healthcheck；需要连同 OpenD readiness 一起收集时再加 `--include-healthcheck`。

---

## 5. 可选：Feishu long-connection

Feishu Bot 走同一组 `OM_FEISHU_BOT_*` env 设置。配置后先做只读检查：

```bash
om inbound feishu-ws --check
```

长期运行时才需要 service 化；不要在安装或初始化阶段自动启动。

---

## 6. 可选：长期运行服务

本地临时使用可以手动跑：

```bash
om run tick --config config.us.json --accounts lx
```

服务器长期运行先 render 服务文件。Linux 生产推荐：

```bash
om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --env-file /etc/options-monitor/options-monitor.env \
  --markets us hk \
  --accounts lx sy \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --config-us /var/lib/options-monitor/config.us.json \
  --config-hk /var/lib/options-monitor/config.hk.json \
  --include-feishu-ws \
  --output-dir /tmp/options-monitor-service
```

Mac launchd 推荐：

```bash
om service render \
  --target launchd \
  --runtime-root "$HOME/Library/Application Support/options-monitor" \
  --env-file "$HOME/Library/Application Support/options-monitor/options-monitor.env" \
  --markets us hk \
  --accounts lx sy \
  --config-yaml "$HOME/Library/Application Support/options-monitor/config.yaml" \
  --config-us "$HOME/Library/Application Support/options-monitor/config.us.json" \
  --config-hk "$HOME/Library/Application Support/options-monitor/config.hk.json" \
  --include-feishu-ws \
  --output-dir /tmp/options-monitor-service
```

`service render` 只生成文件和安装命令，不会自动 install、enable 或 start。确认后再按输出的命令安装和启用。

生成的 Runtime Status 服务使用 `om status --journal-summary`，journal 输出被限制为最多 20 行且不超过 16 KiB；完整结构化诊断仍通过 `om-agent` 的 `runtime_status` 工具读取。systemd 下，受控的 one-shot（包括 `auto-close-*` 和 Quality refresh/recheck/day-end）带有 `TimeoutStartSec`，用于终止 OpenD 异常时的无限挂起；tick、Runtime Status、projection verify 和长期 listener 不继承该限制。render 本身不会把这些变更应用到生产系统。

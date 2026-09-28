# Getting Started

这份文档从“已经安装好代码”开始，目标是让普通用户把 OM 第一次安全跑起来。

还没安装时先看 [INSTALL.md](INSTALL.md)。

下面的首跑路径以 macOS 默认 installer 安装为例，使用全局命令 `om`。
源码 checkout 可从仓库目录使用 `./om`；Linux 长期运行的目录见 [Linux / Mac Deployment](DEPLOY_LINUX_MAC.md)。
安装时使用了自定义 `--prefix`，把下文的 `REPO` 改成安装器输出的 `current` 路径。

## 推荐：终端交互首装

```bash
om setup init
```

向导依次询问手动运行或长期服务、运行目录、普通 env-file 路径、US/HK 市场、Futu 账户标签和数字 ID、初始标的。确认前不写配置；确认后生成所选市场的 YAML 和运行快照，再做一次**离线**检查。数字 ID 的格式正确不等于已经向券商核实账户身份。

请保存向导返回的 `runtime_root`、`env_file` 和 `next_steps`。在新终端照原样运行带路径的命令，例如：

```bash
RUNTIME="$HOME/Library/Application Support/options-monitor"
ENV_FILE="$RUNTIME/options-monitor.env"
om setup check --runtime-root "$RUNTIME" --env-file "$ENV_FILE" --market us
om config edit --runtime-root "$RUNTIME" --env-file "$ENV_FILE"
```

`config edit` 的菜单列出基础设置和 CSP、CC、Combo Yield、Wheel、Close Advice、Assistant/Bot、通知与外部持仓。选择 YAML 功能会在终端编辑器中打开权威 `config.yaml`，保存后先校验、显示变化的键，再确认发布。普通 env 设置和密钥菜单给出各自的终端命令；密钥值仅通过 `om secrets set` 的隐藏输入采集。

Linux 手动运行请选择当前用户可写的运行目录；长期 systemd 路径 `/var/lib/options-monitor` 和 `/etc/options-monitor/options-monitor.env` 需由管理员预先创建并交给部署用户。向导不会提权、安装或启动服务。Linux 普通 shell 无法直接消费 systemd encrypted credentials；相应集成在获得单独的凭据注入运行环境前仍是 `pending`。[部署细节](DEPLOY_LINUX_MAC.md)

以下步骤保留为逐条执行和排障路径；已完成交互首装时，不要再次运行 `config init` 覆盖刚创建的文件。

---

## 1. 可选：先做只读检查

```bash
REPO="$HOME/apps/options-monitor/current"
RUNTIME="$HOME/Library/Application Support/options-monitor"
export PATH="$HOME/.local/bin:$PATH"
export OM_RUNTIME_ROOT="$RUNTIME"
om setup check
```

`setup check` 只读。它不会写配置、不会写 env-file、不会启动服务、不会创建定时任务、不会连接 OpenD 或 Feishu。

它会检查：

- repo / venv / Python 依赖是否完整
- `config.us.json` / `config.hk.json` 是否存在且可校验
- env-file 是否可解析，Feishu Bot 和写入开关是否配置
- runtime root 和期权持仓 SQLite 路径
- 本机是否已有 systemd/launchd service 或 timer
- 下一步应该运行什么命令

如果要忽略本地 `.env/options-monitor.env`，做一次隔离检查：

```bash
om setup check --no-local-env-file
```

---

## 2. 手动初始化配置（未使用向导时）

推荐先维护 `config.yaml`。它只保存用户 override；系统默认来自代码里的 `DEFAULT_CONFIG`。秘密放 Keychain/systemd credentials，普通设置和写入开关放 env-file。
Mac 安装版将 YAML 和生成快照放在 Application Support，不依赖当前工作目录。新终端运行手动命令前，重新设置 `OM_RUNTIME_ROOT`；也可为单次命令显式传配置路径。

```bash
mkdir -p "$RUNTIME"
om config init --output "$RUNTIME/config.yaml" --runtime-output-dir "$RUNTIME" --no-build
${EDITOR:-vi} "$RUNTIME/config.yaml"
```

YAML 使用空格缩进，不要用 tab；示例采用 2 个空格。港股代码这类可能被 YAML 误判的值建议加引号，例如 `"0700.HK"`。
`--no-build` 先只生成 YAML，避免编辑前产生过期快照。默认 starter 只含 Futu 账户、所选市场，Assistant/Bot 和外部持仓关闭；缺少 Futu ID 时是草稿，不会自动生成就绪快照。已有文件时会拒绝覆盖；真实运行目录不要直接用 `--force` 覆盖。
`config build` / `config explain` 只读取 YAML；旧 JSON authoring 和迁移命令已退役。

先校验 YAML 合并代码默认值后的结果：

```bash
om config validate --source yaml --market us --config-yaml "$RUNTIME/config.yaml"
om config validate --source yaml --market hk --config-yaml "$RUNTIME/config.yaml"
```

再生成运行时 JSON 快照并校验：

```bash
om config build --source yaml --market us --config-yaml "$RUNTIME/config.yaml" --output "$RUNTIME/config.us.json"
om config build --source yaml --market hk --config-yaml "$RUNTIME/config.yaml" --output "$RUNTIME/config.hk.json"
om config build-assistant --source yaml --config-yaml "$RUNTIME/config.yaml" --output "$RUNTIME/resolved/config.assistant.json"
om config validate --config-path "$RUNTIME/config.us.json" --market us
om config validate --config-path "$RUNTIME/config.hk.json" --market hk
om setup check
```

富途账户 ID 需要在 YAML 中替换占位值，也可初始化时传 `--futu-acc-id <futu-account-id>`。

---

## 3. 配置普通 env 与秘密存储

真实凭证不放 runtime config，也不默认放 env-file。macOS 使用 Keychain，Linux systemd 使用逐 unit encrypted credentials；完整逻辑名、CLI 和迁移流程见 [Secret Storage](SECRET_STORAGE.md)。

源码 checkout 的 repo-local 兼容路径（交互向导使用自己选择的 env-file）：

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

Mac 安装版从安装的 release 复制普通设置示例；需要检查秘密时只看脱敏状态：

```bash
om config edit --runtime-root "$RUNTIME" --env-file "$RUNTIME/options-monitor.env"
om settings doctor --env-file "$RUNTIME/options-monitor.env"
om secrets status
om setup check --runtime-root "$RUNTIME" --env-file "$RUNTIME/options-monitor.env" --market us
```

普通 env-file 可以从安装目录的示例选择所需键后手动创建；示例含 Linux 绝对路径，不能原样复制到 Mac。`config edit` 的 env 菜单会给出终端编辑和复查命令，但外部编辑器保存后不会自动回滚；不要把密钥填进去。

源码 checkout 使用 repo-local `.env/options-monitor.env` 时，先进入该 checkout；相对模板路径只对该目录有效。
launchd 会由渲染器显式注入 `OM_ENV_FILE` 和 runtime root。手动启动的新终端仍需重新设置 `OM_RUNTIME_ROOT`，并在需要该 env-file 时设置 `OM_ENV_FILE="$RUNTIME/options-monitor.env"`。

长期服务使用的 env-file 应通过 `om settings doctor --env-file <path>` 单独检查。只有限时兼容场景才显式选择 `OM_SECRET_BACKEND=env`。

`settings doctor` 会脱敏显示来源和缺失项。

完成配置构建后再用同一组路径运行一次 `om setup check`。在输出的 `credential_guidance` 中，
`missing` 是已确认未保存，复制对应 `next_steps` 命令到自己的终端执行；`unknown` 表示
当前无法判断，先检查配置或用 `om secrets status --backend keychain`（macOS）、
`sudo "$HOME/apps/options-monitor/current/om" secrets status --backend systemd`（Linux）核对存储端。输入秘密时终端会隐藏输入并要求确认，
不要把值发到聊天框。完成后重跑 `om setup check`；`present` 只说明已保存，服务是否收到并成功使用
仍要在实际部署前单独验证。自定义安装路径或源码 checkout 时，把命令中的 `current/om` 改为对应的绝对路径。


---

## 4. 跑系统诊断

```bash
om doctor --config-key us --config-path "$RUNTIME/config.us.json" --env-file "$RUNTIME/options-monitor.env"
# 只有选了 HK 并生成快照时再运行：
om doctor --config-key hk --config-path "$RUNTIME/config.hk.json" --env-file "$RUNTIME/options-monitor.env"
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
om run tick --config "$RUNTIME/config.us.json" --accounts lx
```

服务器长期运行先 render 服务文件。以下以只选择 US、账户 `lx`、未启用 Feishu 的最小配置为例；使用 HK 或其它账户时，按已生成的 YAML 和快照修改参数。Linux 生产示例：

```bash
om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --env-file /etc/options-monitor/options-monitor.env \
  --markets us \
  --accounts lx \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --config-us /var/lib/options-monitor/config.us.json \
  --output-dir /tmp/options-monitor-service
```

Mac launchd 推荐：

```bash
om service render \
  --target launchd \
  --repo-root "$REPO" \
  --runtime-root "$RUNTIME" \
  --env-file "$RUNTIME/options-monitor.env" \
  --markets us \
  --accounts lx \
  --config-yaml "$RUNTIME/config.yaml" \
  --config-us "$RUNTIME/config.us.json" \
  --output-dir /tmp/options-monitor-service
```

`service render` 只生成文件和安装命令，不会自动 install、enable 或 start。确认后再按输出的命令安装和启用。

生成的 Runtime Status 服务使用 `om status --journal-summary`，journal 输出被限制为最多 20 行且不超过 16 KiB；完整结构化诊断仍通过 `om-agent` 的 `runtime_status` 工具读取。systemd 下，受控的 one-shot（包括 `auto-close-*` 和 Quality refresh/recheck/day-end）带有 `TimeoutStartSec`，用于终止 OpenD 异常时的无限挂起；tick、Runtime Status、projection verify 和长期 listener 不继承该限制。render 本身不会把这些变更应用到生产系统。

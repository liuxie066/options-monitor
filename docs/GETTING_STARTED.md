# Getting Started

这份文档从“已经安装好代码”开始，目标是让普通用户把 OM 第一次安全跑起来。

还没安装时先看 [INSTALL.md](INSTALL.md)。

下文使用安装后的全局命令 `om`。如果你在源码 checkout 里工作，`./om` 也可以作为 fallback。

---

## 1. 先做只读检查

```bash
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

## 2. 初始化配置

推荐先维护 `config.yaml`。它只保存用户 override；系统默认来自代码里的 `DEFAULT_CONFIG`，secrets 和写入开关放 env-file。
下面的本地示例以 repo root 为工作目录；installer 安装后可以先 `cd "$HOME/apps/options-monitor/current"`。生产服务建议把 `config.yaml` 和生成后的 runtime config 放在 release 目录外，再显式传 `--config-yaml` / `--output`。

```bash
om config init --output config.yaml --runtime-output-dir .
$EDITOR config.yaml
```

YAML 使用空格缩进，不要用 tab；示例采用 2 个空格。港股代码这类可能被 YAML 误判的值建议加引号，例如 `"0700.HK"`。
`config init` 默认生成 `config.yaml`，并构建 `config.us.json` / `config.hk.json`。已有文件时会拒绝覆盖；确认要重建 starter 时再加 `--force`。
`config build` / `config explain` 读取 YAML；旧 JSON authoring 需要先迁移到 `config.yaml`。

先校验 YAML 合并代码默认值后的结果：

```bash
om config validate --source yaml --market us
om config validate --source yaml --market hk
```

再生成运行时 JSON 快照并校验：

```bash
om config build --source yaml --market us --output config.us.json
om config build --source yaml --market hk --output config.hk.json
om config validate --config-path config.us.json --market us
om config validate --config-path config.hk.json --market hk
```

已有 legacy `configs/user.*.json` 时，先预览迁移，确认后再写入 `config.yaml`：

```bash
om config migrate-yaml --output config.yaml
om config migrate-yaml --output config.yaml --apply
```

如果是 installer 安装后的空目录首跑，也可以不进入 release 目录：

```bash
mkdir -p ~/options-monitor-first-run
cd ~/options-monitor-first-run
om config init --output config.yaml --runtime-output-dir runtime-config --futu-acc-id <futu-account-id>
om config validate --source yaml --market us --config-yaml config.yaml
om config build --source yaml --market hk --config-yaml config.yaml --output runtime-config/config.hk.json --dry-run
om support bundle --config-path runtime-config/config.us.json --output-dir support --no-local-env-file
```

---

## 3. 配置 env-file

真实凭证放 env-file，不放 runtime config。

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

手动运行时，先复制示例，再按需填写：

```bash
mkdir -p .env
cp -n configs/examples/options-monitor.env.example .env/options-monitor.env
om settings doctor
```

长期服务使用的 env-file 应通过 `om settings doctor --env-file <path>` 单独检查。

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

生成的 Runtime Status 服务使用 `om status --journal-summary`，journal 输出被限制为最多 20 行且不超过 16 KiB；完整结构化诊断仍通过 `om-agent` 的 `runtime_status` 工具读取。systemd 下，受控的 one-shot（包括 `auto-close-*`、Position Advice promotion、Quality refresh/recheck/day-end 和显式启用的 `strategy-lab-sample`）带有 `TimeoutStartSec`，用于终止 OpenD 异常时的无限挂起；tick、Runtime Status、projection verify 和长期 listener 不继承该限制。render 本身不会把这些变更应用到生产系统。

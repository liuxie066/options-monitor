# Linux / Mac Deployment

这份文档用于把 `options-monitor` 部署成长期运行的本机服务。Linux 和 Mac 共用同一套 CLI，差别只在服务管理器。

## 1. 运行时目录契约

部署后必须区分两个根目录：

| 目录 | 用途 |
|---|---|
| `repo_root` | 代码、`./om`、`./om-agent`、canonical config |
| `runtime_root` | 所有运行时状态、报告、SQLite、日志、锁 |

所有运行时产物都应落在 `runtime_root`：

```text
<runtime_root>/output_runs/
<runtime_root>/output_shared/
<runtime_root>/output_accounts/
<runtime_root>/output/
<runtime_root>/logs/
<runtime_root>/locks/
```

期权持仓 SQLite 固定为：

```text
<runtime_root>/output_shared/state/option_positions.sqlite3
```

不要再用 `option_positions.sqlite_path` 作为 active DB 配置。该字段只作为旧库诊断/迁移线索。

## 2. 安装依赖

最小运行依赖：

```bash
python3 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt -c constraints.txt
```

可选 API/server 依赖：

```bash
./.venv/bin/pip install -r requirements/server.txt -c constraints/server.txt
```

开发和验证依赖：

```bash
./.venv/bin/pip install -r requirements/dev.txt -c constraints/dev.txt
```

`futu-api` 的默认固定版本在 constraints 中，业务代码不应依赖某个硬编码版本判断行为。

## 3. Linux: systemd

推荐目录：

```bash
REPO=/opt/options-monitor
RUNTIME=/var/lib/options-monitor
ENV_FILE=/etc/options-monitor/options-monitor.env
DEPLOY_USER=liuxie
sudo mkdir -p "$RUNTIME" "$RUNTIME/logs" "$RUNTIME/locks" "$RUNTIME/output_accounts" "$RUNTIME/output_shared"
sudo chown -R "$DEPLOY_USER":"$DEPLOY_USER" "$RUNTIME"
```

发布环境变量文件：

```bash
sudo mkdir -p /etc/options-monitor
sudo install -m 600 -o root -g root configs/examples/options-monitor.env.example "$ENV_FILE"
sudoedit "$ENV_FILE"
```

`$ENV_FILE` 必须保留在服务器本地，填入真实 Feishu 凭证和表引用，不通过 git 发布。

渲染服务文件：

```bash
cd "$REPO"
./om service render \
  --target systemd \
  --repo-root "$REPO" \
  --runtime-root "$RUNTIME" \
  --env-file "$ENV_FILE" \
  --deploy-user "$DEPLOY_USER" \
  --markets us hk \
  --accounts lx sy \
  --output-dir /tmp/options-monitor-service
```

传入 `--deploy-user "$DEPLOY_USER"` 后，渲染出的 systemd unit 会包含：

```ini
User=liuxie
Environment="HOME=/home/liuxie"
Environment="OM_RUNTIME_ROOT=/var/lib/options-monitor"
```

`liuxie` 只是上面示例里的服务器运行用户，不是代码默认值。如果 HOME 不在 `/home/<user>`，再传 `--deploy-home <path>`。如果不传 `--deploy-user` 且未设置 `OM_DEPLOY_USER` / `DEPLOY_USER`，systemd unit 不会写 `User=` / `HOME=`。

安装前先跑只读 preflight：

```bash
./om service preflight \
  --runtime-root "$RUNTIME" \
  --env-file "$ENV_FILE" \
  --config-us config.us.json \
  --config-hk config.hk.json \
  --accounts lx sy
```

preflight 会检查 env path 是文件还是目录、runtime root / locks / output_accounts / output_shared 权限、`output` 是否为 symlink，以及 runtime config 是否带 `_generated` 元数据。
如果 `output` 已经是普通目录，先 dry-run，再确认迁移：

```bash
./om service repair-output --runtime-root "$RUNTIME" --default-account lx
./om service repair-output --runtime-root "$RUNTIME" --default-account lx --confirm
```

修复会先备份真实目录，再把内容迁移到 `output_accounts/<default-account>`，最后创建 `output -> output_accounts/<default-account>` symlink。

安装：

```bash
sudo cp /tmp/options-monitor-service/systemd/*.service /etc/systemd/system/
sudo cp /tmp/options-monitor-service/systemd/*.timer /etc/systemd/system/
sudo mkdir -p "$RUNTIME"
cp /tmp/options-monitor-service/service.profile.json "$RUNTIME/service.profile.json"
sudo systemd-analyze verify /etc/systemd/system/options-monitor-*.service
sudo systemctl daemon-reload
sudo systemctl enable --now options-monitor-tick-us.timer
sudo systemctl enable --now options-monitor-tick-hk.timer
sudo systemctl enable --now options-monitor-auto-close-us.timer
sudo systemctl enable --now options-monitor-auto-close-hk.timer
sudo systemctl enable --now options-monitor-runtime-status.timer
sudo systemctl enable --now options-monitor-trade-intake.service
```

检查：

```bash
./om service status --profile-path "$RUNTIME/service.profile.json" --include-service-status
./om-agent run --tool runtime_status --input-json "{\"profile_path\":\"$RUNTIME/service.profile.json\"}"
./om option-positions store inspect --config config.us.json
```

线上查 runtime 时优先带 profile path；如果直接用 `config_key`，确保当前 shell 带上 `OM_RUNTIME_ROOT=$RUNTIME`，否则会读 repo 下默认 runtime。

## 4. Mac: launchd

推荐 runtime：

```bash
REPO="$HOME/workspace/options-monitor"
RUNTIME="$HOME/Library/Application Support/options-monitor"
mkdir -p "$RUNTIME" "$RUNTIME/logs" "$RUNTIME/locks"
```

渲染：

```bash
cd "$REPO"
./om service render \
  --target launchd \
  --repo-root "$REPO" \
  --runtime-root "$RUNTIME" \
  --markets us hk \
  --accounts lx sy \
  --output-dir /tmp/options-monitor-service
```

安装：

```bash
mkdir -p "$HOME/Library/LaunchAgents"
cp /tmp/options-monitor-service/launchd/*.plist "$HOME/Library/LaunchAgents/"
cp /tmp/options-monitor-service/service.profile.json "$RUNTIME/service.profile.json"
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.options-monitor.tick-us.plist"
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.options-monitor.tick-hk.plist"
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.options-monitor.auto-close-us.plist"
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.options-monitor.auto-close-hk.plist"
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.options-monitor.runtime-status.plist"
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.options-monitor.trade-intake.plist"
```

检查：

```bash
./om service status --profile-path "$RUNTIME/service.profile.json" --include-service-status
./om-agent run --tool runtime_status --input-json "{\"profile_path\":\"$RUNTIME/service.profile.json\"}"
```

## 5. OpenD / Futu 前置条件

`options-monitor` 不托管 OpenD 本身。部署前必须确认：

- Linux 机器能连接可用 OpenD host/port，或本机已运行 OpenD。
- Mac 机器的 OpenD 登录状态稳定，launchd 服务能访问同一端口。
- runtime config 中的 `fetch.host` / `fetch.port` 指向正确地址。
- OpenD Telnet 已启用，`FutuOpenD.xml` 中应包含 `telnet_ip=127.0.0.1`、`telnet_port=22222`。
- 手机验证码需要通过 Telnet 提交；提交后 `program_status_type=READY`，且 `qot_logined=true`、`trd_logined=true`。

检查 OpenD readiness：

```bash
./om healthcheck --config-key us --accounts lx sy --opend-telnet-host 127.0.0.1 --opend-telnet-port 22222
```

`healthcheck` 的 `opend_readiness*` 检查会展示 OpenD global state 和 Telnet 是否监听。Telnet 未监听不会替代 OpenD API readiness，但会明确提示手机验证码无法通过 Telnet 提交。

## 6. 切换旧数据

如果旧 runtime 里已有真实数据，先备份再迁移：

```bash
OLD_RUNTIME=/path/to/old-runtime
NEW_RUNTIME=/var/lib/options-monitor
mkdir -p "$NEW_RUNTIME/output_shared/state"
cp "$OLD_RUNTIME/output_shared/state/option_positions.sqlite3" "$NEW_RUNTIME/output_shared/state/option_positions.sqlite3"
```

迁移后只用 canonical store 诊断：

```bash
./om option-positions store inspect --config config.us.json
./om option-positions rebuild --config config.us.json
```

如果 `store inspect` 报告 active DB 为空但 legacy DB 有数据，先处理迁移，不要让服务带着双库并行状态启动。

## 7. 安全边界

- `service render` 只渲染文件，不安装、不启动。
- `runtime_status` / `service status` 是只读诊断。
- `tick --no-send` 会写本地 runtime，但不发通知。
- `auto-close-expired --apply` 会写持仓账本并可能发回执；上线前先跑 `--dry-run`。

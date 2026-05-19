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

如果要从飞书发消息控制 OM，还需要填入 inbound gateway 相关值：

```bash
OM_INBOUND_FEISHU_APP_ID=cli_xxx
OM_INBOUND_FEISHU_APP_SECRET=xxx
OM_INBOUND_FEISHU_ENCRYPT_KEY=xxx
OM_INBOUND_FEISHU_VERIFICATION_TOKEN=xxx
OM_INBOUND_ALLOWED_SENDERS=feishu:ou_xxx
```

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
  --include-feishu-gateway \
  --output-dir /tmp/options-monitor-service
```

`--include-feishu-gateway` 会生成 `options-monitor-feishu-gateway.service`，默认监听 `127.0.0.1:8765/feishu/events`。建议用 Nginx/Caddy/Cloudflare Tunnel 对外提供 HTTPS，再反代到本地 gateway。

如果要启用远端自动升级，建议 `$REPO` 使用 `/opt/options-monitor/current` 这样的 symlink 布局，并额外传：

```bash
./om service render \
  --target systemd \
  --repo-root /opt/options-monitor/current \
  --runtime-root "$RUNTIME" \
  --env-file "$ENV_FILE" \
  --deploy-user "$DEPLOY_USER" \
  --markets us hk \
  --accounts lx sy \
  --include-auto-upgrade \
  --output-dir /tmp/options-monitor-service
```

启用 `--include-auto-upgrade` 时，渲染器会保留 `--repo-root` 传入的 symlink 字面路径，并默认把 tick / trade-intake / maintenance config 指到 runtime root 下的 `config.us.json` / `config.hk.json`。这样 release 切换只移动代码，不绑定 release 目录内的生产配置。需要用非默认路径时，显式传 `--config-us` / `--config-hk`。

自动升级切换 release 前，会恢复新 release 缺失的 `configs/user.common.json`、`configs/user.hk.json`、`configs/user.us.json`。来源包括 runtime config metadata 里的 source path、`<runtime_root>/configs/`、当前 release，以及 `releases/` 下最近一个包含完整 overlay 的旧 release。随后会根据 profile 里的 config path 执行 `./om config build` / `./om config validate`；切换 symlink 后还会再用 current symlink 重建/校验一次。如果仍缺少必要 overlay 或 rebuild/validate 失败，升级会记录 remediation 并阻止未切换场景继续切换，避免 tick 进入 runtime config stale 状态。

传入 `--deploy-user "$DEPLOY_USER"` 后，渲染出的 systemd unit 会包含：

```ini
User=liuxie
Environment="HOME=/home/liuxie"
Environment="OM_RUNTIME_ROOT=/var/lib/options-monitor"
```

`liuxie` 只是上面示例里的服务器运行用户，不是代码默认值。如果 HOME 不在 `/home/<user>`，再传 `--deploy-home <path>`。如果不传 `--deploy-user` 且未设置 `OM_DEPLOY_USER` / `DEPLOY_USER`，systemd unit 不会写 `User=` / `HOME=`。

自动升级 timer 也会以该用户运行。systemd 系统级 service 的重启需要 root 权限，因此渲染出的 `service.profile.json` 会把长期 trade-intake 重启策略标记为 `sudo -n systemctl restart ...`。请给部署用户配置最小 sudoers 授权：

```sudoers
liuxie ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-trade-intake.service
liuxie ALL=(root) NOPASSWD: /usr/bin/systemctl restart options-monitor-trade-intake.service
```

如果服务器上的 `systemctl` 只有其中一个路径，只保留对应那一行即可。

安装前先跑只读 preflight：

```bash
./om service preflight \
  --runtime-root "$RUNTIME" \
  --env-file "$ENV_FILE" \
  --config-us "$RUNTIME/config.us.json" \
  --config-hk "$RUNTIME/config.hk.json" \
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
sudo systemctl enable --now options-monitor-projection-verify.timer
sudo systemctl enable --now options-monitor-runtime-status.timer
sudo systemctl enable --now options-monitor-trade-intake.service
sudo systemctl enable --now options-monitor-feishu-gateway.service
```

如果 render 时传了 `--include-auto-upgrade`，再启用升级 timer：

```bash
sudo systemctl enable --now options-monitor-upgrade.timer
```

`options-monitor-projection-verify.timer` 每天北京时间 06:00 运行一次 `./om option-positions verify-projection --mode auto`，用于校验 `trade_events -> position_lots` 并复用 checkpoint。
`options-monitor-auto-close-*.timer` 每天北京时间 05:30 运行一次 `./om option-positions auto-close-expired --apply --quiet`，先处理过期自动平仓，再由 06:00 的 projection verify 做内部对账。
`options-monitor-tick-us.timer` 使用 `OnCalendar=Mon..Fri *-*-* 09..16:00/10:00 America/New_York`，按美东时间 10 分钟整数边界唤醒。
`options-monitor-tick-hk.timer` 使用 `OnCalendar=Mon..Fri *-*-* 09..16:00/10:00 Asia/Hong_Kong`，按香港时间 10 分钟整数边界唤醒；是否真正扫描/通知仍由 `tick-cron` scheduler 的 run points 决定。
`options-monitor-upgrade.timer` 只有在 render 时传了 `--include-auto-upgrade` 才会生成；它每天北京时间 06:10 检查最新 release tag，发现可升级版本后会在目标 release 内创建 `.venv`、安装 runtime/server 依赖、校验 `om-agent spec` 和 tick 运行解释器，再切换 `/opt/options-monitor/current` 并写入 `upgrade_status.json`。

检查：

```bash
./om service status --profile-path "$RUNTIME/service.profile.json" --include-service-status
./om-agent run --tool runtime_status --input-json "{\"profile_path\":\"$RUNTIME/service.profile.json\"}"
./om option-positions store inspect --config config.us.json
./om option-positions --data-config "$RUNTIME/portfolio.runtime.json" verify-projection --mode full
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
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.options-monitor.projection-verify.plist"
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.options-monitor.runtime-status.plist"
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.options-monitor.trade-intake.plist"
```

launchd 的日历时间按 Mac 本机时区执行；要等价于北京时间 05:30 / 06:00，Mac 的系统时区需要设为中国标准时间或等价时区。

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

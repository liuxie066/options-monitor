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
<runtime_root>/logs/
<runtime_root>/locks/
```

期权持仓 SQLite 固定为：

```text
<runtime_root>/output_shared/state/option_positions.sqlite3
```

不要再用 `option_positions.sqlite_path` 作为 active DB 配置。运行时忽略该旧字段；如发现旧库，只能先离线修复为 canonical `trade_events`，再写入 active DB。

## 2. 安装依赖

最小运行依赖：

```bash
python3.12 -m venv .venv
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

`futu-api` / `yfinance` 这类数据源 SDK 不在 constraints 中精确锁死；`requirements/runtime.txt` 只声明最低能力版本，升级时应安装当前可用版本并通过发布验证。

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

发布普通环境设置文件（不填真实秘密）：

```bash
sudo install -d -m 700 /etc/options-monitor
sudo test -f "$ENV_FILE" || sudo install -m 600 -o root -g root configs/examples/options-monitor.env.example "$ENV_FILE"
sudoedit "$ENV_FILE"
```

`$ENV_FILE` 必须保留在服务器本地，只填写 App ID、表引用、路径和开关等普通设置，不通过 git 发布。真实秘密使用 [Secret Storage](SECRET_STORAGE.md) 的 systemd encrypted credential 流程。

如果要通过同一个飞书 Bot 接收命令、自动回复和发送主动通知，env-file 只填写非秘密标识：

```bash
OM_FEISHU_BOT_APP_ID=cli_xxx
OM_FEISHU_BOT_USER_OPEN_ID=ou_xxx
OM_FEISHU_BOT_ALLOWED_OPEN_IDS=ou_xxx
```

Bot secret 使用 `./om secrets set feishu.bot.app_secret` 的隐藏输入单独 provision，不要粘贴到命令或 env-file。

Feishu long-connection 的 reaction、reply、queue 行为配置在 assistant config 的 `inbound.feishu_ws` 下，不写入服务器 secret env file。使用 YAML authoring 时，先用 `./om config build-assistant --source yaml --config-yaml "$RUNTIME/config.yaml" --output "$RUNTIME/resolved/config.assistant.json"` 生成该文件；服务渲染会把它作为 `--assistant-config` 传给 Feishu WS。

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
  --config-yaml "$RUNTIME/config.yaml" \
  --config-us "$RUNTIME/config.us.json" \
  --config-hk "$RUNTIME/config.hk.json" \
  --include-feishu-ws \
  --include-secret-credentials \
  --output-dir /tmp/options-monitor-service
```

`--include-feishu-ws` 会生成 `options-monitor-feishu-ws.service`。它通过飞书长连接接收事件，不监听本地 HTTP 端口，也不需要公网回调 URL、Nginx/Caddy 或 Cloudflare Tunnel。服务会使用 `/var/lib/options-monitor/locks/feishu-ws.lock` 防止同一个 Feishu App 启动多个长连接客户端。

推荐的 `--include-secret-credentials` 默认为每个消费 unit 生成只包含所需
`LoadCredentialEncrypted=` 的 drop-in，不解密为共享 env 文件。它只渲染配置，不创建或修改真实凭据。

对于无法启用 systemd credential mount namespace 的受限 Incus/LXC 容器，在同一命令中显式增加：

```bash
--secret-credential-delivery runtime-files
```

该模式会渲染并由 service drift 管理 `/usr/local/libexec/options-monitor-materialize-service-credentials`
（`0755 root:root`）及逐 unit drop-in。helper 只把该 unit 需要的凭据解密到
`/run/options-monitor/credentials/<unit>/`，启动时验证 `/run` 为 tmpfs，停止时清理。该模式不会自动回退到
`OM_SECRET_BACKEND=env`，也不需要修改 Incus 宿主配置。两种安全交付模式都会从服务进程环境中
移除固定注册表里的旧 secret env 名；env-file 只保留非秘密配置。

以下 `--include-feishu-agent-credential` 是存量迁移兼容路径，不得与推荐开关同时使用：

```bash
--include-feishu-agent-credential
```

这是显式 opt-in，只支持 systemd。它会把以下不含明文秘密的资产纳入同一个 `service.profile.json` 和 service drift 契约：

- `/etc/systemd/system/options-monitor-feishu-agent-credential.service`；
- `/usr/local/libexec/options-monitor-materialize-feishu-agent-credential`，权限 `0755`；
- 每个由该 profile 生成的非 OpenD options-monitor service 下的 `zzzz-feishu-agent-credential.conf` drop-in。

默认读取 `/etc/credstore.encrypted/pm-feishu-agent-app-secret` 和 `/etc/credstore.encrypted/om-feishu-holdings-app-secret`，将解密结果原子写入 tmpfs 上的 `/run/credentials/options-monitor-feishu-agent.env`，权限为 `0440 root:<deploy_user>`。渲染和 drift 不会创建、修改或输出加密凭据；这两个 credential store 必须由运维事先独立配置。OpenD 不读取 Feishu 凭据，因此不会添加该 drop-in。

存量主机不要手工改写 `service.profile.json`。升级到包含新模式的 release 后，使用受控迁移命令：

```bash
./om service credentials-migrate \
  --repo-root /opt/options-monitor/current \
  --runtime-root /var/lib/options-monitor \
  --secret-credential-delivery runtime-files

# 核对 dry-run 后才执行
./om service credentials-migrate \
  --repo-root /opt/options-monitor/current \
  --runtime-root /var/lib/options-monitor \
  --secret-credential-delivery runtime-files \
  --confirm --yes
```

命令不运行 oneshot/timer 业务服务，不修改加密 store；它会在新长运行消费者重启成功后退役旧共享 env 明文。
详细的预检、回滚和残余验证契约见 [Secret Storage](SECRET_STORAGE.md)。

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
  --config-yaml "$RUNTIME/config.yaml" \
  --config-us "$RUNTIME/config.us.json" \
  --config-hk "$RUNTIME/config.hk.json" \
  --include-auto-upgrade \
  --output-dir /tmp/options-monitor-service
```

启用 `--include-auto-upgrade` 时，渲染器会保留 `--repo-root` 传入的 symlink 字面路径，并默认把 tick / trade-intake / Feishu WS / maintenance config 指到 runtime root 下的 `config.us.json` / `config.hk.json`。这样 release 切换只移动代码，不绑定 release 目录内的生产配置。必须同时传 `--config-yaml "$RUNTIME/config.yaml"`；profile 会记录 YAML source，`update apply` 会用 `config build --source yaml` 重建 runtime config，并用 `config build-assistant --source yaml` 重建 `$RUNTIME/resolved/config.assistant.json`。缺少可用 YAML authoring source 时升级会 fail closed，不再从 legacy JSON profile 恢复。

升级切换 release 后还会做一次 service drift reconcile：以当前 release 的 `service render` 结果为期望状态，对比 `$RUNTIME/service.profile.json`、systemd unit 和 profile 显式声明的 helper/drop-in。缺失 unit 会被写入 `/etc/systemd/system/`，缺失 timer 和 credential oneshot 会执行 `systemctl enable --now`；credential helper 还会校验内容、`0755` 权限和 oneshot `Result=success`。随后升级流程会用 reconcile 后的 profile 重启长期运行的 trade-intake / Feishu WS，并执行服务 active/enabled 检查；Feishu WS 还会运行 `./om inbound feishu-ws --check`，避免长驻进程继续使用旧 release、旧 config 或不可用 env。

`--no-restart-services` 只控制长期 service restart。若升级前已为维护显式暂停 systemd timer，同时传 `--preserve-activation-state`；控制面会在 release 切换前记录既有 timer 状态，并让 inactive、disabled 或 masked timer 在升级、失败补偿和 rollback 中保持暂停。定义文件仍会更新和 `daemon-reload`，但保留的 timer 不会被 `enable --now` 或 `restart`，避免 Persistent timer 在升级过程中补跑。

如果要让远端持续积累 Strategy Lab / Shadow Replay 复盘数据，额外显式开启 recorder：

```bash
./om service render \
  --target systemd \
  --repo-root "$REPO" \
  --runtime-root "$RUNTIME" \
  --env-file "$ENV_FILE" \
  --deploy-user "$DEPLOY_USER" \
  --markets us hk \
  --accounts lx sy \
  --config-yaml "$RUNTIME/config.yaml" \
  --config-us "$RUNTIME/config.us.json" \
  --config-hk "$RUNTIME/config.hk.json" \
  --include-strategy-lab-recorder \
  --strategy-lab-recorder-source opend \
  --strategy-lab-recorder-account lx \
  --strategy-lab-recorder-max-datasets 5 \
  --output-dir /tmp/options-monitor-service
```

这个开关会生成三类独立 timer：

- `options-monitor-strategy-lab-build.timer`：每 6 小时幂等构建 latest scanned run 对应的 Shadow Replay dataset；dataset id 默认使用 run id，已存在就跳过，不覆盖已有 mark path。build 只建立 cohort，不占用 mark/settle 维护批次。
- `options-monitor-strategy-lab-sample.timer`：每 2 小时只执行 mark path 采样，单次最多处理 `--strategy-lab-recorder-max-datasets` 个 dataset。`--strategy-lab-recorder-source opend` 会从 canonical config 解析 `--strategy-lab-recorder-account` 的 OpenD host/port，并把端点显式写入采样命令。选择了多个 Futu 账户时必须显式给出 recorder account；只有一个 Futu 账户时可以省略。若同一次 render 也包含 `--include-opend`，systemd unit 只依赖该账户对应的 OpenD service，不依赖其他账户。
- `options-monitor-strategy-lab-settle.timer`：每天北京时间 07:20 尝试 settle 所有到期的 outcome facts；settlement 只读取本地 dataset，不占用 OpenD 采样批次。

如果 OpenD 由外部服务管理，不传 `--include-opend` 即可；渲染出的采样命令仍包含所选账户的显式 host/port，但不会伪造 systemd 依赖。部署前必须单独确认该端点可用。recorder 只写 `$RUNTIME`/repo 下的本地 research artifact、Shadow Replay dataset、required-data / OpenD cache / rate-limit state 和 receipt。它不发通知，不运行 experiment/proposal，不调用在线 AI，不修改 runtime config、交易状态、Feishu 或 broker-facing state。升级时 `service.profile.json` 会保留 `strategy_lab_recorder` opt-in 和账户绑定；service drift 会按绑定账户从 canonical config 重新解析端点，因此配置变化会显示为 drift。不传 recorder 开关则默认不启用。

### AI Decision Advice 外部证据 Collector

当 `config.yaml` 中 `ai_decision_advice.enabled: true` 时，`service render` 会自动额外渲染：

- `/etc/systemd/system/options-monitor-ai-evidence-collector.service`：由 systemd 执行内部 Python module wrapper，不通过 `./om` 公开 collector 命令；
- `/etc/systemd/system/options-monitor-ai-evidence-collector.timer`：每 4 小时刷新外部证据（`OnBootSec=2min` + `OnUnitActiveSec=4h`，`Persistent=true`）。

Collector 只用公开 symbol 身份和 DeepSeek Responses `web_search`，不读取持仓/候选；
运行前必须 provision 逻辑凭据 `llm.deepseek.api_key`，并由 collector unit 选定的逐 unit
credential delivery 模式单独注入。未开启 `ai_decision_advice.enabled` 时不渲染这两个
unit，默认关闭；显式开启代表同意按设计文档第 18 节的最小数据合同向 DeepSeek
传输数据。本功能不提供 operator/manual refresh 命令；Provider 原始响应、搜索
query/call ID 不落盘。设计契约见 `docs/AI_DECISION_ADVICE_DESIGN.md`。

组合分布是独立的可选依赖，不是 Collector 的输入。只有以下显式配置才会在正常 Tick
中按账户读取 portfolio-management：

```yaml
ai_decision_advice:
  enabled: true
  portfolio_distribution:
    provider: portfolio_management
```

服务环境可用 `PORTFOLIO_SERVICE_URL` 指向 loopback PM 服务（默认
`http://127.0.0.1:8765`）；非 loopback 地址会被拒绝。OM 账户通过
`account_settings.<account>.holdings_account` 映射到 PM 账户，未配置时使用同名账户；每个
账户单独请求和校验，禁止跨账户聚合。每次 Tick 将结果封存到
`output_runs/<run_id>/accounts/<account>/state/prepared_portfolio_distribution.v1.json`。
provider 为 `none`、PM 未安装、不可达、账户不匹配或返回数据质量不足时，仍生成明确的
unavailable prepared envelope；Candidate Engine、原始候选和正常回执不会被阻断，AI
动作最高降为 `needs_review`，且不会用 Futu/holdings 数据替代 PM 组合分布。

受控上线检查应分别确认：collector service/timer 的 active/enabled 状态；共享观察集合、
身份和证据文件为私有权限；下一次正常 scheduled Tick 产生账户级 prepared PM/option
工件、正式 Advice JSONL 和对应 Daily Brief 投影；`daily_decision_brief_read` 只读结果与
回执动作一致。不要通过手动刷新 Collector、临时模型调用或真实测试通知完成 canary；
发布、远端升级和真实通知仍需要各自授权。

传入 `--deploy-user "$DEPLOY_USER"` 后，渲染出的 systemd unit 会包含：

```ini
User=liuxie
Environment="HOME=/home/om"
Environment="OM_RUNTIME_ROOT=/var/lib/options-monitor"
```

`liuxie` 只是上面示例里的服务器运行用户，不是代码默认值。如果 HOME 不在 `/home/<user>`，再传 `--deploy-home <path>`。如果不传 `--deploy-user` 且未设置 `OM_DEPLOY_USER` / `DEPLOY_USER`，systemd unit 不会写 `User=` / `HOME=`。

自动升级 timer 也会以该用户运行。systemd 系统级 service 的重启需要 root 权限，因此渲染出的 `service.profile.json` 会把长期 trade-intake / Feishu WS 重启策略标记为 `sudo -n systemctl restart ...`。请给部署用户配置最小 sudoers 授权：

```sudoers
liuxie ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-trade-intake.service
liuxie ALL=(root) NOPASSWD: /usr/bin/systemctl restart options-monitor-trade-intake.service
liuxie ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-feishu-ws.service
liuxie ALL=(root) NOPASSWD: /usr/bin/systemctl restart options-monitor-feishu-ws.service
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

preflight 会检查 env path 是文件还是目录、runtime root / locks / output_accounts / output_shared 权限，以及 runtime config 是否带 `_generated` 元数据。

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
sudo systemctl enable --now options-monitor-feishu-ws.service
```

如果 render 时传了 `--include-feishu-agent-credential`，先确认加密凭据已经存在，再安装 helper 和 drop-in：

```bash
sudo test -f /etc/credstore.encrypted/pm-feishu-agent-app-secret
sudo test -f /etc/credstore.encrypted/om-feishu-holdings-app-secret
sudo install -D -m 0755 \
  /tmp/options-monitor-service/systemd/libexec/options-monitor-materialize-feishu-agent-credential \
  /usr/local/libexec/options-monitor-materialize-feishu-agent-credential
while IFS= read -r source; do
  relative="${source#/tmp/options-monitor-service/systemd/}"
  sudo install -D -m 0644 "$source" "/etc/systemd/system/$relative"
done < <(find /tmp/options-monitor-service/systemd -type f -name zzzz-feishu-agent-credential.conf -print)
sudo systemctl daemon-reload
sudo systemctl enable --now options-monitor-feishu-agent-credential.service
sudo systemctl show --property=Result --value options-monitor-feishu-agent-credential.service
```

最后一条必须输出 `success`。不要在 shell 中解密或回显 credential store 内容。

如果 render 时传了 `--include-auto-upgrade`，再启用升级 timer：

```bash
sudo systemctl enable --now options-monitor-upgrade.timer
```

对于原先把 credential unit 放在 `/usr/lib/systemd/system/` 的存量主机，升级到首个包含此能力的 release 后需要做一次显式收编：

```bash
./om service drift --runtime-root "$RUNTIME"
./om service drift --runtime-root "$RUNTIME" --confirm
./om service drift --runtime-root "$RUNTIME"
```

第一条应显示 `legacy_feishu_agent_credential_inferred`；第二条会把 unit 收编到 `/etc/systemd/system/`、补齐 helper/drop-in 并把显式 opt-in 写回 profile；第三条应返回 clean。首次升级进程是由旧 release 启动的，不能依赖它自己执行新 release 才提供的收编逻辑；这一次收编完成后，后续手动和 06:10 自动升级都会从 profile 保留该意图并自动 reconcile。在仍需要回滚到不识别新 profile 字段的旧 release 期间，保留 `/usr/lib/systemd/system/` 下的 legacy unit；drift 不会自动删除它。

`options-monitor-auto-close-*.timer` 每天北京时间 09:00 运行一次 `./om option-positions auto-close-expired --apply --yes --quiet`。入口会按 runtime config 的 `_generated.market` 过滤 open lots，US/HK timer 只处理各自市场标的；`grace_days=1` 的到期 +1 天 cutoff 按标的市场本地日期计算，US 使用美东时间，HK 使用香港时间。短仓期权还必须有到期后的 OpenD spot 证明已经价外才会自动写入过期平仓；价内/平值或缺少 spot 时会进入 assignment review，等待指派/行权结果。这里使用 `--yes` 是因为 systemd/launchd 属于非交互脚本，高风险写入必须显式确认并输出 `audit_id`。
`options-monitor-projection-verify.timer` 每天北京时间 09:30 运行一次 `./om option-positions verify-projection --mode auto`，用于校验 `trade_events -> position_lots` 并复用 checkpoint。
`options-monitor-tick-us.timer` 使用 `OnCalendar=Mon..Fri *-*-* 09..16:00/10:00 America/New_York`，按美东时间 10 分钟整数边界唤醒。
`options-monitor-tick-hk.timer` 使用 `OnCalendar=Mon..Fri *-*-* 09..16:00/10:00 Asia/Hong_Kong`，按香港时间 10 分钟整数边界唤醒；是否真正扫描/通知仍由 `tick-cron` scheduler 的 run points 决定。
`options-monitor-upgrade.timer` 只有在 render 时传了 `--include-auto-upgrade` 才会生成；它每天北京时间 06:10 检查最新 release tag，发现可升级版本后会从本机 git cache materialize 目标 release、创建 `.venv`、安装 runtime/server 依赖、校验 `om-agent spec` 和 tick 运行解释器，再切换 `/opt/options-monitor/current` 并写入 `upgrade_status.json`。默认 cache root 是 repo symlink 同级的 `_cache/`，也可用 `OM_UPGRADE_CACHE_ROOT` 或 `--cache-root` 覆盖；`_cache/git/options-monitor.git` 首次 mirror clone，后续增量 fetch，依赖下载缓存复用 `_cache/uv` / `_cache/pip`。release 目录不保留 `.git`，所以升级检查和下一次升级会在需要时从 git cache 读取 remote 与 release tags。依赖安装默认 `OM_UPGRADE_INSTALLER=auto`，会优先使用宿主机已安装的 `uv`，并把当前运行中的 Python 3.12+ `sys.executable` 传给 `uv venv --python`；uv 不可用或 auto 模式失败时回退 pip。升级流程不会自动安装 uv；可用 `OM_UPGRADE_INSTALLER=pip|uv` 强制选择。只配置 `PIP_INDEX_URL` 时会自动映射给 uv 的 `UV_INDEX_URL`。

升级后的 release 清理默认 dry-run：

```bash
./om service cleanup \
  --repo-root /opt/options-monitor/current \
  --releases-root /opt/options-monitor/releases \
  --cleanup-downloads \
  --cleanup-pip-cache
```

输出会列出当前 active release、保留 release、待删除旧 release、缓存目录和预计释放空间。默认至少保留 2 个 release（当前版本 + 最近一个回滚版本）；真正删除必须加 `--confirm`。清理边界固定为旧 release 和显式允许的缓存，不会删除 `$RUNTIME`、SQLite、`output_shared` / `output_runs`、locks、runtime config、用户 overlay config、当前 active release 或回滚 release。可选项包括 `--include-apt-cache`、`--journal-vacuum-size 64M`、`--cleanup-downloads`、`--cleanup-pip-cache`。

如果希望升级成功后自动清理旧 release：

```bash
./om update apply \
  --repo-root /opt/options-monitor/current \
  --runtime-root "$RUNTIME" \
  --confirm \
  --cleanup-after-upgrade
```

后置清理只在升级成功、symlink 已切到目标 release、runtime config rebuild/validate 成功、active release 可确认且至少保留 2 个 release 时执行。确认升级时 `--repo-root` 必须是 current symlink 字面路径；如果传成真实 release 目录，会 fail fast，不会先 clone 到错误的 `releases/releases` 结构。

检查：

```bash
./om service status --profile-path "$RUNTIME/service.profile.json" --include-service-status
./om service drift --runtime-root "$RUNTIME"
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
ENV_FILE="$RUNTIME/options-monitor.env"
mkdir -p "$RUNTIME" "$RUNTIME/logs" "$RUNTIME/locks"
test -f "$ENV_FILE" || install -m 600 configs/examples/options-monitor.env.example "$ENV_FILE"
$EDITOR "$ENV_FILE"
```

渲染：

```bash
cd "$REPO"
./om service render \
  --target launchd \
  --repo-root "$REPO" \
  --runtime-root "$RUNTIME" \
  --env-file "$ENV_FILE" \
  --markets us hk \
  --accounts lx sy \
  --config-yaml "$RUNTIME/config.yaml" \
  --config-us "$RUNTIME/config.us.json" \
  --config-hk "$RUNTIME/config.hk.json" \
  --include-feishu-ws \
  --output-dir /tmp/options-monitor-service
```

launchd 不读取 shell profile。渲染器会把 `OM_ENV_FILE=$ENV_FILE` 写入 plist，CLI 启动后再从该 env file 读取 Feishu Bot、holdings 和 inbound audit 配置。

Mac 上同样可以显式开启 Strategy Lab recorder：

```bash
./om service render \
  --target launchd \
  --repo-root "$REPO" \
  --runtime-root "$RUNTIME" \
  --env-file "$ENV_FILE" \
  --markets us hk \
  --accounts lx sy \
  --config-yaml "$RUNTIME/config.yaml" \
  --config-us "$RUNTIME/config.us.json" \
  --config-hk "$RUNTIME/config.hk.json" \
  --include-strategy-lab-recorder \
  --strategy-lab-recorder-source opend \
  --strategy-lab-recorder-account lx \
  --output-dir /tmp/options-monitor-service
```

launchd plist 同样会写入所选账户的显式 OpenD host/port。launchd 没有 systemd 的 `After=` / `Wants=` 关系；使用 `opend` source 时，需要先确认该端点已经稳定可用。

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
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.options-monitor.feishu-ws.plist"
```

如果本次 render 开启了 Strategy Lab recorder，再额外加载：

```bash
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.options-monitor.strategy-lab-build.plist"
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.options-monitor.strategy-lab-sample.plist"
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.options-monitor.strategy-lab-settle.plist"
```

launchd 的日历时间按 Mac 本机时区执行；要等价于北京时间 09:00 / 09:30，Mac 的系统时区需要设为中国标准时间或等价时区。

检查：

```bash
./om service status --profile-path "$RUNTIME/service.profile.json" --include-service-status
./om-agent run --tool runtime_status --input-json "{\"profile_path\":\"$RUNTIME/service.profile.json\"}"
```

## 5. OpenD / Futu 前置条件

`options-monitor` 不托管 OpenD 本身。部署前必须确认：

- Linux 机器能连接可用 OpenD host/port，或本机已运行 OpenD。
- Mac 机器的 OpenD 登录状态稳定，launchd 服务能访问同一端口。
- canonical runtime config 中所选账户的 `account_settings.<account>.futu.host` / `port` 指向正确地址；跨 US/HK render 时两边必须一致。
- OpenD Telnet 已启用，`FutuOpenD.xml` 中应包含 `telnet_ip=127.0.0.1`、`telnet_port=22222`。
- 手机验证码需要通过 Telnet 提交；提交后 `program_status_type=READY`，且 `qot_logined=true`、`trd_logined=true`。

检查 OpenD readiness：

```bash
./om healthcheck --config-key us --accounts lx sy --opend-telnet-host 127.0.0.1 --opend-telnet-port 22222
```

`healthcheck` 会分别输出 `opend_quote_readiness_<endpoint>` 与 `opend_broker_readiness_<account>_<endpoint>`，并把两类 capability 投影回既有 `opend_readiness*` aggregate。账户 primary 只依赖对应 broker readiness；legacy aggregate 则在任一 required capability 失败时 fail closed。Telnet 未监听不会替代 OpenD API readiness，但会明确提示手机验证码无法通过 Telnet 提交。

## 6. 切换旧数据

如果旧 runtime 里已有真实数据，先安排维护窗口并停掉所有可能写 ledger 的
tick / trade-intake / auto-close / inbound Control 进程。源库和目标 runtime 都必须
保持停写，直到完整性检查结束。只停 timer 不够：还要停已经运行的 service 和人工
CLI writer；恢复时只恢复迁移前原本启用或运行的 unit。

仓库使用 SQLite WAL。禁止只 `cp option_positions.sqlite3`：未 checkpoint 的提交可能
仍在 `-wal`，而且裸复制会静默覆盖目标库。下面使用 SQLite backup API 生成一致快照，
并在目标已存在时 fail closed；需要系统已安装 `sqlite3` CLI：

```bash
(
  set -euo pipefail
  umask 077

  OLD_RUNTIME=/path/to/old-runtime
  NEW_RUNTIME=/var/lib/options-monitor
  OLD_DB="$OLD_RUNTIME/output_shared/state/option_positions.sqlite3"
  NEW_STATE_DIR="$NEW_RUNTIME/output_shared/state"
  NEW_DB="$NEW_STATE_DIR/option_positions.sqlite3"
  STAGED_DB="$NEW_STATE_DIR/option_positions.sqlite3.migrating"

  test -f "$OLD_DB"
  mkdir -p "$NEW_STATE_DIR"
  test ! -e "$NEW_DB"
  test ! -e "$STAGED_DB"

  sqlite3 -readonly "$OLD_DB" ".backup '$STAGED_DB'"
  test "$(sqlite3 -readonly "$STAGED_DB" 'PRAGMA integrity_check;')" = "ok"
  mv -n "$STAGED_DB" "$NEW_DB"
  test ! -e "$STAGED_DB"
  test -f "$NEW_DB"
)
```

源库保持不动，作为回退证据。若 `sqlite3` 不可用、目标库已存在、integrity check
不是 `ok` 或移动没有产生目标文件，应停止迁移，不要改用裸复制。

迁移后在恢复任何 writer 前，只用 canonical store 诊断：

```bash
./om option-positions store inspect \
  --config "$NEW_RUNTIME/config.us.json" \
  --runtime-root "$NEW_RUNTIME"

./om option-positions verify-projection \
  --runtime-root "$NEW_RUNTIME" \
  --mode full
```

如果 `store inspect` 报告 active DB 为空但 legacy DB 有数据，或 event / lot 数量与源库
不一致，或 projection verify 不是 `ok`，先处理迁移，不要重建、不要让服务带着双库
并行状态启动。

## 7. 安全边界

- `service render` 只渲染文件，不安装、不启动。
- `runtime_status` / `service status` 是只读诊断。
- `tick --no-send` 会写本地 runtime，但不发通知。
- `auto-close-expired --confirm` 会写持仓账本并可能发回执；上线前先跑 `--dry-run`。服务化非交互运行使用 `--apply --yes`。

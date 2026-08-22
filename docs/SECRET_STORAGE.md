# Secret Storage

options-monitor 通过固定逻辑名读取秘密，业务代码不直接读取秘密环境变量。真实值不得写入
YAML、JSON、JSONL、命令参数、日志、support bundle 或聊天记录。

## 逻辑凭据

| 逻辑名 | 用途 | 旧 env 名（仅兼容） |
|---|---|---|
| `llm.default.api_key` | OpenAI/default LLM | `OM_LLM_API_KEY` |
| `llm.deepseek.api_key` | DeepSeek Assistant provider | `DEEPSEEK_API_KEY` |
| `llm.moonshot.api_key` | Moonshot/Kimi | `MOONSHOT_API_KEY` |
| `llm.kimi.api_key` | Kimi Code | `KIMI_API_KEY` |
| `feishu.holdings.app_secret` | Feishu holdings app | `OM_FEISHU_APP_SECRET` |
| `feishu.bot.app_secret` | Feishu bot / long connection | `OM_FEISHU_BOT_APP_SECRET` |
| `inbound.operation_hmac_key` | inbound 写操作完整性 | `OM_INBOUND_OPERATION_HMAC_KEY` |
| `quality.read_token` | `/quality/status` 读取认证 | `OM_QUALITY_READ_TOKEN` |
| `copilot.cursor_hmac_key` | Copilot 无状态分页游标完整性 | `OM_COPILOT_CURSOR_HMAC_KEY` |

App ID、用户 open ID、table ID、路径、URL、model 名和 feature flag 不是秘密，继续作为普通配置。
Facebook 等后续集成遵循同一规则：App ID 是普通配置；App Secret 只有在出现真实消费方时才加入固定注册表。
本项目不消费 OpenAI 账户密码，也不会为它建立存储入口；只使用对应逻辑名下的 API key。

## 后端选择

默认 `OM_SECRET_BACKEND=auto`：

- macOS 使用 Login Keychain，service 固定为 `options-monitor`，account 为逻辑名；
- Linux systemd unit 显式选择逐 unit 凭据交付模式，应用只读取
  `$CREDENTIALS_DIRECTORY/<credential-id>`；普通主机默认使用 `LoadCredentialEncrypted=`；
- Linux 普通 shell 若没有 `CREDENTIALS_DIRECTORY` 会明确失败，不会回退到 env；
- `OM_SECRET_BACKEND=env` 只用于 CI、测试和限时迁移。

进程只在第一次请求某个逻辑名时读取，并缓存该值。存储端轮换不会热更新运行进程；必须通过独立、可审计的服务重启让新值生效。CLI 不会预加载全部凭据。

## 运维 CLI

这些命令永远不提供 `get`、`show` 或 `export`，也不输出值或 hash：

```bash
./om secrets status
./om secrets set llm.deepseek.api_key
./om secrets rotate llm.deepseek.api_key
./om secrets delete llm.deepseek.api_key --confirm
```

`set` / `rotate` 只通过可见终端中的隐藏输入读取秘密，两次输入必须一致。命令结束后只报告
逻辑名、后端、是否改变和可能受影响的服务；不会自动重启服务。
非交互 stdin、命令参数和管道输入都会被拒绝。

启用 Copilot keyset 分页前，必须通过一次性 root bootstrap 单独 provision
`copilot.cursor_hmac_key`，例如 `sudo ./om secrets set copilot.cursor_hmac_key`。setup、升级和发布
不会生成、读取或轮换它；同一环境跨 release 保持同一值。显式轮换会让尚未过期的旧游标
立即失效，用户需要重新发起查询。

macOS provider 会在私有控制终端中等待 `security` 的两次原生密码提示，再把已确认值从
进程内存写入；值不会进入子进程 argv、stdout、stderr 或临时文件。写入成功仍需用脱敏
`status` 和实际消费方回读验证，不能只信 `security` 的退出码。

macOS 命令写入 Keychain。Linux 写入 `/etc/credstore.encrypted/<credential-id>`，必须用单独的
root 授权运行，例如先确认目标，再执行 `sudo ./om secrets set <logical-name>`。不要把秘密放在
命令参数、shell 变量或管道中。

## Linux 逐 unit 最小权限注入

渲染服务时使用：

```bash
./om service render \
  --target systemd \
  --include-secret-credentials \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --output-dir /tmp/options-monitor-service
```

默认 `--secret-credential-delivery load-credential-encrypted`，为各消费 unit 生成
`LoadCredentialEncrypted=` 绑定。每个 drop-in 只包含该 unit 固定注册表中需要的
credential ID。它不会创建、读取或修改 `/etc/credstore.encrypted` 中的文件，也不会安装 drop-in。
启用 Copilot 时，cursor credential 只绑定给实际运行 Copilot 的 inbound service；升级 unit
不会读取该密钥。
两种安全交付 drop-in 都会用 `UnsetEnvironment=` 从进程环境中移除固定注册表里的旧 secret env 名；
普通 env-file 仍可保留非秘密配置。
安装、daemon-reload、服务重启和健康验证仍属于独立的部署授权边界。

如果 Incus/LXC 宿主禁止 systemd credential 所需的 mount namespace，可显式选择：

```bash
./om service render \
  --target systemd \
  --include-secret-credentials \
  --secret-credential-delivery runtime-files \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --output-dir /tmp/options-monitor-service
```

`runtime-files` 不会回退到 env。它安装一个 `0755 root:root` 的自包含 helper，在 unit
启动前以 root 解密该 unit 的最小凭据集合，原子发布到
`/run/options-monitor/credentials/<unit>/`。helper 要求 `/run` 由 tmpfs 承载，拒绝来源路径祖先中的
符号链接或可替换目录，以及非 root 加密源、过宽权限和异常大小；明文文件为 `0400 <deploy_user>:root`，目录为
`0510 root:<deploy_user-primary-group>`，
unit 停止后清理。值不进入 argv、stdout、stderr 或服务环境变量。

这是受限容器的显式兼容模式，不会自动从原生模式降级。它的隔离强度低于 systemd
私有、只读 credential mount：当多个服务共用同一 Unix 用户时，同 UID 进程之间不具备强文件隔离。
但它仍保持持久层加密、逐 unit 最小映射、tmpfs 明文生命周期以及不使用 secret env。

旧 `--include-feishu-agent-credential` 会生成共享 Feishu env materializer，仅为存量迁移保留；
它与 `--include-secret-credentials` 不能同时启用。

### 存量 Linux 服务迁移

先升级到包含目标交付模式的已发布版本，再从当前 release 使用稳定 repo 链接执行 dry-run。
受限 Incus/LXC 使用：

```bash
./om service credentials-migrate \
  --repo-root /opt/options-monitor/current \
  --runtime-root /var/lib/options-monitor \
  --secret-credential-delivery runtime-files
```

输出会列出固定 credential ID、将重启的长运行消费者、不会被主动触发的 oneshot 消费者以及
目标 drift，但不解密、不写 profile、不重启服务。如果存在与凭据迁移无关的 service drift，命令会先阻断。

确认 dry-run 后再显式执行：

```bash
./om service credentials-migrate \
  --repo-root /opt/options-monitor/current \
  --runtime-root /var/lib/options-monitor \
  --secret-credential-delivery runtime-files \
  --confirm --yes
```

确认模式会在任何 service/profile 写入前，把每个固定加密凭据解密到 `/dev/null` 做失败闭合验证；
stdout/stderr 均不接收明文。随后备份 `service.profile.json`、安装新 helper/drop-in、移除兼容 drop-in，
仅重启迁移前处于 active 的长运行凭据消费者。任一重启失败时尝试恢复旧 profile/unit 并重启原 active 集合。
只有新路径重启和回读 drift 都成功后，才删除旧 helper 和 tmpfs 共享 secret env 文件。
加密 store 本身不会被修改或删除。

oneshot/timer 服务不会被迁移命令主动运行，以避免触发通知、账本或其他业务写入；它们在下一次自然调度时使用新路径。
该命令只负责从存量共享 secret env 迁移到所选安全模式；如果 profile 已启用一种安全的逐 unit
delivery，再请求切换到另一种会明确阻断，避免在没有专用旧 runtime 清理合同时遗留明文。

## env 兼容与风险

环境变量比把密钥写进仓库安全，但不是秘密存储：它们会继承给子进程，可能进入进程诊断、
崩溃信息、错误的 debug 日志、容器/服务配置或运维采集。env-file 还会把明文长期留在磁盘。

因此普通 env-file 只保留非秘密配置。只有显式选择 `OM_SECRET_BACKEND=env` 时，CLI bootstrap
才会把 secret-looking 名称从 env-file 注入进程。安全后端缺失或读取失败时会失败关闭，不会尝试旧 env 名。

旧配置中的 `api_key_env` / `app_secret_env` 只描述 env 兼容名；安全后端始终按 provider 对应的
固定逻辑名读取，不能通过配置把任意 Keychain 或 systemd credential 映射给业务代码。新配置可省略这些旧字段。

迁移顺序：

1. 在供应商侧创建新 key；已暴露或疑似暴露的 key 必须直接轮换，不能只搬家。
2. 用隐藏输入把新值 provision 到 Keychain 或 systemd encrypted store。
3. dry-run 检查逻辑名和逐 unit 映射，不显示值。
4. 在单独授权下重启受影响服务并验证真实调用。
5. 撤销旧 key，确认回读成功后再从 env-file 删除旧行。

源码测试使用注入的 in-memory provider；存量 env 行为测试显式选择 compatibility backend，测试不得访问真实 Keychain 或系统凭据。

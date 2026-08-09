# Secret Storage

options-monitor 通过固定逻辑名读取秘密，业务代码不直接读取秘密环境变量。真实值不得写入
YAML、JSON、JSONL、命令参数、日志、support bundle 或聊天记录。

## 逻辑凭据

| 逻辑名 | 用途 | 旧 env 名（仅兼容） |
|---|---|---|
| `llm.default.api_key` | OpenAI/default LLM | `OM_LLM_API_KEY` |
| `llm.deepseek.api_key` | DeepSeek / AI Decision Advice | `DEEPSEEK_API_KEY` |
| `llm.moonshot.api_key` | Moonshot/Kimi | `MOONSHOT_API_KEY` |
| `llm.kimi.api_key` | Kimi Code | `KIMI_API_KEY` |
| `feishu.holdings.app_secret` | Feishu holdings app | `OM_FEISHU_APP_SECRET` |
| `feishu.bot.app_secret` | Feishu bot / long connection | `OM_FEISHU_BOT_APP_SECRET` |
| `inbound.operation_hmac_key` | inbound 写操作完整性 | `OM_INBOUND_OPERATION_HMAC_KEY` |
| `quality.read_token` | `/quality/status` 读取认证 | `OM_QUALITY_READ_TOKEN` |

App ID、用户 open ID、table ID、路径、URL、model 名和 feature flag 不是秘密，继续作为普通配置。
Facebook 等后续集成遵循同一规则：App ID 是普通配置；App Secret 只有在出现真实消费方时才加入固定注册表。
本项目不消费 OpenAI 账户密码，也不会为它建立存储入口；只使用对应逻辑名下的 API key。

## 后端选择

默认 `OM_SECRET_BACKEND=auto`：

- macOS 使用 Login Keychain，service 固定为 `options-monitor`，account 为逻辑名；
- Linux systemd unit 使用 `LoadCredentialEncrypted=`，应用只读取 systemd 提供的
  `$CREDENTIALS_DIRECTORY/<credential-id>`；
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

macOS 命令写入 Keychain。Linux 写入 `/etc/credstore.encrypted/<credential-id>`，必须用单独的
root 授权运行，例如先确认目标，再执行 `sudo ./om secrets set <logical-name>`。不要把秘密放在
命令参数、shell 变量或管道中。

## systemd 最小权限注入

渲染服务时使用：

```bash
./om service render \
  --target systemd \
  --include-secret-credentials \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --output-dir /tmp/options-monitor-service
```

渲染结果为各消费 unit 生成 `zzzz-secret-credentials.conf`，每一行只绑定该 unit 所需的固定
credential ID。它不会创建、读取或修改 `/etc/credstore.encrypted` 中的文件，也不会安装 drop-in。
安装、daemon-reload、服务重启和健康验证仍属于独立的部署授权边界。

旧 `--include-feishu-agent-credential` 会生成共享 Feishu env materializer，仅为存量迁移保留；
它与 `--include-secret-credentials` 不能同时启用。完成真实凭据轮换、逐 unit dry-run、升级和回读验证后，再单独删除旧 helper、oneshot、drop-in 和共享 env 文件。

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

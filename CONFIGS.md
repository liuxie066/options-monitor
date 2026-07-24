# options-monitor 配置契约

本文只定义配置事实源、生成链路和迁移边界。字段与操作示例见 [Configuration Guide](CONFIGURATION_GUIDE.md)。

## 权威模型

```text
src/application/config_defaults.py::DEFAULT_CONFIG
  + config.yaml
  -> om config build / build-assistant
  -> generated runtime snapshots

env-file
  -> runtime secrets, machine settings, and write gates
```

env-file 不是合并进生成快照的配置层；它在进程启动或工具执行时装入有效环境，
生成 JSON 不复制 secrets。

| 层 | 用途 | 是否人工编辑 |
|---|---|---:|
| `DEFAULT_CONFIG` | 系统默认值 | 否 |
| `config.yaml` | accounts、markets、symbols、非 secret override | 是 |
| env-file | secrets、本机设置、写入开关 | 是，且不提交 |
| `config.us.json` / `config.hk.json` | 市场运行快照 | 否，由 build 生成 |
| `resolved/config.assistant.json` | Assistant 运行快照 | 否，由 build-assistant 生成 |
| `portfolio.runtime.json` | 少数 external holdings env 名兼容 | 通常不需要 |

生成后的 runtime JSON 是“本次运行读取的快照”，不是另一套 authoring source。不要一边编辑 `config.yaml`，一边手改 JSON。

## `config.yaml`

稳定约定：

- 顶层 `accounts` 定义账户及类型；
- `markets.us` / `markets.hk` 选择该市场的账户和 symbols；
- symbol 使用规范代码，例如 `NVDA`、`0700.HK`、`9992.HK`；
- per-symbol 策略 override 放在 `markets.<market>.overrides.<symbol>`；
- YAML 中使用 `covered_call`，生成的内部 runtime / CSV / trace key 仍可能是 `sell_call`；
- `combo_yield` 是当前开仓策略 key；旧 `yield_enhancement` 只在明确兼容边界读取；
- secrets、token、Feishu credential 和 Agent write gate 不进入 YAML。

当前示例：

- `configs/examples/config.yaml.example`
- `src/application/config_defaults.py`

## 生成与验证

```bash
./om config validate --source yaml \
  --market us \
  --config-yaml config.yaml

./om config build --source yaml \
  --market us \
  --config-yaml config.yaml \
  --output config.us.json

./om config build-assistant --source yaml \
  --config-yaml config.yaml \
  --output resolved/config.assistant.json

./om config validate \
  --config-path config.us.json \
  --market us
```

`config build` 会写 `_generated` 来源与指纹。`config.yaml` 变化后必须重新生成对应市场快照；`tick` / `tick-cron` 会拒绝过期快照，除非操作者显式使用应急 override。

不确定某个最终值来自哪里时：

```bash
./om config explain --source yaml \
  --market us \
  --key <dot.path>
```

## Runtime lookup

公开入口通常按以下优先级解析运行快照：

1. 显式 `config_path` / `--config`；
2. 显式 `config_key=us|hk` 对应的 runtime config；
3. 入口定义的 repo-local fallback。

生产 release 目录不应依赖第 3 项。服务和诊断命令应传 `/var/lib/options-monitor/config.us.json` 等真实持久路径。

## Secrets 与本机设置

env-file 保存：

- Feishu App / webhook 等凭证；
- LLM provider API key；
- external holdings 表引用；
- Tool Gateway 写入开关；
- 其他不应进入 Git 的机器级设置。

Linux 推荐：

```text
/etc/options-monitor/options-monitor.env
```

macOS 推荐：

```text
$HOME/Library/Application Support/options-monitor/options-monitor.env
```

只读检查：

```bash
./om settings doctor
./om settings inspect
```

不要提交 env-file，不要在 issue、日志或聊天中粘贴 secret。

## 旧配置迁移

旧 layered JSON 不再支持 normal build / explain。唯一正式迁移入口是：

```bash
./om config migrate-yaml --output config.yaml
```

默认只预览。确认等价性和目标路径后才显式 `--apply`：

```bash
./om config migrate-yaml --output config.yaml --apply
```

迁移完成后：

1. 只维护 `config.yaml`；
2. 对 US/HK 分别 validate；
3. 重新 build runtime JSON；
4. 重新 build assistant JSON；
5. 让 service profile 指向 YAML 与生成快照的持久路径。

`scripts/migrate_runtime_config.py` 只服务特定历史 schedule 迁移，不是通用“升级到最新 schema”入口。

## 数据配置边界

期权账本不需要 Feishu table config：

```text
<runtime_root>/output_shared/state/option_positions.sqlite3
```

`portfolio.runtime.json` 只在 external holdings 需要替代 env 名等兼容场景使用。它不能重新引入 Feishu `option_positions` bootstrap 或镜像。

## 禁止项

- 禁止把 `config.json`、`config.scheduled.json`、`config.market_*.json` 当作正式运行入口。
- 禁止提交 `config.yaml` 的真实生产副本、生成的用户 runtime JSON、env-file 或备份。
- 禁止用手改生成 JSON 代替 `config build`。
- 禁止在生产 release 目录内保存唯一配置副本。
- 禁止通过 legacy JSON、Feishu 表或自动 fallback 绕过当前验证器。

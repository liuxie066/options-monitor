# Install

这份文档只回答一个问题：怎么把 `options-monitor` 安装到机器上。

安装不会创建 runtime config，不会写 env secrets，不会启动 systemd/launchd，也不会连接 OpenD、Feishu 或修改 SQLite 状态。默认会创建用户级 `om` / `om-agent` wrapper，方便从任意目录启动。

整个仓库要求 **Python 3.12 或更高版本**。`om`、`om-agent`、installer、release preflight 和 service upgrade 都会在执行前验证解释器，不再静默接受 macOS 自带的旧 `python3`。

## Quick Install

普通安装默认解析并安装最新 GitHub release，不安装浮动 `main` 分支：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash

"$HOME/.local/bin/om" setup init
```

安装器始终安装已发布版本，不安装 `main` 上尚未发布的改动；新 CLI 参数以所安装版本的 `om setup init --help` 为准。

安装输出会明确打印解析到的 release tag，例如：

```text
[install] resolved latest release: v1.2.118
```

需要复现、回滚或固定生产版本时，使用可审计的两步安装并显式指定 release tag：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o /tmp/options-monitor-install.sh
bash /tmp/options-monitor-install.sh --version v1.2.118 --prefix "$HOME/apps/options-monitor"
```

安装脚本默认把 wrapper 写到 `$HOME/.local/bin`。如果该目录不在 `PATH`，安装输出会提示：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

如果不想创建全局命令，加 `--no-install-cli`；此时使用 fallback：

```bash
"$HOME/apps/options-monitor/current/om" setup check
```

如果需要换 wrapper 目录，用 `--bin-dir PATH`。如果目标目录里已有未知来源的 `om` / `om-agent`，installer 会拒绝覆盖；确认要接管时才使用 `--force-cli-wrapper`。

如果需要 Feishu long-connection、远端 inbound 或服务端依赖，安装时加 `--with-server`：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash -s -- --with-server
```

## 可选 PM 与长桥依赖

使用**运行该 OM 实例的 Python 环境**安装；以下从源码根目录示例，不会开启配置、
写入凭据或连接外部服务。PM 包随源码提供，未发布到插件市场。

```bash
./.venv/bin/python -m pip install ./plugins/portfolio-management
./.venv/bin/python -m pip uninstall om-pm-plugin
```

PM 默认关闭。安装后设置全局 `portfolio_management.enabled: true`；卸载前关闭 PM
及 `portfolio.holdings.enabled`。未安装时 `om help`、`om-agent spec` 与非 PM 功能可正常
加载；启用但缺包的 PM 请求返回 `PORTFOLIO_MANAGEMENT_UNAVAILABLE`。详情见
[配置契约](../CONFIGS.md#可选能力与长桥账户)。

长桥资产适配使用独立 SDK 依赖，默认运行依赖不安装它：

```bash
./.venv/bin/python -m pip install -r requirements/longbridge.txt
```

富途/OpenD 行情仍使用既有依赖。长桥 SDK 不在模块导入或 provider 选择时自动载入；
仅真实读取长桥资产时需要。SDK 安装不完成账号凭据绑定，也不证明连接和数据权限可用。

开发与 CI 的 `requirements/dev.txt` 显式安装 PM 扩展，保留全部 PM 自动发现回归；
缺包状态由隔离子进程验证，不跳过这些测试。

`install.sh --with-dev` 始终从下载的 release 根目录安装这些依赖，不使用调用目录中的
同名插件。正常 service upgrade 保留当前实例实际安装的 PM 扩展和 Longbridge SDK，
与配置是否启用无关；未安装或主动卸载的包不会因配置仍启用而补装。首次从内置 PM 的
旧版迁移时，仅为有效旧配置已启用的 PM 安装扩展。缓存及切换前校验见
[升级流程](RELEASE_PROCESS.md)；首次迁移必须使用含修复的新控制器，旧版自动升级器不能
自行获得目标 release 中的修复。

## 可选持仓建议

运行依赖默认不安装持仓建议包。使用该实例的 Python 从源码根目录安装：

```bash
./.venv/bin/python -m pip install ./plugins/position-advice
./.venv/bin/python -m pip uninstall om-position-advice-plugin
```

安装器也支持 `--with-position-advice`；安装不启用，需在配置中明确开启 `close_advice.enabled`。缺包不影响核心扫描启动及历史报告读取。开发/CI 的 `requirements/dev.txt` 安装此包并保留全部策略测试；缺包启动由隔离子进程覆盖。受控升级保留实际安装选择，并为旧内置建议已启用的实例迁移安装；主动卸载保持卸载。首次迁移仍须使用新升级控制器，见[升级流程](RELEASE_PROCESS.md)。

## macOS

macOS 是一等支持平台，适合本地手动运行或轻量常驻运行。长期无人值守仍优先推荐 Linux。

前置依赖：

```bash
xcode-select --install
python3.12 --version
```

Python 需要 3.12 或更高版本。

如果使用 Homebrew：

```bash
brew install python@3.12 git
```

安装代码：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash

om setup check
```

如果这台 Mac 要跑 Feishu long-connection inbound：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash -s -- --with-server
```

本地手动运行可以继续使用 repo 内忽略文件：

```text
.env/options-monitor.env
```

完成 `om setup init` 后，推荐从 `om` 日常菜单管理配置和服务。普通设置默认位于所选运行目录的 `options-monitor.env`；通知、Bot 和秘密存储分别配置。`om service install`、`om service start` 和 `om service stop` 均先预览，确认后才执行。详见[首次运行指南](GETTING_STARTED.md#6-可选长期运行服务)。下面的 `service render` 保留给需要单独检查或导出服务文件的高级使用者。

如果要渲染 launchd 服务，推荐把 env-file 放在 Mac 的 Application Support：

```bash
mkdir -p "$HOME/Library/Application Support/options-monitor"
cp -n configs/examples/options-monitor.env.example "$HOME/Library/Application Support/options-monitor/options-monitor.env"
om settings doctor --env-file "$HOME/Library/Application Support/options-monitor/options-monitor.env"
```

macOS 服务化使用：

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
  --output-dir /tmp/options-monitor-service
```

如果需要飞书长连接，额外加 `--include-feishu-ws`。launchd 不读取 shell profile，渲染器会把 env-file 通过 `OM_ENV_FILE` 写入 plist。

## Linux

Linux 是推荐的生产长期运行平台。

前置依赖以 Debian/Ubuntu 为例：

```bash
sudo apt-get update
sudo apt-get install -y git python3.12 python3.12-venv
```

Python 需要 3.12 或更高版本；较旧发行版请先启用提供 Python 3.12 的受信软件源或升级发行版。

安装代码：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash

om setup check
```

如果这台服务器要跑 Feishu long-connection inbound：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash -s -- --with-server
```

生产服务 env-file 推荐放在：

```text
/etc/options-monitor/options-monitor.env
```

初始化模板：

```bash
sudo install -d -m 700 /etc/options-monitor
sudo test -f /etc/options-monitor/options-monitor.env || sudo install -m 600 configs/examples/options-monitor.env.example /etc/options-monitor/options-monitor.env
om settings doctor --env-file /etc/options-monitor/options-monitor.env
```

生产 runtime root 推荐放在：

```text
/var/lib/options-monitor
```

systemd 服务化使用：

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
  --output-dir /tmp/options-monitor-service
```

如果需要飞书长连接，额外加 `--include-feishu-ws`。

## Manual Install

```bash
git clone https://github.com/liuxie066/options-monitor.git options-monitor
cd options-monitor
git checkout <release-tag>
python3.12 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt -c constraints.txt
```

如果已有 `.venv` 是 Python 3.10/3.11 或已损坏，launcher 会明确失败而不会绕到 shell 的其他 `python3`。确认该目录只包含可重建依赖后再重建：

```bash
rm -rf .venv
python3.12 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt -c constraints.txt
```

临时诊断或恢复时可以显式指定兼容解释器：

```bash
OM_PYTHON=/absolute/path/to/python3.12 ./om --help
```

可选依赖：

```bash
./.venv/bin/pip install -r requirements/server.txt -c constraints/server.txt
./.venv/bin/pip install -r requirements/dev.txt -c constraints/dev.txt
```

## Layout

`scripts/install.sh` 使用 release 目录布局：

```text
$HOME/apps/options-monitor/
  current -> releases/<resolved-release-tag>
  releases/
    <resolved-release-tag>/
      .venv/
      om
      om-agent

$HOME/.local/bin/
  om        -> wrapper execs $HOME/apps/options-monitor/current/om
  om-agent  -> wrapper execs $HOME/apps/options-monitor/current/om-agent
```

升级时安装新 tag，再切换 `current` symlink。长期运行服务应使用 `current` 作为 repo root。
用户级 wrapper 指向 `current`，所以升级后 `om` / `om-agent` 会自动跟随新的 release。

在 `current` 实际切换前发生可恢复失败或收到 INT/TERM，安装器会恢复原有 wrapper，原本没有入口则移除本次新建入口；切换完成后保留新入口。恢复或清理失败会非零退出，并报告尚存备份的路径。已发布的失败 release 保留，普通重试仍拒绝已有 inactive target；确认后可沿用 `--force` 重建，活动 release 仍拒绝 `--force`。

新发布路径若发现初始 `current` 的链接文本已精确等于本次目标 release 路径（包括悬空链接），或无法读取初始链接，会在事务备份和发布前拒绝，保留原链接、入口与已有备份；已安装快路径保持原行为。上述恢复不保证覆盖 SIGKILL、断电或其他安装器/操作者并发修改现场。

## Safety Contract

installer 允许做：

- clone repo
- checkout 最新或指定的 GitHub release tag
- 创建 `.venv`
- 安装 Python requirements
- 更新 `current` symlink
- 创建或更新带 marker 的用户级 `om` / `om-agent` wrapper（除非传 `--no-install-cli`）
- 输出下一步命令

installer 禁止做：

- 覆盖未知来源的同名 `om` / `om-agent` 命令（除非显式传 `--force-cli-wrapper`）
- 写 `config.yaml`、`config.us.json` 或 `config.hk.json`
- 写真实 env-file 或 secrets
- 创建或启用 systemd/launchd timer
- 启动长期服务
- 连接 OpenD 或 Feishu
- 修改 `option_positions.sqlite3` 或任何交易/持仓状态

安装完成后的下一步是：

```bash
om setup check
```

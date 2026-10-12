# Install

这份文档只回答一个问题：怎么把 `options-monitor` 安装到机器上。

安装阶段不会创建 runtime config，不会写 env secrets，不会启动 systemd/launchd，也不会连接 OpenD、Feishu 或修改 SQLite 状态。默认会创建用户级 `om` / `om-agent` wrapper，方便从任意目录启动。显式传入 `--setup` 后，安装成功才进入已有首次配置向导，其配置写入和服务操作仍逐项预览确认。

整个仓库要求 **Python 3.12 或更高版本**。`om`、`om-agent`、installer、release preflight 和 service upgrade 都会在执行前验证解释器，不再静默接受 macOS 自带的旧 `python3`。

## Quick Install

普通安装默认解析并安装最新 GitHub release，不安装浮动 `main` 分支：

```bash
om_installer="$(mktemp -t options-monitor-install.XXXXXX)" &&
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o "$om_installer" &&
bash "$om_installer" --setup
```

先下载脚本再执行可保留终端输入。不要把需要交互的安装命令通过管道传给 Bash。`--setup` 要求标准输入和标准输出均为终端；否则安装仍可成功，但首次配置未执行，输出会提供准确的绝对路径命令。省略 `--setup` 保持只安装行为。安装器始终安装已发布版本，不安装 `main` 上尚未发布的改动；新 CLI 参数以所安装版本的 `om setup init --help` 为准。

安装输出会明确打印解析到的 release tag，例如：

```text
[install] resolved latest release: v1.2.118
```

需要复现、回滚或固定生产版本时，使用可审计的两步安装并显式指定 release tag：

```bash
om_installer="$(mktemp -t options-monitor-install.XXXXXX)" &&
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o "$om_installer" &&
bash "$om_installer" --version v1.2.118 --prefix "$HOME/apps/options-monitor" --setup
```

安装脚本默认把 wrapper 写到 `$HOME/.local/bin`。安装输出和 `--setup` 直接使用绝对路径；如果要在新终端直接输入 `om`，把该目录加入 shell profile：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

如果不想创建全局命令，加 `--no-install-cli`；此时使用 fallback：

```bash
"$HOME/apps/options-monitor/current/om" setup init
```

如果需要换 wrapper 目录，用 `--bin-dir PATH`。如果目标目录里已有未知来源的 `om` / `om-agent`，installer 会拒绝覆盖；确认要接管时才使用 `--force-cli-wrapper`。

如果需要 Feishu long-connection、远端 inbound 或服务端依赖，安装时加 `--with-server`：

```bash
om_installer="$(mktemp -t options-monitor-install.XXXXXX)" &&
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o "$om_installer" &&
bash "$om_installer" --with-server --setup
```

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
om_installer="$(mktemp -t options-monitor-install.XXXXXX)" &&
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o "$om_installer" &&
bash "$om_installer" --setup
```

如果这台 Mac 要跑 Feishu long-connection inbound：

```bash
bash "$om_installer" --with-server
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
om_installer="$(mktemp -t options-monitor-install.XXXXXX)" &&
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o "$om_installer" &&
bash "$om_installer" --setup
```

如果这台服务器要跑 Feishu long-connection inbound：

```bash
bash "$om_installer" --with-server
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

虚拟环境直接在最终 release 路径创建，避免移动后 `pip` 和 `activate` 仍引用临时目录。每个安装根目录只允许一个安装事务；在切换 `current` 前失败或收到 INT、TERM、HUP，会恢复原有 wrapper。普通失败的完整 release 留存在报出的事务目录，版本路径腾出后可原命令重试；显式替换的未启用版本也会还原。失败目录不自动删除，会占用磁盘，请先核对再决定回收。切换成功后取消首次配置不会撤销安装。

若准备完成后的 release 内容与快照不同、无法核实所有权或恢复失败，安装器原位保留目标、备份和锁并报错，拒绝盲重试；请先检查报出的事务目录。目录标记不代表其中所有文件都可删除，失败版本内容会完整留存，包括准备阶段加入的内容。不要盲删锁或未知目录。SIGKILL、掉电或跨安装根目录并发共用 wrapper 目录尚无自动恢复保证。

## Safety Contract

installer 允许做：

- clone repo
- checkout 最新或指定的 GitHub release tag
- 创建 `.venv`
- 安装 Python requirements
- 更新 `current` symlink
- 创建或更新带 marker 的用户级 `om` / `om-agent` wrapper（除非传 `--no-install-cli`）
- 输出下一步命令
- 显式 `--setup` 时，在提交安装并清理事务后调用已有 `om setup init` 向导

安装阶段禁止做（向导中的显式确认遵循其自身权限边界）：

- 覆盖未知来源的同名 `om` / `om-agent` 命令（除非显式传 `--force-cli-wrapper`）
- 写 `config.yaml`、`config.us.json` 或 `config.hk.json`
- 写真实 env-file 或 secrets
- 创建或启用 systemd/launchd timer
- 启动长期服务
- 连接 OpenD 或 Feishu
- 修改 `option_positions.sqlite3` 或任何交易/持仓状态

安装完成后的下一步是：

```bash
"$HOME/.local/bin/om" setup init
```

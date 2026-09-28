# Install

这份文档只回答一个问题：怎么把 `options-monitor` 安装到机器上。

安装不会创建 runtime config，不会写 env secrets，不会启动 systemd/launchd，也不会连接 OpenD、Feishu 或修改 SQLite 状态。默认会创建用户级 `om` / `om-agent` wrapper，方便从任意目录启动。

整个仓库要求 **Python 3.12 或更高版本**。`om`、`om-agent`、installer、release preflight 和 service upgrade 都会在执行前验证解释器，不再静默接受 macOS 自带的旧 `python3`。

## Quick Install

普通安装默认解析并安装最新 GitHub release，不安装浮动 `main` 分支：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
om setup check
om setup init
```

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
export PATH="$HOME/.local/bin:$PATH"
export OM_RUNTIME_ROOT="$HOME/Library/Application Support/options-monitor"
om setup check
om setup init
```

如果这台 Mac 要跑 Feishu long-connection inbound：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash -s -- --with-server
```

本地手动运行可以继续使用 repo 内忽略文件：

```text
.env/options-monitor.env
```

如果要渲染 launchd 服务，推荐把 env-file 放在 Mac 的 Application Support：

```bash
REPO="$HOME/apps/options-monitor/current"
RUNTIME="$HOME/Library/Application Support/options-monitor"
mkdir -p "$RUNTIME"
test -f "$RUNTIME/options-monitor.env" || install -m 600 /dev/null "$RUNTIME/options-monitor.env"
om config edit --runtime-root "$RUNTIME" --env-file "$RUNTIME/options-monitor.env"
om settings doctor --env-file "$RUNTIME/options-monitor.env"
```

不要把示例 env-file 中的 Linux 绝对路径原样复制到 Mac；只填写实际需要的普通设置，密钥用 `om secrets set`。

macOS 服务化使用：

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

这里的 `REPO` 是 installer 默认目录；使用 `--prefix` 时改为实际安装输出的 `current` 路径。
`om setup init` 会询问运行目录、市场、Futu 账户及初始标的；完成后保存它返回的带路径的检查与编辑命令。首次生成 YAML 和运行快照的步骤见 [Getting Started](GETTING_STARTED.md)，不要在 release 目录里维护配置。
如果需要飞书长连接，额外加 `--include-feishu-ws --feishu-ws-config-key us`；使用港股配置时将 `us` 改为 `hk`。launchd 不读取 shell profile，渲染器会把 env-file 通过 `OM_ENV_FILE` 写入 plist。

## Linux

Linux 是推荐的生产长期运行平台。

前置依赖以 Debian/Ubuntu 为例：

```bash
sudo apt-get update
sudo apt-get install -y curl git python3.12 python3.12-venv
```

Python 需要 3.12 或更高版本；较旧发行版请先启用提供 Python 3.12 的受信软件源或升级发行版。

安装代码：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
om setup check
om setup init
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
sudo test -f /etc/options-monitor/options-monitor.env || sudo install -m 600 "$HOME/apps/options-monitor/current/configs/examples/options-monitor.env.example" /etc/options-monitor/options-monitor.env
sudo "$HOME/apps/options-monitor/current/om" settings doctor --env-file /etc/options-monitor/options-monitor.env
```

生产 runtime root 推荐放在：

```text
/var/lib/options-monitor
```

首次可在 `om setup init` 中选择当前用户可写目录手动运行。要直接使用上述 systemd 目录，请先由管理员创建目录并授予实际部署用户写权限，再以该部署用户运行向导；向导不调用 `sudo`，也不安装服务。它会返回之后 `setup check`、`config edit` 所需的精确路径。[首次使用指南](GETTING_STARTED.md)

systemd 服务化使用：

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

如果需要飞书长连接，额外加 `--include-feishu-ws --feishu-ws-config-key us`；使用港股配置时将 `us` 改为 `hk`。

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

安装完成后先检查，再在交互终端配置：

```bash
om setup check
om setup init
```

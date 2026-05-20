# Install

这份文档只回答一个问题：怎么把 `options-monitor` 安装到机器上。

安装不会创建 runtime config，不会写 env secrets，不会启动 systemd/launchd，也不会连接 OpenD、Feishu 或修改 SQLite 状态。

## Quick Install

默认使用可审计的两步安装，不用 `curl | bash`：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o /tmp/options-monitor-install.sh
bash /tmp/options-monitor-install.sh --version v1.2.92 --prefix "$HOME/apps/options-monitor"

cd "$HOME/apps/options-monitor/current"
./om setup check
```

`--version` 必须是明确 release tag。不要在生产机器上安装浮动分支。

如果需要 Feishu long-connection、远端 inbound 或服务端依赖，安装时加 `--with-server`：

```bash
bash /tmp/options-monitor-install.sh --version v1.2.92 --prefix "$HOME/apps/options-monitor" --with-server
```

## macOS

macOS 是一等支持平台，适合本地手动运行或轻量常驻运行。长期无人值守仍优先推荐 Linux。

前置依赖：

```bash
xcode-select --install
python3 --version
```

Python 需要 3.10 或更高版本。

如果使用 Homebrew：

```bash
brew install python git
```

安装代码：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o /tmp/options-monitor-install.sh
bash /tmp/options-monitor-install.sh --version v1.2.92 --prefix "$HOME/apps/options-monitor"

cd "$HOME/apps/options-monitor/current"
./om setup check
```

如果这台 Mac 要跑 Feishu long-connection inbound：

```bash
bash /tmp/options-monitor-install.sh --version v1.2.92 --prefix "$HOME/apps/options-monitor" --with-server
```

本地手动运行可以继续使用 repo 内忽略文件：

```text
.env/options-monitor.env
```

如果要渲染 launchd 服务，推荐把 env-file 放在 Mac 的 Application Support：

```bash
mkdir -p "$HOME/Library/Application Support/options-monitor"
cp -n configs/examples/options-monitor.env.example "$HOME/Library/Application Support/options-monitor/options-monitor.env"
./om settings doctor --env-file "$HOME/Library/Application Support/options-monitor/options-monitor.env"
```

macOS 服务化使用：

```bash
./om service render \
  --target launchd \
  --runtime-root "$HOME/Library/Application Support/options-monitor" \
  --env-file "$HOME/Library/Application Support/options-monitor/options-monitor.env" \
  --markets us hk \
  --accounts lx sy \
  --output-dir /tmp/options-monitor-service
```

如果需要飞书长连接，额外加 `--include-feishu-ws`。launchd 不读取 shell profile，渲染器会把 env-file 通过 `OM_ENV_FILE` 写入 plist。

## Linux

Linux 是推荐的生产长期运行平台。

前置依赖以 Debian/Ubuntu 为例：

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv
```

Python 需要 3.10 或更高版本；较旧发行版请安装更新的 Python 包。

安装代码：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o /tmp/options-monitor-install.sh
bash /tmp/options-monitor-install.sh --version v1.2.92 --prefix "$HOME/apps/options-monitor"

cd "$HOME/apps/options-monitor/current"
./om setup check
```

如果这台服务器要跑 Feishu long-connection inbound：

```bash
bash /tmp/options-monitor-install.sh --version v1.2.92 --prefix "$HOME/apps/options-monitor" --with-server
```

生产服务 env-file 推荐放在：

```text
/etc/options-monitor/options-monitor.env
```

初始化模板：

```bash
sudo install -d -m 700 /etc/options-monitor
sudo test -f /etc/options-monitor/options-monitor.env || sudo install -m 600 configs/examples/options-monitor.env.example /etc/options-monitor/options-monitor.env
./om settings doctor --env-file /etc/options-monitor/options-monitor.env
```

生产 runtime root 推荐放在：

```text
/var/lib/options-monitor
```

systemd 服务化使用：

```bash
./om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --env-file /etc/options-monitor/options-monitor.env \
  --markets us hk \
  --accounts lx sy \
  --output-dir /tmp/options-monitor-service
```

如果需要飞书长连接，额外加 `--include-feishu-ws`。

## Manual Install

```bash
git clone https://github.com/liuxie066/options-monitor.git options-monitor
cd options-monitor
git checkout v1.2.92
python3 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt -c constraints.txt
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
  current -> releases/v1.2.92
  releases/
    v1.2.92/
      .venv/
      om
      om-agent
```

升级时安装新 tag，再切换 `current` symlink。长期运行服务应使用 `current` 作为 repo root。

## Safety Contract

installer 允许做：

- clone repo
- checkout 指定 tag
- 创建 `.venv`
- 安装 Python requirements
- 更新 `current` symlink
- 输出下一步命令

installer 禁止做：

- 写 `config.us.json` / `config.hk.json`
- 写真实 env-file 或 secrets
- 创建或启用 systemd/launchd timer
- 启动长期服务
- 连接 OpenD 或 Feishu
- 修改 `option_positions.sqlite3` 或任何交易/持仓状态

安装完成后的下一步是：

```bash
./om setup check
```

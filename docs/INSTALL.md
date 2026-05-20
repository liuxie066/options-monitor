# Install

这份文档只回答一个问题：怎么把 `options-monitor` 安装到机器上。

安装不会创建 runtime config，不会写 env secrets，不会启动 systemd/launchd，也不会连接 OpenD、Feishu 或修改 SQLite 状态。

## Quick Install

默认使用可审计的两步安装，不用 `curl | bash`：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o /tmp/options-monitor-install.sh
bash /tmp/options-monitor-install.sh --version v1.2.90 --prefix "$HOME/apps/options-monitor"

cd "$HOME/apps/options-monitor/current"
./om setup check
```

`--version` 必须是明确 release tag。不要在生产机器上安装浮动分支。

如果需要 Feishu long-connection、远端 inbound 或服务端依赖：

```bash
bash /tmp/options-monitor-install.sh --version v1.2.90 --prefix "$HOME/apps/options-monitor" --with-server
```

## Manual Install

```bash
git clone https://github.com/liuxie066/options-monitor.git options-monitor
cd options-monitor
git checkout v1.2.90
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
  current -> releases/v1.2.90
  releases/
    v1.2.90/
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

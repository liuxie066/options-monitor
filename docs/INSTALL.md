# Install

这份文档只回答一个问题：怎么把 `options-monitor` 安装到机器上。

安装不会创建 runtime config，不会写 env secrets，不会启动 systemd/launchd，也不会连接 OpenD、Feishu 或修改 SQLite 状态。默认会创建用户级 `om` / `om-agent` wrapper，方便从任意目录启动。

OM 使用 **Python 3.12 或更高版本**。完整运行包携带私有解释器和运行依赖，不要求用户安装系统 Python、Git 或 Homebrew；源码安装另有前置条件。富途 OpenD 是独立程序，仍需用户按官方说明安装、登录，运行包不包含它。

## 运行包资格与源码安装

下述默认运行包路径已进入当前源码，但本次变更尚未发布，不能假定最新 Release 已提供对应资产。平台名只是构建/选择目标；实际解包搬迁、自检和宿主系统回执通过后，才能将该版本、平台列为合格。已有本机开发包实测，其他平台和最低系统版本资格仍需分别取证，不声称所有 macOS/Linux 版本受支持。

| 构建目标 | 实际运行包资格 |
|---|---|
| `macos-arm64` | 开发包在 macOS 27.0.1 arm64 通过构建、搬迁及无用户依赖安装；尚未发布 |
| `macos-x86_64` | 候选 CI 目标，尚无实际支持回执 |
| `linux-x86_64` | 候选 CI 目标，尚无实际支持回执 |

当前锁文件的拟发布集合仅含 `macos-arm64`；其他两项是资格矩阵候选，不代表随本次 Release 提供。拟发布也不等于任务已通过或资产已发布。

默认安装要求目标稳定 Release 有精确匹配的 `options-monitor-<tag>-<platform>.tar.gz` 及可信 SHA-256。安装端需要 shell、HTTPS 下载工具 `curl`、`tar`、`awk`、`sed` 和 `sha256sum` 或 `shasum`，会先检查缺失工具。没有合格资产、摘要不符或平台不匹配会失败，不会自动安装编译器、Homebrew 或切换源码路径。发布证据要求见[运行包发布资格](RELEASE_PROCESS.md#完整运行包的发布资格)。

尚无合格资产时，若选择源码安装，须自己准备 **Git、Python 3.12+ 和 venv 支持**，并显式指定：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o /tmp/options-monitor-install.sh
bash /tmp/options-monitor-install.sh --source
```

这仍只安装已发布版本，不包含尚未发布的引导能力。固定版本追加 `--version <release-tag>`；开发 checkout 见 [Manual Install](#manual-install)。Homebrew 只是准备源码依赖的可选途径，运行包用户不需要它。

## Quick Install

确认目标版本已有本机平台合格运行包后，默认解析并安装最新稳定 GitHub Release，不安装浮动 `main`：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash

"$HOME/.local/bin/om"
```

安装器始终安装已发布版本，不安装 `main` 上尚未发布的改动；新 CLI 参数以所安装版本的 `om setup init --help` 为准。

安装输出会明确打印解析到的 release tag，例如：

```text
[install] resolved latest release: v<version>
```

需要复现、回滚或固定生产版本时，使用可审计的两步安装并显式指定 release tag：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o /tmp/options-monitor-install.sh
bash /tmp/options-monitor-install.sh --version "<qualified-release-tag>" --prefix "$HOME/apps/options-monitor"
```

将占位 tag 换成已确认有本机平台合格运行包的已发布版本，不代表任一历史版本已补发运行包。

安装脚本默认把 wrapper 写到 `$HOME/.local/bin`。如果该目录不在 `PATH`，安装输出会提示：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

如果不想创建全局命令，加 `--no-install-cli`；此时使用 fallback：

```bash
"$HOME/apps/options-monitor/current/om"
```

安装完成后只打印实际入口的 shell 引用命令：有 wrapper 时使用实际 `--bin-dir` 下的 `om`，否则使用 `current/om`，兼容中文与空格路径。它进入统一首页，不强制追加 `setup init`，也不自动启动向导或连接券商。

如果需要换 wrapper 目录，用 `--bin-dir PATH`。如果目标目录里已有未知来源的 `om` / `om-agent`，installer 会拒绝覆盖；确认要接管时才使用 `--force-cli-wrapper`。

运行包已包含公开运行功能所需依赖，关闭的可选功能不要求配置凭证。仅在源码安装需要 Feishu long-connection、远端 inbound 或服务端依赖时，加 `--with-server`：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash -s -- --source --with-server
```

## macOS

macOS 提供本地手动运行和 LaunchAgent 入口；运行包资格按上表和实际宿主回执确认，不由入口存在推断。

以下前置条件仅用于显式源码安装，运行包不需要这些步骤：

```bash
xcode-select --install
python3.12 --version
```

Python 需要 3.12 或更高版本。

已有 Homebrew 时可用下列方式准备源码依赖；也可使用其他受信来源，无需为 OM 运行包安装 Homebrew：

```bash
brew install python@3.12 git
```

安装代码：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash -s -- --source

om setup check
```

如果这台 Mac 要跑 Feishu long-connection inbound：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash -s -- --source --with-server
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

Linux 提供 systemd 部署入口；运行包只按精确平台和版本验收，不把构建目标当作全部发行版支持承诺。

仅源码安装的前置依赖以 Debian/Ubuntu 为例；运行包无需安装系统 Python/Git：

```bash
sudo apt-get update
sudo apt-get install -y git python3.12 python3.12-venv
```

Python 需要 3.12 或更高版本；较旧发行版请先启用提供 Python 3.12 的受信软件源或升级发行版。

安装代码：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash -s -- --source

om setup check
```

如果这台服务器要跑 Feishu long-connection inbound：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash -s -- --source --with-server
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
      # 源码安装使用 .venv；运行包改为：
      om.runtime.json
      runtime/python/bin/python3
      om
      om-agent

$HOME/.local/bin/
  om        -> wrapper execs $HOME/apps/options-monitor/current/om
  om-agent  -> wrapper execs $HOME/apps/options-monitor/current/om-agent
```

运行包与源码安装共用该目录及 wrapper 布局；两种解释器目录不是同时必需。解释器选择先检查显式 `OM_PYTHON`，再检查已声明运行包，然后是已有 `.venv` 和系统候选。声明运行包但解释器或清单损坏时明确失败，不偷用系统 Python。

初装不接管活动实例；检测到活动部署时使用[受控升级](RELEASE_PROCESS.md#远端自动升级)，不要用安装命令代替服务升级。升级在自身事务内物化新版本并切换 `current`，源码保持原路径，运行包使用已校验资产，不隐含转换安装类型。长期运行服务应使用 `current` 作为 repo root。
用户级 wrapper 指向 `current`，所以升级后 `om` / `om-agent` 会自动跟随新的 release。

在 `current` 实际切换前发生可恢复失败或收到 INT/TERM，安装器会恢复原有 wrapper，原本没有入口则移除本次新建入口；切换完成后保留新入口。恢复或清理失败会非零退出，并报告尚存备份的路径。已发布的失败 release 保留，普通重试仍拒绝已有 inactive target；确认后可沿用 `--force` 重建，活动 release 仍拒绝 `--force`。

新发布路径若发现初始 `current` 的链接文本已精确等于本次目标 release 路径（包括悬空链接），或无法读取初始链接，会在事务备份和发布前拒绝，保留原链接、入口与已有备份；已安装快路径保持原行为。上述恢复不保证覆盖 SIGKILL、断电或其他安装器/操作者并发修改现场。

## Safety Contract

installer 允许做：

- 下载并校验目标稳定 Release 的运行包，在独占 staging 解包、自检并发布 release 目录
- 显式 `--source` 时 clone/checkout 目标 Release、创建 `.venv`、安装 requirements
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
"$HOME/.local/bin/om"
```

无配置时进入首次配置；软件安装成功不代表账户连接或首次扫描完成。完整步骤见[首次运行指南](GETTING_STARTED.md)。

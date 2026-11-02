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

产品运行包在构建时排除仓库开发指令 `AGENTS.md`、`CLAUDE.md` 和 `.agents/`（含嵌套目录），避免安装产品时将维护者的私人称呼和开发约定加载到用户 Agent。显式 `--source` 安装在 clone 后执行相同排除，也覆盖旧 tag；仅源码安装保留 Git 元数据供版本检查与升级使用。重复安装的已安装快路径不清理已有文件，开发者直接 clone 的工作区也不受影响。

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

运行包包含基础运行依赖，关闭的可选功能不要求配置凭证。源码安装服务端依赖可加
`--with-server`；飞书收发另加 `--with-feishu`，微信收发另加 `--with-wechat`。
例如安装含飞书消息能力的源码实例：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash -s -- --source --with-server --with-feishu
```

## 官方可选插件

官方插件固定为 `pm`（PM 接入）、`position-advice`（持仓评估与建议）、`feishu`（飞书收发）和 `wechat`（微信收发）。包含本分发功能的 Release 提供四个 `py3-none-any.whl`、`om-plugins.json` 和 `om-plugins.md`，绑定精确 OM 版本、tag、commit、Python >=3.12 及资产摘要。该路径尚未发布；不能假定旧 Release 已有这些资产，也不会退回下载其他版本的插件。

源码版与完整运行包使用同一入口；添加插件时无需另行使用 pip、Git 或安装系统 Python：

```bash
om plugins list
om plugins list --format json
om plugins install pm                         # 默认只预览
om plugins install pm --confirm
om plugins install position-advice --confirm
om plugins install feishu --confirm
om plugins install wechat --confirm
om plugins uninstall pm                      # 默认只预览
om plugins uninstall pm --confirm
```

`list` 只读本地状态，不联网。安装从当前 OM 版本的官方稳定 Release 获取清单，确认后仅下载所选 wheel；校验资产摘要、大小、包元数据、RECORD 和隔离导入，再原子发布安装目录。缺资产、版本不配套或校验失败会中止，不修改配置和已有安装。

飞书插件依赖 `lark-oapi`，用户无需另行安装 SDK。确认安装时复用目标解释器中已验证可用的 SDK，不强制降级；源码版缺 SDK 时，优先使用可用的 uv（无需目标 venv 内有 pip），否则使用该实例的 pip，按 `constraints/runtime-bundle.txt` 下载到插件专属 `dependencies/` 目录，保留文件摘要并验证实际 SDK API 和依赖兼容性。依赖校验使用核心运行依赖 `packaging`，不依赖 pip 内部模块。安装失败不启用插件、不改源码 venv。此步骤需要可访问 Python 包索引；预览不执行。运行包复用其已锁定的 SDK，缺失或损坏时拒绝安装，不向不可变运行包补包。微信无第三方依赖。

文件位于代码目录同级 `.om-plugins/<代码绝对路径摘要>/<插件名>/`，与运行包的不可变文件清单分离。安装选择属于这个 OM 代码实例，改变账户或运行目录不改变安装选择；新代码路径需由受控升级迁移选择。进程固定使用启动时的物理代码目录，切换 `current` 不会让旧进程改读新版本插件。直接搬迁代码后应重新检查安装状态。请以该实例实际运行用户执行安装和卸载，插件目录为该用户私有，不要用 sudo 改变归属。

安装不等于启用：PM 仍由 `portfolio_management.enabled` 控制，持仓建议仍由 `close_advice.enabled` 控制；消息渠道仍使用 `channels.feishu`、`channels.wechat_clawbot` 的总开关、收信和通知开关。命令不改配置、凭据、账本、历史报告或服务；已加载插件的进程需另行重启，新进程按需加载。损坏的托管安装可先明确卸载再重装。受控升级在切换前安装并校验目标版本的已选插件，目标资产缺失会保留当前实例；见[升级流程](RELEASE_PROCESS.md)。

卸载先将安装原子移出加载目录，再清理文件；移出失败保留原安装，清理失败则明确显示已卸载及残留目录（JSON 为 `cleanup_pending`、`retained_directory`），这些残留不会被加载，不应误认作仍已安装。

回滚使用目标版本原有的插件选择，确认时先校验其 receipt 和实际加载，再暂存配置和切换代码；失败保留当前实例，不下载或迁移插件。仅预览时不加载插件。

已有 pip 安装会显示 `python_environment`，不由此命令覆盖或删除；先用原环境卸载，再改用官方托管路径。如果后来又通过开发依赖安装了同名 pip 包，托管卸载会明确拒绝，须先移除 pip 包，避免移除托管副本后又加载另一份。以下源码开发方式仍保留：源码包版本目前是 `0.1.0`，官方 wheel 版本跟随 OM VERSION，二者不能互作兼容凭据。

## 可选 PM 与长桥依赖

PM 普通安装见[官方可选插件](#官方可选插件)。以下 pip 开发方式及长桥 SDK 仅用于源码安装（安装器使用 `--source`）；不要向默认运行包的内置环境追加依赖。
使用**运行该 OM 实例的 Python 环境**安装；以下从源码根目录示例，不会开启配置、
写入凭据或连接外部服务。PM 源码包随仓库提供。

```bash
./.venv/bin/python -m pip install ./plugins/portfolio-management
./.venv/bin/python -m pip uninstall om-pm-plugin
```

PM 默认关闭。安装后设置全局 `portfolio_management.enabled: true`；卸载前关闭 PM
及 `portfolio.holdings.enabled`。未安装时 `om help`、`om tools spec` 与非 PM 功能可正常
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
同名插件。源码 service upgrade 保留当前实例实际安装的 PM 扩展和 Longbridge SDK，
与配置是否启用无关；未安装或主动卸载的包不会因配置仍启用而补装。首次从内置 PM 的
旧版迁移时，仅为有效旧配置已启用的 PM 安装扩展。若目标是运行包且需要保留 pip 安装的插件或长桥 SDK，
升级在切换前拒绝；通过 `om plugins` 托管的 PM、持仓建议、飞书和微信另走官方资产迁移路径。缓存及切换前校验见
[升级流程](RELEASE_PROCESS.md)；首次迁移必须使用含修复的新控制器，旧版自动升级器不能
自行获得目标 release 中的修复。

## 可选持仓建议

运行依赖默认不安装持仓建议包。普通安装见[官方可选插件](#官方可选插件)，支持源码版与完整运行包。以下仅为源码开发方式，
安装器 `--with-position-advice` 须配合 `--source`。使用该实例的 Python 从源码根目录安装：

```bash
./.venv/bin/python -m pip install ./plugins/position-advice
./.venv/bin/python -m pip uninstall om-position-advice-plugin
```

安装器也支持 `--with-position-advice`；安装不启用，需在配置中明确开启 `close_advice.enabled`。缺包不影响核心扫描启动及历史报告读取。开发/CI 的 `requirements/dev.txt` 安装此包并保留全部策略测试；缺包启动由隔离子进程覆盖。受控升级保留实际安装选择，并为旧内置建议已启用的实例迁移安装；主动卸载保持卸载。首次迁移仍须使用新升级控制器，见[升级流程](RELEASE_PROCESS.md)。

## 可选消息渠道

飞书与微信分别安装，也可均不安装。每个包包括收发传输；核心 CLI、本地扫描、报告和历史读取
不要求消息包。Feishu Bitable 数据集成独立于消息插件。
源码版与运行包均可通过[官方可选插件](#官方可选插件)安装 `feishu`、`wechat`，两者可同时选择。
以下源码开发方式仍保留：安装器使用 `--source --with-feishu` / `--source --with-wechat`，
也可使用实例的 Python 从源码根目录安装或移除：

```bash
./.venv/bin/python -m pip install ./plugins/feishu-channel
./.venv/bin/python -m pip install ./plugins/wechat-channel
./.venv/bin/python -m pip uninstall om-feishu-channel-plugin om-wechat-channel-plugin
```

安装不启用；按 [配置契约](../CONFIGS.md#消息渠道) 设置各渠道总开关、收信与主动通知。
开发依赖安装两包，以保留完整回归；基础 server 依赖不再强制安装飞书 SDK。
缺包与配置关闭是不同状态，`om channel status` 分别展示。选择了消息插件的升级必须保留安装选择，
托管选择通过目标版本官方资产保留；pip 安装的消息包不能直接迁移进固定运行包，切换前拒绝。
首次迁移仍使用含修复的新升级控制器。

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
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash -s -- --source --with-server --with-feishu
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
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash -s -- --source --with-server --with-feishu
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

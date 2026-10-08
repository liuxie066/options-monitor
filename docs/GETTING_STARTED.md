# Getting Started

这份指南从已安装 OM 开始。还未安装时先看 [INSTALL.md](INSTALL.md)。下文使用全局命令 `om`；源码 checkout 可用 `./om`。

## 1. 进入任务菜单

在交互式终端运行 `om`，选择首次安装或日常管理。非交互终端显示帮助，不等待输入。`om help` 按任务导航，`om help all` 保留全部高级命令；各命令的 `--help` 给出完整参数。

日常菜单包含结果与状态、账户与标的、通知与 Bot、策略与全局持仓风险、运行维护。查看结果读取已有记录，手动运行和外部连接需另外选择。

## 2. 初始化配置

```bash
om setup init
```

引导顺序：

1. 显示平台默认配置与运行目录，无需普通用户选择。高级用户可以传 `--output-dir`。
2. 选择一个市场，填写 OpenD 地址、端口，明确 REAL 或 SIMULATE，填写自定义账户标签和数字富途账户 ID。账户标签必填且无默认值；脚本化初始化也须提供 `--account-label`。OM 不安装或登录 OpenD。
3. 输入自己的监控标的。每个标的选择 CSP、CC 或 both；CSP 必填 max strike，CC 必填 min strike；另外两个边界可选。边界必须是正数，最小值不能超过最大值。
4. 检查账户、环境、标的、策略、文件路径预览，输入 `yes` 保存。
5. 选择是否验证 OpenD 连接；通知通道、Bot LLM、常驻服务分别询问并可跳过。
6. 选择是否手动运行一次。默认不发通知；SIMULATE 体验模式显式使用 `--experience --no-send`，沿用手动体验的约束。

首次只配置一个账户；之后通过日常菜单或 `om accounts add` 添加。同市场账户共享标的。新增市场必须同时填写该市场的用户标的和策略，完整验证后才发布。

新配置的通知与 Bot 默认关闭，未配置不会算作就绪。后续可用 `om channel configure` / `om bot configure` 补齐。再次运行 `om setup init` 保留已保存的配置并继续可选步骤；账户与标的修改走日常管理。确认前取消不写入；已完成的配置、凭证或服务步骤分别保留并报告。

初始化生成 `config.yaml`、所选市场 `config.us.json` / `config.hk.json`、`resolved/config.bot.json`，并在 `~/.config/options-monitor/runtime-root` 记住目录。显式配置路径及有效 `OM_RUNTIME_ROOT` 优先于记录；记录损坏时会报错，不偷换到另一个实例。创建过程异常中断可能留下文件，需先核对具体冲突；不会自动覆盖或猜测删除归属。

脚本化创建使用 `om setup init --help` 中的完整参数：`--account-label`、市场、数字 `--futu-acc-id`、`--trd-env REAL|SIMULATE`、每个用户标的及策略边界。先用 `--dry-run` 预览，再以同样参数改用 `--apply`。高级 `om config init` 也须提供账户标签，可创建尚未填写富途账户 ID 的占位配置，但不会将其判断为就绪，也不建立用户运行目录记录。

### 日常维护账户与标的

推荐直接进入 `om` 的日常菜单，表单会保留未修改的字段并在预览后确认。命令行也保留相同能力：

```bash
om accounts list
om accounts add --help
om accounts edit --help
om symbols list
om symbols add YOUR_SYMBOL --strategy csp --csp-max-strike YOUR_MAX_STRIKE
om symbols edit YOUR_SYMBOL --set sell_put.max_strike=YOUR_MAX_STRIKE
```

替换占位参数，不直接照抄标的或价格。新增标的不能省略策略和必要边界。标准代码可识别市场，例如 `NVDA` 和 `0700.HK`；指定 `--market` 时必须一致。多个市场的 list 需指定市场。改动同市场标的会影响该市场全部关联账户，预览显示这些账户。

写入前保留预览给出的 source SHA，确认时追加 `--apply --expected-source-sha256 <SHA>`；账户命令还需 `--confirm`。菜单自动携带 SHA，文件在确认前被其他进程修改时拒绝旧预览。已有直接 `--apply` 脚本保持兼容。

所有这些命令通过同一配置事务发布 YAML 和生成快照。只有手动编辑 YAML 时才需要 `om config build` / `build-bot`；不要编辑生成的 JSON。完整配置解释见 [CONFIGURATION_GUIDE.md](../CONFIGURATION_GUIDE.md)。

## 3. 完成外部接入

### 富途 OpenAPI/OpenD

准备并登录 OpenD，确认对应市场权限与账户环境。端口可连不等于登录就绪；诊断需要检查 READY、行情/交易登录及账户映射。初始化记录端点和身份，不代替实际券商验证。说明见[部署指南](DEPLOY_LINUX_MAC.md#5-opend--futu-前置条件)。

```bash
om doctor --config-key us
```

按实际市场选择 us/hk。带标的的富途字段检查对两市场都读取标的快照和交易状态；`option_fields_ok` 仅表示期权字段可用，`scan_prerequisites_ok` 还要求现有扫描行情规则认可的标的观测（身份、价格、时间、证券与市场状态）。闭市或数据缺失时可能为 false，手工价格不能替代该观测。检查通过不代表所有策略筛选条件都通过。`doctor` 会尝试连接 OpenD；`setup check` 是离线检查，两者用途不同。调整映射使用 `om accounts edit`。

### 通知通道

```bash
om channel configure
```

选择 Feishu App 或 WeChat ClawBot。Feishu 表单分别收集 App ID、通知接收人的 open_id、Bot 入站允许名单，隐藏录入 App Secret。微信通过现有扫码绑定流程确认目标；绑定状态和路由保存分别报告。配置不会自动发送测试消息。

显式关闭通知后，定时通知、交易与维护回执、系统告警跳过外部发送。旧配置未写 `notifications.enabled` 时保持原有启用语义。Bot 对用户主动消息的回复受 Bot 自身开关和入站权限控制。已有历史回执保留；配置命令自身不重放消息。

### Bot LLM

```bash
om bot configure
om bot model catalog --format text
om bot model check --active --format text
```

表单选择已有模型配置或填写受支持的 provider/model，分别预览并保存模型与凭证。模型检查是静态配置与凭证状态检查，不调用远端模型。对话命令 `om bot run` 保持只读问答语义；`om bot handle` 保留 Control 的写入权限与确认规则。

推荐名称统一为 `om bot`；旧 `om assistant` 仍兼容。Feishu 传输的推荐入口为 `om channel feishu event` / `serve`，旧 `om inbound feishu` / `feishu-ws` 仍可用。不会因为名称合并而开放写权限。

### 普通设置与密钥

可选功能表单默认将普通设置写入 `<runtime_root>/options-monitor.env`，权限 0600，保留无关行并备份。显式 `--env-file` 和调用者 `OM_ENV_FILE` 优先；若它指向其他文件，表单提示明确目标。CLI 每次调用绑定所选实例并在结束后恢复临时环境，不将上一个实例的接收人带到下一实例。源码本地 `.env/options-monitor.env` 保留兼容。

密钥使用 macOS Keychain 或 Linux systemd encrypted credentials；不会通过 CLI 参数、普通 env 表单或 Agent 输入框传入。完整逻辑名见 [Secret Storage](SECRET_STORAGE.md)。

Linux 普通用户无法直接写系统加密凭证目录，向导会保留“待完成”状态并给出单独的终端命令，例如：

```bash
sudo "$(command -v om)" secrets set feishu.bot.app_secret --backend systemd
```

使用终端隐藏输入。加密文件已存在不等于当前进程可以解密；普通前台进程没有 systemd credential context 时仍不能读取它。长期服务按实际启用的消费者绑定所需凭证，服务身份的读取能力需在实际环境验证。不要把静态状态或文件存在称作运行成功。

## 4. 检查状态与扩展功能

```bash
om setup check --format text
om settings doctor --format text
om secrets status --format text
om status --config-key us
om daily-brief latest
```

`setup check` 检查配置快照、依赖、目录及安装先决条件，Bot 单独报告；它不证明 OpenD 登录、通知送达或模型可用。`status` 和 `daily-brief latest` 读取已有状态和结果，留意时间和缺失原因。

日常的策略与风险菜单可发现：

- `om close-advice configure`：开关平仓建议，默认开启；关闭保留历史结果。
- `om holdings configure`：为全局持仓风险纳入同机 Portfolio Management 的可选非富途资产。启用 PM 集成需另行确认，再预览来源与批准券商；只接受 loopback 服务地址。关闭不依赖 PM 在线。无需配置旧 Feishu Holdings 表，也不创建 external_holdings 账户。
- `om wheel`：沿用账户层激活和策略确认。
- Combo：沿用 `symbols edit` 的 yield_enhancement 高级配置，无第二套总开关。

账本、交易审核、研究、诊断包和升级等高级入口仍在 `om help all`。结构化工具供 Agent 或脚本使用，见 `om-agent spec`。

## 5. 可选：Feishu long-connection

```bash
om channel feishu serve --check
```

这只检查配置。长期接收与回复需在安装服务时显式选择消息接入通道和绑定市场，或按完整帮助手动运行。配置通知不自动启动 Bot 接入。

## 6. 可选：长期运行服务

重新开启通知前，预览会说明后续调度将恢复尚未确认投递的待处理回执，包括通知关闭期间积累的回执。已确认投递的记录不重复发送；配置命令本身不发送消息。

推荐使用 `om` 日常菜单中的运行维护 → service。也可直接预览：

```bash
om service install
om service start
om service stop
```

每次根据当前预览追加 `--confirm --expected-preview-sha256 <SHA>` 才执行。安装与启动分开；安装缺失的普通 env 文件时仅创建空文件并设置权限，保留已有内容。start/stop 读取已安装 profile，无需重新填写通道。操作另一实例时明确传 `--config-yaml` / `--runtime-root`；固定服务名称不支持悄悄接管其他实例。

预览列出所有任务，包括交易监听写入账本、自动平仓记录维护和可能的通知。新启用可选功能后须按新配置重新预览安装服务，配置修改本身不重启已暂停服务。

macOS 使用当前用户 LaunchAgents/Keychain；Linux 使用 systemd 系统服务和明确的部署用户。普通用户默认自身身份，root 必须指定 `--deploy-user`，配置与运行目录的属主必须匹配。权限不足时显示交接命令，不自动 sudo。所有可选功能关闭时，基础服务不会要求无关 Feishu/LLM 密钥。

`om service render` 保留高级文件渲染能力，只生成文件和说明，不实际安装。当前 macOS 自动升级的服务协调仍有限制；基本 install/start/stop 不表示所有升级能力都已实现。实际平台服务、OpenD、模型和消息投递需在授权的目标环境验收，开发测试不代替这些事实。

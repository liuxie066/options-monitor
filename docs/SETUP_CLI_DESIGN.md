# Setup CLI 交互设计

## 目标与边界

让新用户在 macOS 或 Linux 的终端完成 OM 的首次配置，并在以后通过终端找到、修改和验证基础与高级功能。用户不需要把账户或密钥发到 Agent 聊天框。`config.yaml`、普通 env-file、Keychain/systemd credentials 继续分别拥有各自的值；运行 JSON 仍由 YAML 生成。

本设计只授权本地源码、文档和隔离测试修改。不安装、启动或重启 launchd/systemd/OpenD，不连接真实券商或 Feishu，不修改真实运行目录、凭据、报告或账本，不提交、推送、发布或升级。

成功信号：

1. S1：在交互终端用 `om setup init` 可选择 Mac/Linux 的手动运行路径或长期服务路径，生成只含所选市场、Futu 账户和标的的最小配置；未确认、取消、非 TTY、权限不足和已有目标不会静默覆盖或留下半成品；写入中断有可读回的恢复结果。
2. S2：`om config edit` 能显示配置来源和功能目录，支持修改现有基础配置；CSP、CC、Combo Yield、Wheel、Close Advice、Assistant/Bot、通知和外部持仓都能从高级入口到达真实 authoring source，完成校验、预览和确认。全部合法高级字段可经终端编辑器编辑，不另造配置存储。
3. S3：普通 env 设置和密钥有明确、平台正确的终端操作路径；秘密不进参数、stdout、JSON、日志、YAML 或普通 env。Linux 权限不足时提供部署用户与管理员分别执行的命令，不在向导内隐式提权。
4. S4：`setup check` 使用首次选择的精确路径，按所选市场和实际启用功能报告离线配置、凭据存储及服务文件的证据级别；券商登录、行情、通知消费均标为未验证；占位 Futu ID 不判为已就绪；非 Bot 用户不因 Bot 模型或会话目录得到首装错误。
5. S5：Mac/Linux 路径、首次运行、再次编辑、失败/取消、安全输出有隔离回归测试；README、安装与首次使用文档说明交互入口及手动命令的等价路径。

## 当前事实与复用清单

检索范围：`src/interfaces/cli/{main,setup_ops,config_ops,account_ops,settings_ops,secret_ops}.py`、`src/application/{config_yaml_init,config_yaml_accounts,config_yaml_symbols,config_authoring_transaction,setup/check,platform_profile}.py`、`src/application/settings/effective.py`、`src/infrastructure/secret_store/factory.py`、`CONFIGS.md`、`CONFIGURATION_GUIDE.md`、`docs/{INDEX,GETTING_STARTED,SECRET_STORAGE,INSTALL}.md` 和相关测试。关键词 `setup init`、`config edit`、`interactive`、`getpass`、`publish_yaml_config_generation`；前两项当前无入口。

| 语义 | 归属裁决 |
|---|---|
| CLI 命令解析与 JSON envelope | 复用 `src/interfaces/cli/main.py`、`setup_ops.py`、`config_ops.py`；仅新增人工交互适配，不改 `om-agent` 协议 |
| 首装模板与 YAML 合成 | 复用 `config_yaml_init.init_yaml_config` 的输入规范化和模板；最小默认值和所选市场在此修正，向导不复制模板逻辑。首次多文件发布扩展现有 authoring transaction 的“源文件不存在”分支，不复用当前逐文件直接写入 |
| 账户与标的编辑 | 复用 `config_yaml_accounts.mutate_yaml_account_config`、`config_yaml_symbols` 的领域校验和发布机制；交互只收集参数 |
| 完整 YAML 高级编辑 | 复用 `config_yaml.load_yaml_config_file` 与 `config_authoring_transaction.publish_yaml_config_generation`；新增一层终端编辑器适配，因为现有 CLI 没有编辑完整 YAML 的入口 |
| 普通环境设置来源与校验 | 复用 `settings/effective.py` 的 env 解析、来源解释和 `settings doctor`；不把 env 搬进 YAML |
| 秘密 | 复用 `secrets set/status` 与 `secret_store.factory`；不新建 secret 字段或后端 |
| 平台路径和服务目标 | 复用 `platform_profile.current_platform_profile`；交互按手动/服务用途选路径，不用 OS 名直接决定写入权限 |
| 配置检查 | 复用 `setup.check.run_setup_check` 与 runtime readiness；按启用功能修正误报，不建第二套诊断 |
| 新名称 | 仅新增 `om setup init`、`om config edit` 两个公开命令；`om settings inspect/doctor/explain` 继续为只读生效设置诊断 |

`docs/INDEX.md` 指向 `CONFIGURATION_GUIDE.md` 作为操作说明 owner，`CONFIGS.md` 作为配置事实链 owner。两者均非交互工作流的技术设计 owner，因此本文是唯一交互设计 owner；实施后更新操作说明，不在其它文档复制设计状态机。

## 交互与写入合同

### 首装 `om setup init`

命令只在 `stdin` 为 TTY 时提问；`--help` 和现有非交互 `om config init` 继续可用于脚本。提示写到 `stderr`，最终结构化结果仍写到 `stdout`。输入 EOF、Ctrl-C 或任一步选择取消，返回明确取消结果且不写目标。向导不连接 OpenD/Feishu，不启动服务。

顺序：检测平台 → 选择手动运行/长期服务 → 选择运行目录与普通 env-file 路径 → 市场 US/HK → Futu 账户标签和数字格式 ID → 所选市场的初始标的 → 展示可选功能入口及其前提 → 展示文件路径与将启用的功能 → 明确确认 → 生成 YAML 与快照 → 用同一运行目录、env-file 和市场运行只读检查。数字格式只证明输入形状，账户身份须后续券商只读回查。高级功能可以在首装结束后立即进入 `om config edit`，也可稍后进入；跳过不影响最小配置。

Mac 默认 runtime 在 `~/Library/Application Support/options-monitor`；Linux 长期服务默认 `/var/lib/options-monitor`、env-file `/etc/options-monitor/options-monitor.env`；Linux 手动运行默认用户目录并允许覆盖。需要 root/deploy-user 权限的 Linux 目标在写入前阻断并给出准备命令；向导不调用 `sudo`。向导在结果中返回 `runtime_root`、`config_yaml_path`、`env_file`，以及可复制的 `om config edit --runtime-root ... --env-file ...`、`om setup check --runtime-root ... --env-file ...` 命令。路径优先级为显式参数 > `OM_RUNTIME_ROOT`/`OM_ENV_FILE` > 平台默认；仅显式与环境两种用户指定来源指向不同目标时显示候选并停止，平台 fallback 不构成冲突。服务渲染仍须显式传入同一路径。env-file 不从含 Linux 绝对路径的模板原样复制到 Mac；只有选择相关集成时才引导普通 env 设置。秘密始终通过 `om secrets set` 终端隐藏输入。

`config init` 的首装模板改为：无外部持仓账户，Assistant/Bot 关闭，只有所选市场；不暗中启用通知、交易写入或服务。Futu ID 必须为数字格式才能通过离线结构检查，但身份仍为 `unknown`。非交互 `config init` 可继续生成待填草稿，但其返回必须明确 `draft`；含占位 ID 的草稿不能构建为就绪快照，也不能被 `setup check` 报为可运行。已存在目标默认拒绝，禁止向导替用户使用 `--force`。

首次创建使用现有配置发布事务的同一锁、暂存生成、manifest、提交及恢复语义，增加不存在源文件的分支：确认前与持锁后两次检查 YAML 和目标 JSON 均不存在；构建所有目标内容成功后才开始发布。每个目标采用原子、无替换创建，避免锁外进程并发创建时被覆盖。源 YAML 排在快照后提交；失败时若源仍不存在，恢复删除本次新建目标并读回原状；若源已提交，恢复补齐目标并读回完整生成。无法证明恢复时保留 manifest、返回 `unknown` 并提示人工处理。旧版 `config init --force` 行为不扩展到向导。注入每个写入点失败及源提交后的清理失败，验证既无假成功也不覆盖并发创建的文件。

### 后续设置 `om config edit`

菜单先显示权威 YAML 路径、普通 env-file 路径、所选市场及账户、当前功能开关与来源。`--runtime-root` 与 `--env-file` 接受首次返回的精确路径；不从仓库副本或旧快照自动推断。基础区可修改市场、账户和标的；高级区包含 CSP、CC、Combo Yield、Wheel、Close Advice、Assistant/Bot、通知与外部持仓。每项显示作用、实际已有的开关/参数位置、所需数据或凭据和当前状态；不为没有开关的功能创造新开关。

已存在的窄写入命令继续负责账户和标的更新。对其余高级 YAML 字段，用户可从对应菜单选择“编辑完整配置”：CLI 把当前 YAML 复制到权限为 `0600` 的临时文件，以 `VISUAL`、`EDITOR` 或系统 `vi` 通过 argv 启动编辑器，不经过 shell；退出后拒绝重复 YAML 键及疑似明文秘密字段，解析并校验编辑后**全部市场**及 Assistant 配置，展示实际会发布的序列化结果的变更键路径与摘要（值不输出），再要求确认。只有确认后调用现有配置发布事务，使用原文件 SHA 防止并发覆盖；无变化不写，编辑器失败/取消不写。序列化可能去除注释和重排格式，确认前明确提示；事务保留其它 YAML 键，提供备份、快照构建、冲突保护和失败恢复。删去市场时，旧 JSON 和服务仍可能运行；本命令报告旧路径为 `retirement_pending`，不暗中删除或停服务。

普通 env 的高级项使用现有 `settings inspect/explain/doctor` 展示来源，并给出选定 env-file 的终端编辑命令；CLI 不在同一事务中承诺 YAML、env、Keychain 三者原子提交。`sudoedit` 是外部手动写入，不承诺 CLI 的预览/确认/回滚，写后必须运行同路径的 `settings doctor`；若诊断报语法、重复键或秘密字段，明确停在待修复状态。密钥菜单只显示 `secrets status` 和需要执行的 `secrets set/rotate` 命令，不接收秘密值。Linux 手动 shell 即使 systemd 加密凭据存储 `present`，若无 `CREDENTIALS_DIRECTORY` 仍为 `pending`，不建议把秘密退回 env。每一步输出 `configured`、`pending` 或 `unknown`，允许下次继续。

所有交互写入都显示被修改的精确路径，且不修改正在运行的服务。修改后提醒生成快照不代表运行进程已加载新值；服务部署与重启仍走独立的 `om service` 受控流程。

### 验证与失败语义

`setup check` 只按选中市场和传入的 `--runtime-root`/`--env-file` 检查本地快照；Bot 未启用时模型和会话目录为不适用而非错误；凭据 `present` 只表示存储可观察，不能声称服务已收到或调用成功。配置结构、占位 ID、市场快照新鲜度、服务文件可观察；OpenD 登录/行情及通知消费均为 `unknown`，须用户另外运行明确的在线或投递验收命令。普通 env-file 必须在检查和最终建议中使用同一路径。

向导确认前完成目标存在性、写权限和全部可离线完成的语义验证；发布失败依赖现有事务的补偿/恢复证据。取消、EOF、编辑器非零退出、无效 YAML、来源 SHA 冲突和缺权限均保留原文件和快照。Keychain/systemd 凭据 provisioning 是独立步骤，已成功的凭据写入不因后续配置失败自动删除。

## 实施切片与验收

| 切片 | 行为增量 | 覆盖 | 依赖 |
|---|---|---|---|
| A | 最小首装模板、市场/占位 ID/readiness 合同及针对性测试 | S1、S4 | 无 |
| B | `setup init` 和 `config edit` 终端交互、YAML/env/密钥入口与取消/冲突/权限测试 | S1、S2、S3 | A |
| C | Mac/Linux 端到端隔离验收、功能发现与安装/配置文档 | S3、S4、S5 | B |

每片在临时目录中运行，注入 fake prompt/editor/secret provider，不读取真实 Keychain、`/etc`、`/var/lib` 或用户 runtime。相关 CLI facade、配置事务与 setup 检查跑针对性测试；最终运行项目要求的静态检查、文档 guardrail 和受影响的回归测试。完成标准包括 TTY 与脚本输出不互相污染、无秘密泄露、无真实服务调用、Mac/Linux 默认与显式路径均可回读。

## 取舍、风险与未决项

不采用新的 TUI 依赖、重复配置数据库、把所有高级字段做成表单或在向导中自动安装服务。高级菜单提供功能索引和可达的完整编辑路径；当真实使用表明某一高级字段反复被改且容易填错时，再为它增加窄的类型化提示。现有 YAML 序列化可能改变注释/格式，预览必须显示完整 diff，备份必须可恢复。Linux systemd 不一定可用，服务步骤应检测实际能力并停在手动运行/明确诊断，不凭 `platform=linux` 宣称服务已就绪。

没有未决产品选择：用户已要求高级功能保留、Mac/Linux 通用、终端输入而非 Agent 聊天输入，并选择 Devflow full 实现。

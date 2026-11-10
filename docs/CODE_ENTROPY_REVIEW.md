# 代码熵巡检

设计版本1，2026-11-10。本文描述独立扫描器的合同；CI、真实扫描和本机调度是否已启用，以各自回执为准。

## 目标与隔离

本流程检查合并到main的代码是否积累死代码、无用分支、复制和不必要的复杂度。现有code-health负责正确性。两者不导入彼此模块，不读写彼此状态目录、游标、队列、锁和预算，不修改对方的脚本或plist。

| 边界 | 代码熵流程 |
|---|---|
| 默认状态 | `~/Library/Application Support/options-monitor/code-entropy-review/` |
| 日任务label | `com.options-monitor.code-entropy.daily` |
| 周任务label | `com.options-monitor.code-entropy.weekly` |
| issue标签/marker | `code-entropy` / `<!-- code-entropy:KEY -->` |
| 日/周总预算 | 900秒 / 5400秒，独立进程各自计时 |
| 锁 | 自身状态目录的`run.lock`，日周任务非阻塞互斥 |

状态路径必须以code-entropy-review为目录名，并拒绝code-health路径和源码工作区。扫描器只写自己的mirror、报告和决策。GitHub写入口只有建issue；不改代码、不修复、不建PR、不合并、不发版、不自动评论或关闭issue。本次实现的交付PR与运行时权限分开。

## 输入、增量与工具

每轮使用自己的bare mirror fetch远端main，冻结完整SHA，通过Git archive导出tracked源码。工作区未提交文件、其他分支、历史worktree和运行目录不进入输入。指标范围为src、domain、om_strategy_core、scripts、plugins子树的Python文件；仓库根目录tests和其他tracked文本用于引用排除。插件子树按文件清单完整纳入。评分卡保留实际文件列表，不能把范围内LOC冒充仓库所有文件的总行数。

周扫遍历整个指标范围。日扫只对独立日游标之后新增/改动文件生成指标和发现；全仓索引用于引用排除与重复片段对照。首次日扫只建立游标，明确未回溯。周扫不推进日游标。失败、超时、Git历史不连续、基线变化不推进日游标。修改文件中原有的相同key不再列为新增；复杂度继续恶化可重新列为候选，issue仍按稳定key去重。

| 维度 | 固定工具与口径 |
|---|---|
| LOC | Python物理行数；另记非空且非纯注释的行数，后者包含docstring |
| 零引用候选 | 全仓tracked文本词索引与AST；仅私有顶层函数，同名使用、字符串引用会保守排除 |
| 死代码确认 | 零引用、无运行时命中、非公开API、非配置/插件入口四条件均明确true |
| 不可达分支 | 只确认字面`if False` / `while False`的正向分支 |
| 测试未覆盖分支 | 绑定同SHA的coverage JSON中missing_branches，单列，不冒充不可达 |
| 复杂度 | `entropy-ast-v1`，函数独立计数；1–5、6–10、11–20、>20分布，严格>10列候选 |
| 重复率 | `entropy-token-v1`，用行首50 token种子匹配并向两侧延长，整段在两文件中均至少跨10行；重叠位置取并集 |
| 未用配置 | 配置字典键没有其他文本出现时列候选；动态消费者未排除，确认数为null |
| ruff | 显式`--isolated --target-version py312 --select F401,F841,ARG`；只列候选，不改项目lint规则 |
| vulture | 可用时运行`--min-confidence 100`，原始输出留档；自身输出不能越过死代码四门禁 |

复杂度和重复使用自带、版本化的等价算法；不需要安装radon/jscpd。重复指标是行首窗口能够证明的重复token比例，是保守下界，不能与jscpd语义或不同窗口口径直接比较。忽略注释、排版和docstring，保留名称及其他字符串。

优先查找现有命令；Python工具也可通过既有解释器的隔离模块模式运行。每轮记录可用性、版本、命令、退出码、stdout/stderr。缺失工具不自动安装，缺证据不填0。仓库现有scripts/guardrails_check.py拥有公开面解析，本流程复用其declared_public_names；不复制公开API规则。scripts/generate_dependency_graph.py继续拥有依赖图；新增模块按仓库守卫刷新生成文档。不存在的tools目录不虚构为已复用能力。

全仓存在动态取名、注册、星号导入等风险时，不能仅凭函数所在文件没有入口语法就确认非入口；保留未知门禁。coverage只检查函数体命中，排除def声明行；缺少可执行行证据也不能确认零命中。四个过度设计代理为单一显式实现ABC/Protocol、静态单取值布尔开关、单调用点转发包装、单调用点常量实参通用参数。它们始终只作低置信候选，不等同应当删除。

## 基线与评分历史

[实践基线](CODE_ENTROPY_BASELINE.md)拥有官方来源、阈值和项目AGENTS.md / LESSONS.md约定。扫描读取冻结main中的基线并记录摘要；目标没有该文件时明确使用初始化时的基线副本。未支持的基线版本阻塞扫描，日游标的基线摘要变化要求显式reset-cursor。

维护者每周或按需通过Git diff更新基线，递增版本并记录日期、理由和验证。扫描不现场联网查最佳实践；新问题类型才另行查官方来源并先沉淀基线，再启用规则。不让模型现场发明新规则。

每轮保留runs/<run-id>/下的scorecard.json、report.md、candidates.json、muted.json和编号工具原件；有coverage时另保留规范化的行/分支证据。临时源码快照在该轮结束后清理，冻结源码可由mirror的SHA重建。scorecards.jsonl每轮追加一行；失败记录保留状态和缺口，不伪装成完成。

评分卡字段包括：时间、run ID、main/base SHA、模式、基线版本/摘要、扫描器代码摘要、解释器、预算、文件范围、LOC、死代码确认/候选数、不可达/未覆盖分支、复杂度分布及>10函数数、重复token比例、未用配置确认/候选数、工具可用性、静音数和issue投递状态。null是未测或门禁未证实，0是相应口径已测无发现。不制造任意加权总分。趋势只比较相同模式、范围、基线和算法；日扫数字不能当作全仓总量。

## 决策与issue

稳定key由规则、相对路径和限定符号/结构身份做SHA-256摘要，不含行号或commit。不可达分支包含函数限定名；重复片段使用稳定文件对与token摘要，不因日周模式选中另一端而改变key。文件重命名可形成新身份，是v1限制。

用户或模型通过accept判断是否值得处理，必须引用本轮候选并绑定证据摘要和理由。客观门禁不通过的候选不能被接受升级。第一阶段没有自动启动模型会话或授予模型终端权限。decisions.json永久保留mute及理由，无过期时间；静音优先于接受和去重，显式unmute会清掉旧接受，须重新裁决。

投递前读取专属标签下open/closed issue的marker去重。标签须预先存在；脚本不创建或修改标签。每轮最多创建3条，正文含工具名、原始片段、文件:行、main SHA固定链接、基线与裁决理由。创建前持久化pending；超时或返回不明不盲重试。成功也须独立读回正文marker和标签，再记为published。创建失败、URL不明、回读失败或不匹配、未解决pending、额度不足均阻止本轮完成及日游标推进。--publish在无新候选时也核对未静音pending；只靠远端读回消解，不自动删除、关闭或重发。

## 手动运行与证据导入

使用既有Python 3.12及已安装工具，不安装依赖。初始化只创建新流程自身的状态，不安装launchd：

```bash
entropy_state="$HOME/Library/Application Support/options-monitor/code-entropy-review"
python3.12 scripts/code_entropy_review.py --state-dir "$entropy_state" init --repository OWNER/REPO
python3.12 scripts/code_entropy_review.py --state-dir "$entropy_state" run --mode weekly
python3.12 scripts/code_entropy_review.py --state-dir "$entropy_state" run --mode daily
python3.12 scripts/code_entropy_review.py --state-dir "$entropy_state" history --limit 12
```

默认手动运行不建issue；确认客观门禁和裁决后加--publish才投递。接受和永久静音的入口：

```bash
python3.12 scripts/code_entropy_review.py --state-dir "$entropy_state" accept KEY --run-id RUN_ID --reason '工具事实和处理收益已核对'
python3.12 scripts/code_entropy_review.py --state-dir "$entropy_state" mute KEY --reason '已承认，决定保留'
python3.12 scripts/code_entropy_review.py --state-dir "$entropy_state" unmute KEY --reason '明确重新考虑'
python3.12 scripts/code_entropy_review.py --state-dir "$entropy_state" reset-cursor --reason '已复核基线变更，重新建立起点'
```

coverage不自动跑业务测试，避免外部副作用和资源争用。已有成功测试产生的branch coverage JSON可用--coverage-manifest导入。manifest包含main_sha、整型exit_code=0、非空命令数组command、coverage_json文件路径、coverage_sha256；绝对源码路径还需source_root。验证源码SHA、文件摘要、分支元数据和行/分支类型；拒绝未绑定或失败工作负载。测试未命中不代表生产永不执行。

## launchd预览与待确认安装

仓库的两份模板位于scripts/launchd/，`render-plists`仅在自身plist-preview目录渲染，不写LaunchAgents，不调用launchctl：

```bash
python3.12 scripts/code_entropy_review.py --state-dir "$entropy_state" render-plists
```

默认渲染runner指向自身installed目录；确认安装后才将main上的两份扫描模块、guardrails解析模块及所需模板复制到那里并留摘要回执。预览不是安装成功证明，路径尚未落地时不能加载任务。

日扫每天04:20，墙钟上限900秒；周扫周日01:20，上限5400秒，均为本机时区。单个子进程另受剩余预算和300秒上限约束，超时终止自身进程组；进程内静态分析也受总墙钟时限。两任务RunAtLoad=false、ProcessType=Background、LowPriorityIO=true、Nice=15、无KeepAlive。优先级设置不是CPU/内存硬配额。

这些时间避开已只读核对的code-health每天09:17；launchd可能补跑错过的日程，因此不能承诺绝对不重叠。未获得用户对完整plist的确认前，不复制到LaunchAgents、不bootstrap/load。确认后的安装须先plutil lint、再加载并用launchctl print独立回读；不调整code-health来迁就新任务。

## 验证

回归入口为[tests/test_code_entropy.py](../tests/test_code_entropy.py)，覆盖四门禁及未知值、四个真实AST代理、动态入口、稳定key、永久静音、Git真实增量范围、周扫游标隔离、失败续跑、基线绑定、issue去重/不明结果/读回、CLI入口、预算和plist预览。提交前运行Ruff、定向pytest、文档/敏感资料守卫、依赖图和git diff --check；合并仍须必需CI通过。VERSION不变，不发版。

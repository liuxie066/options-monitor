# 代码熵实践基线

版本：2；维护日期：2026-11-11；规则 ID：`entropy-v1`。每周复核或按需提交 diff，递增版本并填写变更理由；扫描器不自行联网更新。

## 来源与项目约定

- [Python PEP 8](https://peps.python.org/pep-0008/)：清晰、局部可读；风格建议不自动构成问题。
- [Python AST](https://docs.python.org/3.12/library/ast.html)：静态结构的解释基础；不导入被扫描业务模块。
- [ruff F401](https://docs.astral.sh/ruff/rules/unused-import/)、[F841](https://docs.astral.sh/ruff/rules/unused-variable/)、[ARG](https://docs.astral.sh/ruff/rules/#flake8-unused-arguments-arg)：只运行这些固定熵规则，不修改仓库已有 lint 规则。
- [coverage JSON](https://coverage.readthedocs.io/en/7.17.2/commands/cmd_json.html)：未命中是给定工作负载的事实，不是不可达证明。
- 项目 [AGENTS.md](../AGENTS.md)、[LESSONS.md](../LESSONS.md)、[Guardrails](GUARDRAILS.md)：保留 canonical owner、公开合同、动态入口、刻意冻结的投影器和用户已承认的不修项；不为理论边缘情况增设抽象。

以上是规则来源引用，不代表本轮联网复核。版本 2 固定这些来源和下面的本地代理，后续变动通过 Git 审阅。

## 判定合同

死代码必须同时有四个明确肯定事实：零引用、绑定本轮源码与成功测试工作负载的零运行时命中、不是公开 API、不是配置/插件/反射入口。未知一律不能通过。vulture 零引用自身不够；可用时它只作为零引用的第二证据来源：内置全仓索引与 vulture 的 `unused function/method` 必须同时判定零引用，候选才能成立；vulture 不报告的函数不会因此被新增。vulture 不可用时退回内置全仓索引并在 gaps 说明。AST/全文检索有任意同名使用即保守排除。全仓存在动态取名、注册或星号导入风险时，不能只检查本文件就确认非入口，只列候选且门禁为未知。零命中只看函数体的可执行语句，排除def声明行；缺少行信息不确认。

过度设计只在有代理时进入候选：显式单一实现 ABC/Protocol、静态布尔开关只有一个观察值、单调用点转发包装、单调用点且传入常量的通用化参数。代理不能证明值得删，不自动创建 issue。

圈复杂度优先由 `radon 6.0.1` 的 `cc --json` 逐函数测量；它把lambda体内的控制流计入所属函数。radon 不报告定义在函数内部的局部类的方法，这些函数逐条回退到 `entropy-ast-v1`：每个def/async-def函数基数1；if/for/async-for/while/if-expression/except/comprehension每项+1，bool额外操作数+1，comprehension filter+1，非默认match case+1，match guard+1；嵌套函数独立计数，外层不计嵌套函数/类/lambda的控制流，lambda本身不计入函数分布。报告分布1–5、6–10、11–20、>20，严格>10为复杂候选；回退数量记入评分卡。

`entropy-token-v1` 精确重复：忽略注释、排版和docstring，保留标识符/其他字符串；以行首50个有效token作匹配种子，再向两侧延长相同片段，最后确认整段至少50 token且在两个文件中都跨至少10行。不能用恰好50 token的种子本身不足10行来排除长克隆。重复率为能证明重复的唯一token位置 / 扫描范围内全部有效token位置；重叠窗口不重复计数。这是保守下界，不是语义重复率，也不与jscpd不同口径混比。jscpd 是 Node 工具，本仓为纯 Python，不采用；重复率继续使用本算法。

不可达分支只证明字面常量 `if False` / `while False` 的正向分支。coverage 未覆盖分支单列。未用配置静态候选不排除动态键访问，因此未用配置确认数保持未知。

高置信度 issue 还需要绑定证据摘要的接受裁决和理由。模型判断不得把缺失 gate 改成通过；永久静音优先于裁决和去重。所有缺失工具或证据用 null/原因呈现，不能填 0。

## 维护记录

| 版本 | 日期 | 理由 | 验证 |
|---|---|---|---|
| 1 | 2026-11-10 | 建立独立熵扫描；固定代理、动态入口未知门禁及日周稳定key；不以测试未覆盖冒充不可达 | 回归入口：[test_code_entropy.py](../tests/test_code_entropy.py)；CI、首扫及安装分别以回执为准 |
| 2 | 2026-11-11 | 接入vulture作为零引用第二证据、radon为复杂度权威测量（局部类方法逐条回退）；周扫改用快照内一次性venv自测coverage；jscpd不采用 | 回归入口：[test_code_entropy.py](../tests/test_code_entropy.py)；依赖固定于requirements/dev.txt与constraints/dev.txt |

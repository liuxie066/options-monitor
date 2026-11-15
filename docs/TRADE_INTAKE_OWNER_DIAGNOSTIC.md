# Trade-intake 临时owner对象诊断

本单元仅本地开发/隔离验证/审核；未部署、未采生产对象。默认关闭，不是内存优化、全heap/native/allocator profiler，也不提供2Gi容量保证。当前开发基线是合并CPU PR #1189后的`9f8e15dde66911582ce995ab66ea18222c1f3941`，独立分支`diag/trade-intake-owner-top-20261115`；不修改原分叉main或已合并CPU工作树。

## 范围和默认关闭

`OwnerDiagnostic.from_environment()`只在Linux且显式设置`OM_OWNER_DIAGNOSTIC_SOCKET`为固定`/run/om-owner-diagnostics-<effective_uid>/om-owner-top-*/owner.sock`规范绝对路径（拒`..`和额外层级）时创建一次性客户端；未设置时返回None，不做诊断文件/socket操作、不读取业务对象。没有新线程/服务/持久化诊断日志。未来源发布以`python -B -m src.application.trades.owner_diagnostic --socket ... --pid ... --start-ticks ...`从受控release目录显式运行接收入口；`scripts/receive_owner_diagnostic.py`只是本地开发薄wrapper，被既有export-ignore排除，不依赖它出现在安装包中，不扩大发布白名单。环境项只包含临时诊断端点路径，不存凭据；不改变systemd-creds合同。不能把开发授权当成设置生产环境项的许可。

服务在已有自然heartbeat完成、`inbox_wakeup.wait(1)`前检查一次。多source共用一个短claim门闩，门闩释放后才取快照/遍历；不持业务RLock/SQLite事务。只允许**一个source**赢得一次连接尝试，之后成功或失败均done、不重连。启用但端点未出现时只借既有heartbeat检查，首个自然检查起120秒后停止；不是新轮询线程，也不人为触发业务。

`/run/om-owner-diagnostics-<service_uid>/om-owner-top-<唯一任务标识>/owner.sock`必须由接收器创建，目录同uid/0700、socket同uid/0600，拒绝路径任一层symlink。Linux `SO_PEERCRED`验证同uid，接收器还验证对端PID；请求固定schema/PID/start_ticks/随机request_id，生产端比较自身`/proc`启动身份。无任意代码、取根表达式、任意路径读写或自定义budget请求。

只读取三根：进程级Brief校验缓存、赢得请求的source inbox缓存、该source lifecycle缓存。其他source、活跃scope、所有SDK/native/allocator及其他模块**未覆盖**。inbox还有push线程写者，但三个顶层cache均只替换entry/clear，已发布entry的嵌套图在当前消费者中无原位写者：采用短`exact dict.copy()`浅快照保留引用，不遍历正在变化的顶层dict。Brief cache超过256、source cache超过1或非exactdict时拒采。浅快照可能延长已有大对象存活时间；不能将它当作零内存操作。未来若引入嵌套原位写者，这个合同必须重新核查，不能临时加业务锁。

输出固定`coverage=brief_cache_and_one_source`与`metric=snapshot_reachable_bytes`，防止把局部complete误读成全进程Top；另有每根的entry数、快照容器浅字节、可达类型/数量/大小及三根union按id去重的类型/数量/大小、步数、耗时、元数据估计、complete/partial原因。快照根容器本身计入图，故是“快照可达图”，不等于原dict桶容量、retained/dominator字节或RSS；多个根图不能相加。只计固定内置类型，拒绝自定义`__sizeof__`/类型比较/属性访问；不输出值、id/地址、账户、路径、正文、secret。输出接收器再次严格allowlist，拒绝任意peer JSON字段。缺失/截断不能称完整owner Top；数据不应外推为全生产对象排名。

## 预算与开销

原候选50万节点/32MiB已被本地实测否证为“总增量上限”：去重set跟踪峰值33.60MiB、实现字典去重峰值40.67MiB；完整诊断在32MiB估计停止时跟踪峰值40.68MiB（该隔离校准特意放宽时间至30秒，避免tracemalloc先截断，不能称原2秒默认的生产峰值）。表扩容时新旧桶共存，会在下一检查前越过阈值。

因此默认改为**50000 unique节点、深度64、100ms合作式遍历deadline、8MiB visited/ID/栈估计软阈值**。同一请求内所有根共享预算，不每根重置。不预先list(dict.items())或扩展所有子节点：宽/超大单容器通过迭代器逐项走，超深停止。命中数/深度/时间/元数据/取消/不支持类型/检测到迭代变化均返回partial或失败，不无限补测。

连接/请求接收/输出发送各最多20ms socket超时（读消息使用绝对deadline，慢速滴字节不能无限延长）；请求最多1KiB、响应最多16KiB；Unix stream用一次请求和EOF半关闭定界，不重连。遍历100ms deadline从连接前开始计算；输出与引用释放可能发生在deadline后。接收器整体自然窗口最多120秒，剩余时间用于accept/read，不额外再叠加无限等待。

**上述均不是进程RSS或业务暂停的硬上限**。GIL/OS调度、set/dict扩容、单对象C操作、浅快照引用保留、JSON编码/socket/最终释放均有额外开销；Python无法在任意C操作中合作式打断。元数据估计不包括解释器arena、所有小计容器、root浅快照、输出缓冲或被延长存活的原payload。不能用杀业务PID强制兑现100ms或8MiB。

本机CPython3.12.13/macOS arm64测量：节点上限路径在隔离中临时放宽时间以避免tracemalloc先触发计时截断，完整诊断（浅快照/遍历/编码/真实本地Unix stream/接收器线程）跟踪分配峰值约**5.10MiB**，释放后约数百至几千字节跟踪量；RSS仍可保留约数MiB页。默认100ms时间截断下的tracemalloc峰值更小，但**不能用这个较小数冒充节点上限最坏开销**。这不是Linux3.12.3/x86_64的证明；没有获准现成目标平台环境，未下载、启动VM或安装工具。

未带tracemalloc的默认请求每shape3次，本地完整流程约0.1–100.4ms；近5万但未到节点上限时可能需遍历共享根第二遍，100ms先截止。1ms背景线程最大观测间隔约9ms，只是本地GIL/调度关联探针，不是生产writer等待或绝对暂停上限。样本少，不能报稳定p95。完整逐样本数据、扩容点、RSS/释放和方法见本任务`docs/gateflow/trade-intake-owner-top-20261115/`中的实测报告与原始JSONL；过程产物按仓库规则不跟踪。

## 未来启用与回退runbook（仅文档，所有生产步骤待另批）

1. **绑定可恢复基线**：执行前重新核主机、active symlink、已发布VERSION/source hash、业务PID/start_ticks、service用户/健康/激活状态、timer/auto-upgrade状态、runtime root和service profile。记录authoring config、生成runtime config和systemd-creds的路径/引用及非秘密校验依据；不复制秘密、真实账本或业务正文到研究目录。核已保留的原release/venv与插件完整可运行，`om update rollback`默认preview能够选择它。旧4.6.37只是历史目标，不能在执行时盲用。
2. **确认启用前关闭**：无诊断环境项、无接收器端点，默认函数返回None；源版本无临时诊断或禁用时不产生连接。仅本地开发版不能覆盖生产4.6.37目录或借其VERSION冒充同一release；未来发布/部署需要独立明确授权及受控、可回退版本身份。先预览实际apply/restart/rollback操作与服务影响，不能将本地审核pass当部署票。
3. **一次自然窗口**：普通service uid不能直接在root:0755的`/run`建目录；不能改用root接收器，因为peer/目录uid必须与service一致。未来先单独明确授权管理员bootstrap唯一固定base `/run/om-owner-diagnostics-<service_uid>`：仅不存在且`/run`已核tmpfs/non-symlink时，按核定uid/gid创建exact目录、chown为service用户、mode0700；已有base只能核对dir/non-symlink/uid/0700，不自动chown或覆盖不匹配目录。此为额外生产目录操作，本轮未执行。接收器以service用户在该base内独占创建本次`om-owner-top-<id>`子目录（已有child拒绝），创建socket；代码在mkdir child之前验证base，env/CLI只接受当前effective_uid固定layout。接收器在SSH前台运行，stdout流回本地0600文件，不走journal/审计/源根盘日志；只向本机Unix socket通信。绑定目标PID/start，生产设置环境项和所需受控重启须明确批准；重启会丢当前堆，需自然预热后新PID的可比窗口，不能追溯原609MiB。未连接、peer/身份错误或读取超时只消耗本次请求，业务照常，不触发回填/对账/通知。
4. **停止新请求/取消**：接收器正常返回、Python异常展开或Ctrl-C会在finally尝试清端点；SIGTERM/SIGHUP、SSH进程硬中断或崩溃不保证finally，直接按下一条遗留端点核验分支处理；尚未连接的客户端继续自然检查到120秒窗口届满，不能重新创建相同端点当隐式重试。已经连接但接收器退出时，生产客户端有界recv/send错误后done。已经开始遍历时，断开接收器不保证即时中断遍历；已有stop_event取消在每步检查，但不能为诊断取消而停止整个业务服务。最迟合作式deadline后退出遍历，原生调用/调度/释放延迟不保证上界；不得发送SIGKILL/SIGUSR或强制GC。
5. **精确临时资源归属**：接收器只删除它创建且inode仍匹配的socket和仍匹配的本次空child目录；固定bootstrap base不是本次资源，默认不删除。若以后确认无active owner/child且另获清理授权，才可单独移除精确空base；不删替换对象或非空目录，不动业务数据。若进程崩溃留下目录，后续人工核对任务ID、owner、dev/inode、无活动接收器和连接后，另获清理授权才删精确端点；不可`rm -rf /run/...`广泛清理。生产客户端不写临时脚本/heap/log，不创建source文件。
6. **撤除代码不是done标志**：done仅阻止当前进程再次采集。未来恢复必须去除本次诊断环境覆盖（只移除本项/本次drop-in，不动secret或其他配置），用项目支持的`om update rollback --repo-root <current-symlink> --runtime-root <bound-runtime> --to-version <verified-published-baseline>`先preview，核planned operations和影响，再凭明确授权加`--confirm`执行受控回退/重启。需要保留原timer激活状态时按已有`--preserve-activation-state`合同，不自行关闭auto-upgrade timer。不能无版本原地热改代码/手动拨symlink来“卸载”。
7. **恢复验证**：独立readback active symlink/VERSION/hash、解释器/插件、service健康/新PID-start、诊断开关关闭、无本次端点/连接，以及现有update verify/恢复状态。原ledger、幂等水位、outbox/pending/inflight只走原恢复合同，不清队列/改SQLite或手工推进通知。观察自然恢复摘要，发现积压/通知不明状态按原runbook处理，不用诊断重放业务。
8. **失败分支**：部分安装或安装失败不等于已切换，先读实际active/profile与原回执，再走受控补偿；接收失败/超时/预算截断仅得到unknown/下界，无自动重试或预算提高；auto-upgrade若在窗口改变active版本/PID则停止本次归因、保留已得结果为旧版本，不能自动关闭timer或强行回退。回退preview不安全、原release不可用或需要新权限时停止报告，不能跳过保护。

本轮没有发布、生产安装/重启/附着/注入、生产socket、生产对象Top采集、提交或推送。下一步是用户审阅实测与回退，再决定是否授权指定版本/环境的交付和取证；不是自动进入cache改造。

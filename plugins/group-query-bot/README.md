# 群查询bot

可选插件 `group-query-bot`（distribution `om-group-query-bot-plugin`），必须绑定独立的新飞书应用机器人。S1 最小接入预检、S2 五方法 API 与 S3 自然语言、会话存储和可选服务生命周期的本地实现均已完成；真实个人群接入、模型/行情真实联调和部署尚未验证。

普通 OM 安装不选择本插件，也不启动新服务。插件导入及命令 help 不启动网络、解析凭据或创建数据目录。完整合同见 [群查询bot设计](../../docs/GROUP_QUERY_BOT_DESIGN.md)。

## 源码开发与预览

在本工作区的独立 Python 环境显式安装源码包。加载器仍检查 distribution 和版本，不回退到仓库源码目录：

```bash
./.venv/bin/python -m pip install ./plugins/group-query-bot
./om group-query-bot preflight --config /absolute/path/preflight.json
```

尚未发布的开发工作区使用以上源码安装。发布后的官方托管安装沿用 `om plugins install group-query-bot --confirm` 的精确 Release/资产校验，不用旧 Release catalog 启动新插件预检。

### worktree 里的测试环境

主仓库 `.venv` 里安装的 `om_group_query_bot` 可能来自另一个 checkout（见
`site-packages/om_group_query_bot_plugin-*.dist-info/direct_url.json`），在
worktree 里直接跑测试会导入旧副本。为当前 worktree 建独立环境，并让插件包
跟随本树源码：

```bash
python3 -m venv --system-site-packages .venv
./.venv/bin/pip install -r requirements/dev.txt -c constraints/dev.txt
SP=$(echo .venv/lib/python*/site-packages)
rm -rf "$SP/om_group_query_bot"
ln -s "$PWD/plugins/group-query-bot/src/om_group_query_bot" "$SP/om_group_query_bot"
./.venv/bin/python -m pytest tests/test_group_query_bot_*.py -q -p no:cacheprovider
```

`pip install` 保留 distribution 元数据（加载器会校验 distribution 与版本），
符号链接让后续源码编辑无需重装。其余插件按 `requirements/dev.txt` 的副本安装，
preflight 等跨插件测试因此可用。

预检配置为独立 JSON，字段必须完整且不能增加其他段：

```json
{
  "instance_id": "personal-group-test",
  "data_dir": "/var/lib/options-monitor-group-query-bot/personal-group-test",
  "feishu": {
    "app_id": "cli_new_application",
    "app_secret_ref": "group_query_bot.feishu.app_secret",
    "allowed_chat_ids": ["oc_target_group"]
  }
}
```

默认返回 `live_not_run`，只读取指定配置并验证，零凭据读取、零网络、零数据目录写入。配置不接受 App Secret 明文、model/query_api 段、账户或其他 bot 凭据引用；data_dir 必须为管理员显式指定的绝对路径。不读取 OM config.yaml、生产配置或 home 下的默认配置。`allowed_chat_ids` 可写一个或多个具体群 ID，也可只写 `["*"]` 表示接受任意群；通配必须是唯一项，不能与具体群混用，群范围改由飞书侧「谁可邀请 bot 入群」控制。

## 经授权的真实预检

先由管理员创建新应用、满足飞书认证/共享/发布条件并加入指定个人群；普通 webhook 机器人不能代替应用机器人。安装既有 `feishu` 插件及其 SDK，并通过已有安全凭据体系交付 `group_query_bot.feishu.app_secret`。不在配置、命令、聊天或报告里填写真实凭据。

只有明确授权该应用与目标群测试后，才由操作人员执行：

```bash
./om group-query-bot preflight --config /absolute/path/preflight.json --confirm
```

程序通过应用 token 调用固定机器人信息接口取得 bot open_id，再启动既有飞书长连接。仅接收配置群范围内（具体白名单或唯一通配 `["*"]`）@当前 bot 的文本事件，回复原消息固定文本“群查询bot 接入测试已收到”。不调用模型、行情、账户或查询 API。SDK 回调只进入容量 16 的队列；单 worker 处理，满载拒绝并记录计数。

Ctrl+C 停止接收新工作；正在进行的不可取消 I/O 可能继续，返回 stopped_with_pending_io 时不能宣称请求已经终止。已占用但未完成的投递记录保留原状态，重复事件不会自动重发。

## bot 与 API 进程（本地实现）

正式入口为两个独立进程，各自使用独立配置、实例锁和数据目录；bot 进程不会加载行情或 position-advice 依赖：

```bash
./om group-query-bot serve --config /absolute/path/bot.json --confirm
./om group-query-bot api-serve --config /absolute/path/api.json
```

`serve` 在 `data_dir/bot.lock` 持有实例锁，`api-serve` 在 `data_dir/api.lock` 持有实例锁；同一实例重复启动被拒绝。配置不完整、凭据缺失或地址非回环时进程不启动，也不回退到环境变量或 OM 账户配置。

bot 配置字段固定为 `instance_id`、`data_dir`、`feishu`（`app_id`、`app_secret_ref`、`allowed_chat_ids`）、`model`（`provider`、`model`、`base_url`、`api_key_ref`、`context_window_tokens`、`max_output_tokens`、`timeout_seconds`）和 `query_api`（`base_url`、`token_ref`）；`base_url` 只能是 `http://127.0.0.1:<port>`。三个引用必须是 `group_query_bot.feishu.app_secret`、`group_query_bot.llm.api_key`、`group_query_bot.query_api.token`。`allowed_chat_ids` 同样支持唯一通配 `["*"]`。可选 `limits` 默认每用户 3/min、每群 10/min、用户并发 1、bot 并发 2、回合 90 秒、模型 4 次、API 3 次、输出 2000 tokens、预留 32000 tokens、历史 10 轮/4000 tokens、API 超时 30 秒。模型只能使用 `query_quotes`、`query_candidates`、`evaluate_close`、`evaluate_roll_down`、`evaluate_roll_up` 五个固定工具，群/发送者/线程/截止时间/输入身份由宿主填写；关键数值由结构化结果渲染，模型只解释或补参。

API 配置字段固定为 `instance_id`、`data_dir`、`api`（`bind_host` 必须为 `127.0.0.1`、`bind_port`、`token_ref`、`app_id`）、`principal`（`principal_id`、`capabilities`、`markets`、`account_scope` 必须为 `[]`）、`quote`、`max_budget_seconds`、`public_policy`；可选 `principal_requests_per_minute` 默认 30。API 只接受固定五个回环路由，按服务端能力、市场和账户范围授权，不接受账户、持仓、收益或报告参数。

bot 侧会话保存在 `data_dir/conversation.sqlite3`：`messages` 保存按 `turn_id` 分组的用户、工具结果和回复正文，`scenario_inputs` 按版本保存待补参数和已确认输入，`outbox_payloads` 保存待发送正文，`audit` 只保存状态与原因码；`deliveries` 只保存 7 天最小投递元数据。正文、输入、outbox 和审计最多 24 小时，且不得超过其原始消息的到期时间；派生到期取自身 24 小时与全部来源到期时间的较早者，新消息、修改 limit、摘要或重启都不续期。outbox 正文到期只停止重发，不改变原投递状态。读取一律先按 scope 和到期时间过滤；每轮开始和周期维护执行可重复清理，清理失败也不放行过期读取。

服务安装是可选显式集合：`om service render` 和 `om service lifecycle install` 接受 `--group-query-bot-instances`（指向 JSON 数组文件的路径，每项 `instance_id`、`bot_config`、`api_config`）。默认空集合不渲染任何群查询单元，保持既有服务集合不变。显式配置时为每个实例渲染 bot 和 API 两个单元；bot 单元接收飞书、模型和查询凭据，API 单元只接收查询凭据。安装不启动服务，也不自动部署或升级。

## 续聊与候选条件

首次消息须真实 @ 当前 bot。随后在同一实例、应用、租户、群、发送者及线程内，最近一条成功回复的用户消息接入时间距今未满 600 秒时，可以直接发送没有 mention 的文本续聊。成功回复会续期；发送失败或结果未知不续期。仅 @ 其他对象的消息不处理。历史保留 24 小时，独立于这一短时窗口。

在 bot 配置的 `limits` 中设置 `followup_window_seconds` 可调整窗口，接受 0..3600 的整数；默认 600，0 恢复逐条要求 @。飞书侧必须具备接收普通群消息的权限并确实投递事件，代码放行不能替代平台权限；最小接入 preflight 仍要求逐条 @。

`query_candidates.parameters` 的行权价条件支持三种字段：

| 字段 | 含义 | 组合 |
| --- | --- | --- |
| `strike` | 精确等于 | 与另外两个字段互斥 |
| `min_strike` | 大于等于，包含下限 | 可与 max_strike 组合 |
| `max_strike` | 小于等于，包含上限 | 可与 min_strike 组合 |

三者均可省略，采用公共策略范围；只指定一侧时覆盖该侧边界。显式值须为正有限 JSON 数字，不能传 null、布尔值或数字字符串；下限大于上限时在取数前拒绝。例如查询最大行权价 380 的候选：

```json
{"symbol":"0700.HK","strategy":"short_put","max_strike":380}
```

续聊修改边界时保留标的、方向及其他未修改的用户条件；从精确价改为上下限要清除旧 strike。上下限均包含端点，严格不含端点的要求须先确认是否接受这一规则。

候选查询完成后，由宿主直接渲染实际生效条件、合约、到期日和报价，不再调用模型改写结果。模型仍负责理解自然语言和提取参数；其他工具继续按事实卡与回复校验规则处理。

## 本地记录与验收

`data_dir/preflight.sqlite3` 只保存消息/event ID、scope 摘要、固定 uuid、状态和首次创建/失效时间，保留 7 天。scope 包含实例、应用、header tenant、群、发送者和线程；线程缺省 null，header tenant/发送者缺失拒绝。sender 自身 tenant 单独保留来源证据，不用组织归属决定是否可使用。

状态为 claimed → sending → acknowledged/failed/outcome_unknown。acknowledged 仅表示平台返回成功且带回复 message_id，不证明用户收悉。结果不明、失败和重复事件均不会自动重发；重启不续期或改写原投递状态。

`data_dir/preflight-evidence.json` 保存最多 32 个脱敏身份样本、配置/机器人身份摘要、结果和队列拒绝数。相同 ID 使用一致别名，删除正文、名称、凭据和额外字段。证据批次最多 24 小时；运行时清理、写出前过滤，启动重新建立当前预检报告。正文清理不删除仍未满 7 天的投递元数据。证据不会自动上传。

真实验收还需人工记录新应用创建、目标个人群、平台前提及两名发送者的入群/@收发结果，区分真实 SDK 观察和离线重放。报告固定 `live_g2_verified=false`，不能仅凭程序启动或一条成功回复判定 G2 完成。按 2026-11-10 用户确认的修订顺序，S2/S3 的本地实现和离线审查不再以真实 G2 为前置；真实 G2 仍是最终联调、实际使用和完整验收的前置。

身份查询使用 [飞书机器人信息接口](https://open.feishu.cn/document/server-docs/bot-v3/bot-info/get)；其顶层 bot.open_id 响应也可对照 [飞书官方 CLI 的解析实现](https://github.com/larksuite/cli/blob/main/shortcuts/common/runner.go)。

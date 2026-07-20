# Automatic Release Version Recommendation — Revised Implementation Plan

- Status: proposed
- Date: 2026-07-20
- Review sources:
  - `docs/reviews/plan-review-20260720-094432.md`
  - `docs/reviews/plan-review-20260720-095634.md`
  - `docs/reviews/plan-review-20260720-101250.md`
- Target repo: options-monitor

## 1. Goal

在准备发布时，基于远端最新稳定 release 和标准化 `CHANGELOG.md / Unreleased` 内容，确定性推荐 `major|minor|patch`，向用户展示依据；用户明确确认后，复用现有 `version_update` 写入 `VERSION`。

成功标准：

1. `bump=auto` 默认只读，输出基线、推荐等级、目标版本、声明依据、人工复核标记和 freshness digest。
2. 用户确认采用时重新计算全部证据；工作区、Changelog、基线或远端 tag namespace 变化则拒绝写入。
3. 自动路径只更新 `VERSION`，不修改 Changelog、不 commit、不 push、不创建 tag、不发布、不升级生产。
4. 原有 `patch|minor|major|target_version` 手动路径行为保持兼容。
5. 第一版不声称理解任意源码的业务兼容性；缺少规范化 release intent 时返回 `needs_input`，不猜。

## 2. Non-goals

第一版不做：

- 从任意 Python diff 推断字段语义是否 breaking；
- 自动比较全部 CLI、配置、输出 schema、ledger 或 source-of-truth；
- 自动生成或移动 Changelog；
- 强制所有手动 VERSION write 遵守自动推荐；
- 在 WeChat/Assistant 中建立新的两阶段确认状态机；
- 自动 fetch tag、rebase、commit、push、release 或生产 apply；
- prerelease 推荐。

这些边界避免把启发式结果包装成确定性兼容性证明。

## 3. Product decisions

### 3.1 v1 是 advisory，不是强制发布策略

- `bump=auto` 产生建议。
- 用户可以采用建议。
- 原有显式 `bump=patch|minor|major` 和 `target_version` 保持不变。
- v1 不新增 `override_reason`，不拦截手动路径。
- 若未来要强制 minimum bump，作为独立 public-contract work unit 设计。

### 3.2 支持 surface

v1 支持：

- 本地 `om-agent`；
- Codex/维护者发布流程。

v1 不直接支持 WeChat/Assistant 自然语言两阶段确认。如果未来接入聊天面，必须复用现有 `InboundOperationStore` preview/confirm/cancel/TTL/payload-hash 状态机。

### 3.3 freshness、remote authority 与用户授权分离

- `recommendation_digest` 只证明 recommendation 输入没有变化。
- digest必须绑定 `remote_name` 和该 remote 当时解析出的 fetch endpoint fingerprint，不能只绑定 tag SHA。
- remote原始URL可能包含credential；工具只输出脱敏display value和不可逆endpoint fingerprint，不回显原始URL。
- `confirm=true` 仍是现有低层 Tool Gateway 的显式写入意图参数。
- 工具本身不声称能证明“人类亲自看过并确认”；Codex/CLI 调用方负责先展示推荐并取得用户确认。
- auto apply重复请求若发现 `VERSION` 已等于调用方提交的 expected target，只返回无写入的 `already_at_target`；该状态证明当前结果，不声称证明上一请求一定成功。

## 4. Authoritative inputs

### 4.1 版本基线

基线必须来自调用方指定remote（默认 `origin`）的最高稳定 tag：

```text
vMAJOR.MINOR.PATCH
```

明确排除：

```text
v1.4.0-rc.1
v1.4.0-beta.2
```

remote authority 解析顺序：

1. `remote_name` 必须精确匹配 `git remote` 中已配置的一个remote；
2. 使用 `git remote get-url <remote_name>` 取得Git实际使用的fetch URL；
3. 对原始fetch URL做SHA-256得到 `remote_endpoint_fingerprint`；原始URL不得进入Tool输出或日志；
4. 使用参数数组而不是shell字符串执行：

```bash
git ls-remote --tags <remote_name>
```

preview和apply都必须重新解析remote name、fetch URL fingerprint和tags。remote alias或endpoint fingerprint变化均视为stale/blocked。

稳定 tag 的 raw ref 只接受：

```text
^refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$
```

同名 annotated tag 的 peeled ref 只接受：

```text
^refs/tags/v[0-9]+\.[0-9]+\.[0-9]+\^\{\}$
```

对每个稳定版本保存：

```text
remote_tag_object_sha   # raw ref SHA
remote_commit_sha       # peeled SHA；lightweight tag 时等于 raw SHA
```

解析规则：

1. raw ref 必须存在；
2. 存在 `^{}` ref 时，该 SHA 是 `remote_commit_sha`；
3. 不存在 `^{}` ref 时，raw SHA 同时作为 `remote_commit_sha`；
4. orphan peeled ref、重复且冲突的 raw/peeled ref 或无效 SHA 均 fail closed。

### 4.2 本地基线可用性

选出远端最高稳定 tag 后：

1. 本地必须存在对应 tag/commit object；
2. `git rev-parse refs/tags/vX.Y.Z^{commit}` 必须等于 `remote_commit_sha`；
3. 该 commit 必须是当前 `HEAD` 的 ancestor；
4. 当前 `VERSION` 必须等于该稳定版本；
5. 目标版本 raw/peeled ref 在远端均必须不存在。

任一失败均返回 `blocked`，不自动 fetch、不自动 rebase、不修改仓库。

Detached HEAD 是允许状态，不是 blocker。只要上述 remote identity、local ancestry、VERSION 和 target collision 检查全部通过，preview 和 apply 都可继续。输出必须包含：

```json
{
  "workspace": {
    "branch": null,
    "detached": true
  },
  "review_flags": ["DETACHED_HEAD"]
}
```

并提示后续 commit/push 由调用方选择正确分支或隔离 release worktree完成。

reason codes：

- `REMOTE_NOT_CONFIGURED`
- `REMOTE_ENDPOINT_LOOKUP_FAILED`
- `REMOTE_ENDPOINT_CHANGED`
- `REMOTE_TAG_LOOKUP_FAILED`
- `NO_STABLE_RELEASE_TAG`
- `BASE_TAG_NOT_AVAILABLE_LOCALLY`
- `REMOTE_TAG_IDENTITY_INVALID`
- `REMOTE_LOCAL_TAG_MISMATCH`
- `BASE_TAG_NOT_ANCESTOR`
- `VERSION_BASE_MISMATCH`
- `TARGET_TAG_ALREADY_EXISTS`
- `UNSUPPORTED_PRERELEASE_VERSION`
- `MALFORMED_UNRELEASED_SECTION`
- `UNSUPPORTED_UNRELEASED_CONTENT`
- `UNRELEASED_IMPACT_REQUIRED`
- `EVIDENCE_LIMIT_EXCEEDED`
- `EVIDENCE_UNSUPPORTED_FILE_TYPE`
- `EXPECTED_RECOMMENDATION_MISMATCH`
- `RECOMMENDATION_STALE`

### 4.3 Release intent

自动分类唯一 authoritative semantic input 是 `CHANGELOG.md` 中唯一的 `## Unreleased` 区段。

v1使用严格、有限、fail-closed的line grammar，不引入通用Markdown AST：

1. 文档中必须恰好存在一个精确的 `## Unreleased`；缺失或重复均返回 `needs_input / MALFORMED_UNRELEASED_SECTION`；
2. 区段从该标题下一行开始，到下一个 `## ` 标题或EOF结束；
3. 允许空行；
4. 只允许以下三级标题，且每个标题最多出现一次，顺序不限：

```markdown
### Breaking Changes
### Added
### Changed
### Fixed
```

5. release item必须是当前受支持三级标题下，以 `- ` 开头且同一行含有非空文本的单行bullet；
6. 未知标题、标题外bullet、重复受支持标题、code fence、普通段落、嵌套列表或其它非空内容均返回 `needs_input / UNSUPPORTED_UNRELEASED_CONTENT`；
7. parser不能静默丢弃任何非空 `Unreleased` 内容。

规范示例：

```markdown
## Unreleased

### Breaking Changes
- Removed the legacy CLI flag.

### Added
- Added automatic release-version recommendation.

### Changed
- Changed the release confirmation prompt.

### Fixed
- Fixed version metadata validation.
```

如果语法合法但没有任何bullet：

```text
status=needs_input
reason_code=UNRELEASED_IMPACT_REQUIRED
```

不得默认回退到patch。若未来需要支持 `Removed|Deprecated|Security` 等新分类，必须先为其定义明确SemVer映射和contract tests；v1不做猜测。

## 5. Classification policy

### 5.1 Deterministic precedence

```text
Breaking Changes > Added > Changed/Fixed
```

规则：

| Unreleased 内容 | 推荐 |
|---|---|
| 至少一个 `Breaking Changes` bullet | `major` |
| 无 Breaking，至少一个 `Added` bullet | `minor` |
| 只有 `Changed` 和/或 `Fixed` bullet | `patch` |
| 语法合法但没有任何 bullet | `needs_input` |
| 出现任何未知或无法归属的非空内容 | `needs_input` |

目标版本继续复用现有 `bump_version()`：

```text
1.3.0 + patch -> 1.3.1
1.3.0 + minor -> 1.4.0
1.3.0 + major -> 2.0.0
```

### 5.2 Declaration status and review flags

Git change paths只用于提示人工复核，不直接改变推荐等级。

第一版敏感范围：

```text
src/interfaces/**
src/application/agent_tools/**
src/application/config*.py
src/application/layered_config.py
domain/domain/ledger/**
src/application/positions/**
src/application/trades/**
src/application/service_upgrade.py
src/application/service_deploy.py
scripts/install.sh
scripts/python_runtime.sh
src/__init__.py
.github/workflows/**
```

所有成功recommendation的 `recommendation` object必须包含：

```json
{
  "classification_basis": "changelog_unreleased",
  "declaration_status": "complete",
  "manual_review_required": true
}
```

`review_flags` 始终是data顶层字段；human-readable warning复用现有Tool envelope顶层 `warnings`，不在data内重复。命中敏感路径时：

```json
{
  "data": {
    "review_flags": [
      "COMPATIBILITY_SENSITIVE_PATH_CHANGED"
    ]
  },
  "warnings": [
    "compatibility-sensitive files changed; confirm Unreleased impact classification"
  ]
}
```

字段唯一层级以8.3的完整schema为准。

不输出 `confidence=high|medium|low`。该工具只能证明声明已按规则解析，不能证明维护者对业务兼容性的判断正确。无法分类时直接 `needs_input`。

## 6. Git evidence and digest

### 6.1 Evidence collection

在 baseline commit 与当前工作区之间收集：

- `HEAD` commit；
- tracked name-status，开启 rename detection；
- baseline-to-worktree tracked content digest，排除顶层 `VERSION`，因为 VERSION作为独立字段绑定；
- staged/unstaged状态只用于展示，不进入digest；仅执行 `git add`/`git reset` 而最终tracked content不变时，recommendation不失效；
- untracked file paths and bounded content digests；
- `Unreleased` canonical text；
- current `VERSION`；
- normalized `remote_name`；
- `remote_endpoint_fingerprint`；
- remote highest stable tag/version；
- remote raw tag object SHA；
- remote peeled/commit SHA；
- local resolved baseline commit SHA；
- recommended bump and target version。

### 6.2 Untracked limits

为避免读取超大或异常文件：

- 最多 256 个 untracked files；
- 单文件最多 1 MiB；
- 总计最多 10 MiB；
- 只接受regular file；symlink、socket、device或其它文件类型直接返回 `blocked / EVIDENCE_UNSUPPORTED_FILE_TYPE`，不跟随、不做path-only freshness；
- 路径必须resolve在repo root内；
- 超限返回 `blocked / EVIDENCE_LIMIT_EXCEEDED`。

ignored files不参与。

### 6.3 recommendation digest

对 canonical JSON 做 SHA-256：

```text
schema_version
normalized remote_name
remote endpoint fingerprint
remote stable tag + version
remote raw tag object SHA
remote peeled/commit SHA
local resolved baseline commit SHA
HEAD commit
current VERSION
tracked content digest excluding top-level VERSION
untracked path/content digests
canonical Unreleased content
recommended bump
target version
```

字段名：

```text
recommendation_digest
```

不得称为 confirmation token 或 authorization receipt。canonical JSON必须使用固定schema version、UTF-8、sorted keys和确定性数组排序；remote原始URL不得进入可见output。staged/unstaged placement不属于recommendation freshness。

## 7. State transitions

```text
analyze
  ├─ blocked          # remote/base/git/evidence failure
  ├─ needs_input      # Unreleased缺失、未知或未声明影响
  └─ recommended
         ├─ user declines -> no write
         └─ user confirms
                ├─ VERSION already equals expected target -> already_at_target, no write
                ├─ expected base/target mismatch -> stale, no write
                ├─ recompute/digest/remote endpoint mismatch -> stale, no write
                ├─ remote target now exists -> blocked, no write
                └─ applied -> atomic VERSION write
```

### 7.1 Preview

调用：

```json
{
  "bump": "auto",
  "apply": false,
  "remote_name": "origin"
}
```

### 7.2 Apply recommendation

调用：

```json
{
  "bump": "auto",
  "recommendation_digest": "sha256:...",
  "expected_base_version": "1.3.0",
  "expected_target_version": "1.4.0",
  "apply": true,
  "confirm": true,
  "remote_name": "origin"
}
```

Apply 顺序：

1. 通过现有 write permission gate；
2. 校验 `recommendation_digest`、`expected_base_version`、`expected_target_version` 均存在且格式合法；
3. 读取current VERSION；若已等于 `expected_target_version`，不执行任何写入，返回 `status=already_at_target, changed=false`，并明确该状态只证明当前文件已达目标，不证明上一调用一定成功；可附带当前remote target状态，但remote collision不得把这个no-op改成写失败；
4. 若current VERSION不等于 `expected_base_version`，返回 `stale / EXPECTED_RECOMMENDATION_MISMATCH`；
5. 重新运行完整recommendation，重新解析 `remote_name`、endpoint fingerprint、stable tag raw/peeled identity和全部content evidence；
6. recomputed base/target必须分别等于expected base/target，否则stale；
7. 比较digest；
8. 再次查询remote并确认endpoint fingerprint、baseline raw/peeled/local identity仍一致；
9. 再次确认target raw/peeled tag均不存在；
10. 原子写入 `VERSION.tmp -> VERSION`；
11. 返回 `status=applied, changed=true`。

`already_at_target` 是幂等recovery结果，不是authorization receipt。网络、Git、endpoint或解析失败均fail closed；只有第3步的纯本地no-op recovery可以在不重新写VERSION的情况下返回当前结果。

## 8. Tool contract

扩展现有 `VERSION_UPDATE_TOOL`，不新增 Tool。

### 8.0 Requirements decision

`bump=auto` 必须访问 Git remote，而手动 `patch|minor|major|target_version` 仍可离线执行。当前 `AgentTool.requires` 只支持静态声明，不支持按 payload 动态声明。

v1 采用保守的静态 superset：

```python
requires=("local_repo", "git_remote")
```

这只改变 manifest 中声明的最大依赖，不改变手动路径的运行行为：手动路径不得执行任何 remote command。Tool description、input schema 和 docs 必须明确：`git_remote` 仅由 `bump=auto` 使用。

对应 contract tests 必须证明：

1. manifest包含 `local_repo` 和 `git_remote`；
2. 离线手动 patch preview/apply仍不调用 remote；
3. 离线 auto返回 `REMOTE_TAG_LOOKUP_FAILED` 且不写文件。

### 8.1 Input additions

```text
bump: major|minor|patch|auto
remote_name: optional, default origin
recommendation_digest: required for apply=true with bump=auto
expected_base_version: required for apply=true with bump=auto
expected_target_version: required for apply=true with bump=auto
```

### 8.2 Compatibility

保持不变：

```text
target_version
apply
confirm
allow_downgrade
```

保持现有 safe default：

```json
{"bump":"patch","apply":false}
```

不把 safe default 改成 auto，避免现有 Agent manifest 和调用语义漂移。发布流程需要显式调用 `bump=auto`。

### 8.3 Output

Tool envelope与业务状态必须分离：

- permission、confirm、payload格式等调用错误使用现有 `ok=false,error={code,message,hint}`；
- recommendation可恢复业务状态使用 `ok=true,data.status`；
- static `output_contract` 描述manual/auto两种variant，保证 `om-agent spec` 可发现；mode-sensitive `output_contract_resolver` 在具体payload下返回manual现有shape或 `release_version_recommendation.v1`；
- human-readable warnings只使用现有Tool envelope顶层 `warnings`，data不重复warnings。

推荐成功的唯一字段层级：

```json
{
  "schema_version": "release_version_recommendation.v1",
  "status": "recommended",
  "mode": "dry_run",
  "reason_code": null,
  "base": {
    "remote_name": "origin",
    "remote_endpoint_display": "https://github.com/.../options-monitor.git",
    "remote_endpoint_fingerprint": "sha256:...",
    "tag": "v1.3.0",
    "version": "1.3.0",
    "remote_tag_object_sha": "...",
    "remote_commit_sha": "...",
    "local_commit_sha": "..."
  },
  "workspace": {
    "head": "...",
    "branch": "main",
    "detached": false,
    "dirty": true,
    "changed_files": ["..."],
    "tracked_content_digest": "sha256:...",
    "staged_files": ["..."],
    "unstaged_files": ["..."]
  },
  "recommendation": {
    "bump": "minor",
    "target_version": "1.4.0",
    "classification_basis": "changelog_unreleased",
    "declaration_status": "complete",
    "manual_review_required": true
  },
  "evidence": {
    "breaking_changes": [],
    "added": ["..."],
    "changed": [],
    "fixed": ["..."],
    "sensitive_paths": ["src/application/agent_tools/runtime.py"]
  },
  "review_flags": ["COMPATIBILITY_SENSITIVE_PATH_CHANGED"],
  "recommendation_digest": "sha256:...",
  "write": {
    "changed": false,
    "already_at_target": false
  }
}
```

Auto data statuses及required fields：

| status | Tool envelope | required fields |
|---|---|---|
| `recommended` | `ok=true` | base, workspace, recommendation, evidence, digest, review_flags；human warnings在Tool envelope |
| `blocked` | `ok=true` | reason_code, message, available base/workspace evidence, no write |
| `needs_input` | `ok=true` | reason_code, message, parsed/unsupported Unreleased evidence, no digest/write |
| `stale` | `ok=true` | reason_code, message, expected/recomputed summary, no write |
| `applied` | `ok=true` | recommendation, `write.changed=true`, masked version_path |
| `already_at_target` | `ok=true` | expected target, current VERSION, `write.changed=false`, `write.already_at_target=true` |

`review_flags` 始终位于data顶层；human-readable warning始终位于Tool envelope顶层 `warnings`；`evidence` 不重复这些字段。任何返回的remote URL都必须脱敏，raw URL不进入output。

## 9. User-facing interaction

Codex/调用方负责把结构化结果渲染为：

```text
建议升级次版本：1.3.0 -> 1.4.0
分类依据：Unreleased 声明包含 Added 2 项、Fixed 1 项。
需要人工复核：Agent Tool / CLI 相关文件发生变化。
尚未修改 VERSION。

是否采用 1.4.0？
```

收到用户明确确认后，调用 auto apply。

工具返回 `applied` 后继续提示：

```text
VERSION 已更新为 1.4.0。
CHANGELOG 版本段、测试、commit、push、GitHub Release 和生产升级尚未执行。
```

工具返回 `already_at_target` 时提示：

```text
VERSION 当前已经是 1.4.0，本次没有再次写入。
这证明当前文件已达到目标，不证明上一调用一定成功；后续发布步骤仍未执行。
```

## 10. Ownership and files

### New

`src/application/release_version_recommendation.py`

职责：

- remote stable tag discovery；
- local baseline validation；
- Unreleased parsing；
- sensitive path detection；
- evidence collection and bounded hashing；
- recommendation and digest；
- no writes。

该模块只依赖 `src.application.release_target` 的 SemVer primitives，不得导入 `version_check`。

### Modify

`src/application/release_target.py`

- 接管纯函数 `bump_version()` 和 `BUMP_KINDS`；
- 保持 `parse_version()` / `compare_versions()` / bump calculation 为同一 SemVer truth source；
- 新增独立的 `parse_remote_stable_tag_identities()`（或等价窄类型）解析stable raw/peeled identity；
- 保持现有 `parse_release_tags()` / `select_latest_release()` 的prerelease和tuple facade不变，避免影响version_check/service_upgrade consumers。

`src/application/version_check.py`

- 接受 `bump=auto`；
- preview 时调用 recommendation；
- apply 时重算并验证 digest；
- 继续复用现有 atomic VERSION write。
- 从 `release_target` 导入并 re-export/wrap `bump_version()`，保持现有 import facade 和 tests 兼容。

`src/application/agent_tools/runtime.py`

- 扩展input schema、examples、static union output contract和mode-sensitive output contract resolver；
- 将静态 requirements更新为 `("local_repo", "git_remote")`，并说明remote仅用于auto mode；
- safe default不变；
- 固定auto业务status与Tool `ok=false` error envelope边界。

`src/application/agent_tools/operations_impl.py`

- 转发 `remote_name`、`recommendation_digest`、`expected_base_version` 和 `expected_target_version`；
- 将内部recommendation result适配为唯一v1 output层级，负责URL脱敏和flags/warnings位置。

`docs/RELEASE_PROCESS.md`

- 增加标准化 Unreleased 规则；
- 增加 auto recommend/confirm 流程；
- 明确 advisory 和 non-goals。

`docs/TOOL_REFERENCE.md`

- 更新 Tool schema、status 和示例。

### Tests

新增：

`tests/test_release_version_recommendation.py`

更新：

- `tests/test_version_check.py`
- `tests/test_agent_plugin_contract.py`
- `tests/test_agent_plugin_smoke.py`
- `tests/test_release_test_plan.py`（仅把新模块映射到 release/service focused tests）

不修改：

- production `service_upgrade.py`；
- release workflow；
- inbound operation state machine；
- domain business logic。

## 11. Implementation slices

### S1 — Pure recommendation engine

实现：

- configured remote selector、fetch endpoint fingerprint和stable raw/peeled identity parsing；
- shared SemVer bump primitives迁移到 `release_target` 并保持 `version_check` facade；
- local baseline/ancestry/VERSION checks；
- Unreleased parser；
- deterministic bump classification；
- sensitive warnings；
- bounded digest，排除VERSION后的tracked content与独立VERSION字段；
- strict fail-closed Unreleased grammar。

验证：

- 纯函数单元测试；
- 临时 Git repo integration tests；
- 不接 Tool，不写 VERSION。

Exit criteria：同一evidence重复运行结果完全一致；lightweight/annotated tag均解析到正确remote commit；remote alias/endpoint或remote/local identity mismatch fail closed；任何未知Unreleased内容都不产生recommendation；所有blocked/needs_input状态有明确reason code；现有prerelease tag parser facade测试继续通过。

### S2 — version_update integration

实现：

- `bump=auto` preview；
- auto apply expected base/target和digest validation；
- `already_at_target` 无写入recovery；
- apply前remote endpoint/tag race recheck；
- Tool manifest使用静态superset requirements，同时保持手动模式不访问remote；
- detached HEAD在完整校验通过时允许preview/apply并返回review flag；
- existing manual paths unchanged。

验证：

- Tool contract/smoke tests；
- permission gate tests；
- stale recommendation tests；
- atomic write tests。

Exit criteria：没有用户确认时零写入；response-loss后的相同expected target重试得到确定性no-op结果；旧显式调用测试保持通过；auto output schema所有status通过contract tests。

### S3 — Release workflow documentation

实现：

- Release Process 增加 Unreleased authoring sequence；
- Tool Reference 增加示例；
- release test planner包含新模块 focused tests。

验证：

- docs commands 与实际 spec 一致；
- release preflight 和 dependency graph通过。

## 12. Test matrix

### Classification and strict grammar

1. Breaking + Added + Fixed -> major。
2. Added + Fixed -> minor。
3. Changed only -> patch。
4. Fixed only -> patch。
5. Empty valid Unreleased -> needs_input / UNRELEASED_IMPACT_REQUIRED。
6. Unsupported heading only -> needs_input / UNSUPPORTED_UNRELEASED_CONTENT。
7. Removed + Fixed -> needs_input，不得静默推荐patch。
8. Security + Added -> needs_input，不得静默推荐minor。
9. Heading外直接bullet -> needs_input。
10. Duplicate `## Unreleased` -> needs_input / MALFORMED_UNRELEASED_SECTION。
11. Duplicate supported heading -> needs_input。
12. Code fence内伪heading或普通段落 -> needs_input。
13. Sensitive path + Added -> minor, declaration complete, compatibility review flag。
14. Non-sensitive path + Fixed -> patch, declaration complete, manual review仍为true。

### Baseline, remote authority and Git topology

15. Configured remote latest stable tag available and ancestor -> recommended。
16. Remote lightweight tag raw SHA等于local resolved commit -> recommended。
17. Remote annotated tag peeled SHA等于local resolved commit -> recommended。
18. 同名 remote/local tag commit不一致 -> blocked。
19. Orphan/conflicting peeled ref -> blocked。
20. Local clone缺base tag object -> blocked。
21. Branch behind latest stable tag -> blocked。
22. Base tag不在HEAD ancestry -> blocked。
23. VERSION differs from remote latest stable -> blocked。
24. Remote only has prerelease after latest stable -> ignore prerelease。
25. Current VERSION itself is prerelease -> blocked auto mode。
26. Target raw或peeled tag already exists -> blocked。
27. Preview后另一个clone发布target tag -> apply blocked。
28. Preview后remote同名baseline tag被移动 -> apply blocked。
29. Preview使用origin、apply改为相同commit的upstream -> stale。
30. Preview后 `git remote set-url origin ...`，即使baseline SHA相同 -> stale。
31. Remote URL包含credential -> raw URL不出现在data/warnings/log assertions，fingerprint稳定。
32. Rename detection does not by itself alter bump。
33. Detached HEAD且全部校验通过 -> preview/apply允许，并返回顶层DETACHED_HEAD review flag。
34. 现有prerelease `parse_release_tags()` / `select_latest_release()` facade保持不变。

### Freshness and evidence

35. Tracked content changes after preview -> stale。
36. 仅 staged/unstaged placement变化、最终tracked content不变 -> digest不变；workspace展示状态更新。
37. Untracked regular-file content changes -> stale。
38. Changelog changes -> stale。
39. HEAD changes -> stale。
40. Remote baseline version/object/peeled commit changes -> stale/blocked。
41. Remote name或endpoint fingerprint变化 -> stale。
42. Oversized untracked evidence -> blocked / EVIDENCE_LIMIT_EXCEEDED。
43. Untracked symlink或其它非regular file -> blocked / EVIDENCE_UNSUPPORTED_FILE_TYPE，never followed。
44. Top-level VERSION不进入tracked content digest，但独立current VERSION字段变化 -> stale或already_at_target branch。
45. Canonical JSON key/path/bullet ordering重复执行得到相同digest。

### Compatibility, permission, recovery and output contract

46. Manifest静态requires包含local_repo和git_remote。
47. Existing patch dry-run unchanged且不调用remote。
48. Existing explicit target apply unchanged且不调用remote。
49. Existing downgrade rule unchanged。
50. Offline auto -> remote lookup failure, no write。
51. Auto apply without write enable -> permission denied (`ok=false`)。
52. Auto apply without confirm -> confirmation required (`ok=false`)。
53. Auto apply缺digest、expected base或expected target -> input error (`ok=false`)。
54. Expected base/target与recompute结果不同 -> stale (`ok=true`), no write。
55. Valid digest + confirm -> only VERSION changes，status=applied。
56. 首次apply写成功后以相同expected target重试 -> already_at_target，changed=false，VERSION不再写。
57. current VERSION已经等于expected target但remote target也存在 -> already_at_target no-op并披露remote状态，不误报新写入。
58. current VERSION既非expected base也非expected target -> stale, no write。
59. Tool never writes Changelog or Git refs。
60. 每个auto data status满足唯一schema；review_flags只在data顶层，human warnings只在Tool envelope顶层。
61. Static spec可发现manual/auto variants；resolver对具体payload返回正确contract。
62. Permission/input failures使用Tool `ok=false`；blocked/needs_input/stale使用 `ok=true,data.status`。
63. `version_check.bump_version` facade与 `release_target.bump_version` 结果一致。
64. recommendation module不导入version_check。

## 13. Quality gates

Focused：

```bash
./.venv/bin/python -m pytest \
  tests/test_release_version_recommendation.py \
  tests/test_version_check.py \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py \
  tests/test_release_test_plan.py
```

Static and metadata：

```bash
git diff --check
./.venv/bin/python scripts/generate_dependency_graph.py --check
./om-agent spec
```

Final：

```bash
make release-preflight ARGS="--full"
```

## 14. Planreview finding resolution

| Finding | Resolution |
|---|---|
| PR-01 contract extraction unspecified | v1 no longer claims arbitrary contract extraction; Unreleased is the semantic authority, sensitive paths only warn |
| PR-02 stale/local tag baseline | remote highest stable tag is authoritative; local object, ancestry, VERSION and target collision are checked twice |
| PR-03 digest confused with authorization | renamed recommendation_digest and scoped to freshness; real chat confirmation deferred to existing inbound state machine |
| PR-04 Unreleased assumption | made explicit mandatory input for auto mode; empty section returns needs_input |
| PR-05 advisory vs enforcing conflict | v1 is advisory; manual paths remain unchanged; no override policy in this slice |
| PR-06 scope too broad | split into three slices and removed generic semantic diff detectors |
| PR-07 Git topology tests missing | added bare-origin/two-clone integration matrix and remote race cases |
| PR2-01 remote tag identity incomplete | parse raw/peeled refs, compare remote commit to local resolved commit, bind all identities into digest |
| PR2-02 confidence overclaims semantics | remove confidence; expose classification_basis, declaration_status and review_flags; every recommendation requires manual review |
| PR2-03 dynamic remote dependency unclear | choose conservative static superset requirements and prove manual modes never invoke remote |
| PR2-04 SemVer ownership unclear | move bump primitives to release_target and retain version_check facade |
| PR2-05 detached apply undecided | allow detached preview/apply after all checks and emit DETACHED_HEAD review flag |
| PR3-01 unknown Unreleased content ignored | adopt strict fail-closed line grammar; any unknown or unowned nonempty content returns needs_input |
| PR3-02 digest omits remote selector/endpoint | bind normalized remote_name and fetch endpoint fingerprint; re-resolve on apply and redact raw URL |
| PR3-03 apply retry outcome ambiguous | require expected base/target and return no-write already_at_target when current VERSION equals expected target |
| PR3-04 staging freshness impossible | remove staging placement from digest freshness; bind final tracked content and report staging separately |
| PR3-05 output schema inconsistent | define one v1 field hierarchy, mode-sensitive output contract, and explicit Tool-error versus data-status boundary |

## 15. Residual risks

1. Changelog impact仍由维护者声明；严格grammar只能保证没有内容被静默忽略，不能证明声明本身正确。
2. 敏感路径warning可能有误报或漏报，但不改变推荐等级，也不改变 `manual_review_required=true`。
3. 远端tag查询需要网络；失败时auto mode不可用，手动bump仍可用。
4. 平行release race只能通过apply前remote recheck缩小，最终GitHub release workflow仍应保留tag collision失败语义。
5. `already_at_target` 只证明当前VERSION值，不是上一请求的执行receipt；响应丢失后的用户提示必须保留该限定。
6. remote endpoint fingerprint会在credential或URL spelling变化时使recommendation stale，即使实际仓库相同；这是保守的fail-closed选择。
7. 若未来需要自动检测Agent Tool/CLI schema breaking change，应先建立canonical contract snapshot，再单独设计comparator。

## 16. Final implementation handoff

该计划已收敛为可执行 v1：

- 自动分类来自严格、fail-closed的release intent grammar，而不是猜源码语义；
- 远端stable release的remote alias、endpoint fingerprint和raw/peeled commit identity共同控制版本基线；
- freshness、remote authority和用户授权分离；
- classification output不再使用semantic confidence；
- staging placement只展示，不制造无业务价值的stale；
- apply重复请求有明确的already_at_target no-op结果；
- auto data、Tool warnings和error envelope各自只有一套字段层级；
- 手动路径兼容；
- 不引入数据库、后台服务或第二套Tool；
- 每个失败状态都有可测试的reason code。

实施顺序必须保持 S1 -> S2 -> S3；S1 未通过真实 Git topology tests 前，不进入 VERSION write integration。

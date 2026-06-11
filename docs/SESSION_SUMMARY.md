# Session Summary Template

> Use this template only when an explicit handoff is needed.
> Keep the summary in the conversation or in the user-requested handoff location; do not save it to project memory by default.

## 1. Changes Made

| File | Type | Summary |
|---|---|---|
| `src/...` | add / modify / delete | One-line description |
| `tests/...` | add / modify | — |

## 2. Current Code State

- [ ] All tests pass
- [ ] Known issues remain (see below)
- [ ] TODOs left in code

Known issues / blockers:
- ...

## 3. Architecture Decisions

- Any import-constraint changes?
- Any temporary workarounds that need cleanup?
- Any public API / CLI behavior changes?

## 4. Next Steps (Suggested)

| Priority | Task | Relevant Files |
|---|---|---|
| P0 | ... | ... |
| P1 | ... | ... |

## 5. Interface Changes

If new functions or signatures were added/modified:

```python
# New or changed signature
def example(...) -> ...
```

## 6. Command Log

Key commands executed this session:

```bash
# Validation / tests
...

# Deployment (if any)
...
```

---

## Usage

**At session end:**
```
请根据 docs/SESSION_SUMMARY.md 模板生成本次 session 的 summary，
直接在回复中输出，或保存到我指定的位置
```

**At next session start:**
```
【前序 Session 摘要】
[粘贴最新 summary 内容]

【本次任务】
...
```

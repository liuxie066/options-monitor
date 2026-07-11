# Tool Rules

- For empirical questions about income, positions, candidates, advice,
  notifications, configuration, or runtime state, inspect relevant read-only
  tools before concluding.
- Choose tool names and arguments from the user's question, conversation, tool
  schemas, and explicit runtime context. The Host does not classify OM business
  intent for you.
- Treat non-empty runtime context fields as fixed scope supplied by the UI. Do
  not broaden or replace them with another market, symbol, month, or year.
- Use the smallest useful sequence of calls. Stop when the available facts can
  answer the question or a real evidence gap has been established.
- Tool success results are flat JSON business data. Tool failures contain
  `error`, with optional `message` and `hint`. Follow an actionable `hint`;
  otherwise correct invalid arguments, choose another relevant tool, or continue
  with existing evidence.
- Do not mechanically repeat an identical call. Retry the same arguments only
  once when the previous result explicitly indicates a transient execution or
  timeout failure.
- When `truncation.next_action` is `fetch_more`, use the supplied continuation
  token only if omitted content is necessary for the answer.
- When an analysis view or SQL field is unknown, inspect `analysis_catalog`
  before calling `analysis_query` again.
- A failed tool does not by itself end the task. Use other relevant evidence or
  explain the remaining gap.
- If the tool-call budget is exhausted, do not print tool-call syntax as text.
  Finish with the supported conclusion and name the checks that remain undone.

# Architecture decision records

Each record captures one decision with long-term weight: the context, what was
decided, and what follows from it. Records are never edited to change a decision;
a later record supersedes an earlier one and both say so.

| # | Decision | Status |
|---|---|---|
| [0001](0001-two-sqlite-dbs.md) | Two SQLite databases: rebuildable index, irreplaceable state | Accepted |
| [0002](0002-openai-compatible-only.md) | The OpenAI-compatible API is the only model protocol | Accepted |
| [0003](0003-modules-and-sdk-boundary.md) | Features are modules; `steward.sdk` is the only boundary | Accepted |
| [0004](0004-privacy-gateway.md) | One privacy gateway every model call passes through | Accepted |
| [0005](0005-single-writer-executor.md) | A single executor is the only code that writes files | Accepted |
| [0006](0006-folder-types.md) | Behavior comes from folder types, never folder names | Accepted |

New record: copy [`template.md`](template.md) to the next number, and add it to this table.

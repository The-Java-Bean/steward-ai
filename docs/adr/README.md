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
| [0007](0007-per-owner-migrations.md) | Each migration owner has its own Alembic history | Accepted |
| [0008](0008-jobs-events-scheduler.md) | Jobs, events and schedules in SQLite, run by lane threads | Accepted |
| [0009](0009-folder-settings-inheritance.md) | Folder settings: types inherit, restrictions carry down | Accepted |
| [0010](0010-discovery-and-file-identity.md) | Discovery and file identity | Accepted |
| [0011](0011-stage-framework.md) | Stage framework: a graph of stages, planned from current state | Accepted |
| [0012](0012-auth.md) | Accounts, sessions, re-auth and API tokens | Accepted |
| [0013](0013-api-and-setup.md) | The web API shell, its access checks, and the setup wizard | Accepted |
| [0014](0014-executor-journal-undo.md) | The executor: action types, the journal and undo | Accepted |
| [0015](0015-privacy-implementation.md) | Privacy in practice: classification, the gateway and visibility | Accepted |
| [0016](0016-backups.md) | Backups, exports and migrations | Accepted |
| [0017](0017-module-web-services.md) | Web services for module pages; the file manager is a module | Accepted |
| [0018](0018-resumable-uploads.md) | Resumable uploads | Accepted |

New record: copy [`template.md`](template.md) to the next number, and add it to this table.

# 0015. Privacy in practice: classification, the gateway and visibility

- Status: Accepted
- Date: 2026-10-28

## Context
ADR 0004 decides that one gateway enforces sensitivity for every model call and that
locked folders are invisible outside the Locked Folder view. This record fixes how
files are classified, what the gateway checks, and how listings and search hide
locked and hidden folders.

## Decision
- **Classification** (`core.privacy`): a file's sensitivity is the highest of its
  folder's effective setting, `privacy.yaml` rules (name globs, path patterns), the
  secret patterns (`excluded`) and a per-file override (`file_sensitivity`, set with
  `steward files sensitivity`). Anything that can't be resolved is `local-only` and
  treated as locked.
- **Excluded files are never read.** Discovery records their size and time but doesn't
  hash them (any old hash is cleared), and the planner gives them no stages.
- **The gateway** (`core.llm.Gateway.call`) takes the role, the request, a
  `provenance` of file IDs (a bare string is a `TypeError`), a purpose (`index` or
  `chat`) and the conversation's taint. It refuses `excluded` content, locked files in
  chat, and `local-only` content to a `remote` endpoint unless the caller allows the
  role's `local_fallback`. It limits concurrency per endpoint and logs no content.
- **Remote endpoints are refused at startup** until the `remote_llm` module exists.
- **Visibility** (`core.search.Visibility`): normal listings, search, the journal and
  `/api/v1` leave out everything inside a locked folder, a hidden (`visible: false`)
  folder, the trash and edit-backup folders. The filter is part of the SQL query, not a
  later pass. Journal entries that touch a locked folder show a generic summary until
  the owner has entered the password recently.
- **Tokens never see locked folders.** Search with `locked=true` needs a session and a
  recent password check.

## Consequences
- Every feature that sends content to a model has to pass file IDs. Text without
  provenance can't be sent.
- Paths inside locked folders answer 404 rather than 403, so their names don't leak.

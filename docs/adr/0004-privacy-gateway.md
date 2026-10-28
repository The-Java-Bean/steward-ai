# 0004. One privacy gateway every model call passes through

- Status: Accepted
- Date: 2026-10-27

## Context
steward-ai sends file contents to language models. Some folders are private and must
never reach a model server the owner doesn't trust; some files (key files, password
vaults) must never be read at all. Rules that live in prompts, or are checked
separately by each feature, will eventually be forgotten by one of them.

## Decision
- Every file has a **sensitivity**: `normal` < `local-only` < `excluded`. It's the
  highest of its folder type's setting, any path or pattern rule, and any per-file
  override. Rules can only raise it. A path that can't be resolved counts as
  `local-only`. A default list of secret patterns (`*.kdbx`, `*.pem`, `id_rsa*`,
  `.env`, `.ssh/**`, …) is `excluded`, meaning its content is never read.
- All model traffic goes through **one function** in `steward.core.llm`, which takes a
  `provenance` list: every file whose content (text, chunk, caption or filename) is in
  the request. The gateway computes the maximum sensitivity of the provenance and the
  conversation's taint:
  - `excluded` is refused outright (it means a bug upstream);
  - `local-only` to an endpoint with `trust: remote` raises `PrivacyViolation`; it is
    never silently re-routed.
- A conversation that touches anything `local-only` is **tainted** for the rest of its
  life and only uses local chat models from then on.
- Locked folders are invisible to every chat surface, to search outside the Locked
  Folder view, to shares and to integrations; the same gateway and query layer
  enforce it.
- Only `core.llm` may import the model SDK (ADR 0002). Modules call models only through
  the SDK's gateway handle, which requires `provenance`.
- A dedicated privacy test suite must pass before every release.

## Consequences
- Privacy doesn't depend on prompts or on each module remembering the rules.
- Every caller has to track provenance correctly; retrieval also filters `local-only`
  chunks out in SQL for remote endpoints, so the gateway is a second check, not the
  only one.
- Logs, events, notifications and digests carry IDs and paths, never content.
- What this does *not* protect against is documented: anyone with shell or disk access
  to the server, and other programs that read the disk directly.

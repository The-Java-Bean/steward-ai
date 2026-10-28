# 0002. The OpenAI-compatible API is the only model protocol

- Status: Accepted
- Date: 2026-10-27

## Context
steward-ai needs models for embeddings, captions, structured extraction, tagging,
triage and chat. People run very different model servers: Ollama, llama.cpp, vLLM,
LM Studio, or cloud APIs. Supporting each one natively multiplies code, tests and
failure modes, and makes the privacy check (ADR 0004) harder to keep in one place.

## Decision
- Speak **only the OpenAI-compatible HTTP API** (`/v1/chat/completions`,
  `/v1/embeddings`), using the `openai` Python SDK with a configurable `base_url`.
- Code never names a model. It asks for a **role** (`embed`, `caption`, `fields`,
  `apply_tags`, `generate_tags`, `triage`, `chat`); configuration maps each role to an
  endpoint and a model, plus a capability profile (native or prompted tool calls,
  JSON-schema or prompted structured output, vision, context length).
- A role without a mapping disables the stages that need it (with a health warning)
  instead of failing startup.
- Every endpoint declares `trust: local | remote`. Remote endpoints exist only when the
  `remote_llm` module is enabled.
- Only `steward.core.llm` may import the `openai` SDK (import-linter contract).

## Consequences
- Any server that speaks the API works, local or cloud, and switching models is a
  config change plus an eval run, not a code change.
- Provider-specific features outside the shared API (special embedding options, native
  vision formats, server-side caching) are unavailable unless they fit the API.
- `steward models probe` checks what each configured model can actually do, because
  "OpenAI-compatible" servers differ in their tool-call and JSON-schema support.
- `ChatModel` and `EmbedModel` interfaces keep a non-OpenAI adapter possible later,
  but adding one needs a new ADR.

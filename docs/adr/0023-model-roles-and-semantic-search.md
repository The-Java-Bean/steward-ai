# 0023. Model roles for stages, embedding spaces and hybrid search

- Status: Accepted
- Date: 2026-10-28

## Context
Phase 3 needs stages that call models: embeddings now, captions, fields and tags later
(plan §11). Code never names a model, only a role, and steward-ai must keep working
with no model at all and catch up when one is added (§3). Search should find documents
by meaning, not only by words (§12.2), with the same scope rules (locked, hidden,
trash) applied in SQL as for names and words (ADR 0021). The plan named sqlite-vec with
one `vec0` table per embedding space.

## Decision
- **Roles in the SDK.** `Registry.model_role(role, kind="chat"|"embed", description=,
  vision=)` declares a role; several modules may declare the same one if the kind
  agrees. `Registry.stage(..., roles=(...))` names the roles a stage calls. At startup a
  stage whose role isn't mapped in `llm.yaml` (or lacks `vision` when needed) is left
  out of the graph, with every stage that requires its output (`StageGraph.inactive`,
  shown by `steward stages list` and the health check `core.models`).
- **Model keys.** A role's key is its model name; embedding roles add `dims` and a hash
  of `document_prefix` (their vectors depend on them). The key goes into the graph
  signature, so mapping a role or changing its model triggers the full pass that
  back-fills, and into `stage_runs.model` (written by the core), so a result made with
  another model isn't current.
- **`StageContext.model(role)`** gives `embed`, `chat` and `structured(schema,
  messages, retries=2)`. Every call goes through the privacy gateway with
  `purpose="index"` and every present file holding the content as provenance; a
  `PrivacyViolation` makes the run `skipped`. Structured output uses the role's method
  (`json_schema` → `response_format` with the schema, `json_mode` → `json_object` with
  the schema in the prompt, `prompted` → prompt only), strips reasoning blocks and code
  fences, validates with Pydantic and sends validation errors (without the input
  values) back to the model; then `StructuredOutputError`.
- **Waiting for the model.** `ModelUnavailable` (now in the SDK) in a stage job becomes
  `RetryLater` (new in the SDK): the worker re-queues the job after a delay without
  counting the attempt (`queue.postpone`). The gateway keeps a per-endpoint circuit
  breaker (30 s after a failure, doubling up to 15 min, reset by a success), so a
  thousand queued jobs against a dead server cost one timeout, not a thousand.
- **Embedding spaces as plain tables.** Index revision `0005`: `embedding_spaces` (role,
  endpoint, model, dims, model key, `filling`/`complete`) and `embeddings` (space,
  chunk, L2-normalized float32 BLOB), cascading from `chunks` and from the space. Search
  compares vectors with sqlite-vec's `vec_distance_cosine` in the same SQL that applies
  the scope rules and filters; sqlite-vec is loaded only into the search connection.
  `StageContext.write_embeddings(vectors, role=, source=)` stores one vector per chunk
  (count and size checked) in the space of the role's current key, created on first use.
- **Spaces over time.** The space of the role's current model is the one searched, even
  while it fills (a query can only be embedded by the current model). It becomes
  `complete` once the stored graph signature is the current one and no planning job nor
  job of a stage using the role is queued or running; then the role's other spaces are
  deleted (worker housekeeping). Until then an old space stays, so reverting
  `llm.yaml` is instant.
- **The `search` module** (built-in, on by default) declares the `embed` role and the
  stage `search.embed` (`chunks` → `embeddings`, lane `llm-local`, heading + text per
  chunk, `batch_size` 16, documents over `max_chunks` 5000 skipped).
- **Hybrid search.** `search_all(..., embed=)` takes the process's `QueryEmbedder`
  (query prefix, 15 s timeout, small cache). The vector ranking is the best chunk per
  file, top 50, above the role's `min_similarity` (default 0.40). Modes: `names`; `text`
  = words + meaning; `all` = names + words + meaning; all merged by reciprocal rank
  fusion. Files found only by meaning show the start of their closest chunk and are
  marked (`semantic`). Any failure to embed the query means words only
  (`SearchResults.semantic = False`), never an error.
- **`steward models list|probe`**: what each role maps to and which stages use it; test
  requests (embedding size; reply, structured JSON, a tool call when `tools: native`, a
  generated red image when `vision`), never with file content.

## Measurements
On a 6-core desktop CPU, 100,000 random 1024-dimension vectors (415 MB):
- plain table + `vec_distance_cosine`, filtered by a join, top 200: **0.22 s**
  (0.05 s when the filter leaves a seventh of the rows);
- `vec0` KNN: 0.21 s (k = 200), 0.36 s (k = 4096, its maximum), but the top-k is taken
  before any scope filter, so hidden files would crowd out visible ones;
- reading the same vectors from a `vec0` table in an ordinary filtered query: 28 s.

With the reference embedding model (a 0.6B Qwen3 embedding model, 1024 dimensions, its
query instruction as `query_prefix`): clear matches scored 0.54–0.76, looser ones
("pet health" → a vet visit, "money owed" → bills) 0.41–0.46, unrelated pairs at most
0.33. 13 short documents embedded in 1.4 s over the LAN; the worker stayed at 169 MiB.

## Consequences
- Search by meaning costs a scan of every vector in the space per query: fine for
  hundreds of thousands of chunks on a home server; an approximate index (or int8
  vectors) can come later behind the same functions.
- `min_similarity` depends on the model; the default fits the reference model, and other
  models may need their own value in `llm.yaml`.
- sqlite-vec is pre-1.0; only its scalar distance function is used, and if it can't be
  loaded search falls back to words.
- Changing a model re-runs every stage using it. That is right for embeddings; for
  later chat roles (fields, tags) it means a model swap re-processes everything, which
  the eval step (Phase 3 chunk 6) should precede.
- Not done: pausing background `llm-local` jobs while a chat request runs (Phase 4);
  the setup wizard's model step.

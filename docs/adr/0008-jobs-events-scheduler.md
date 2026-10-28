# 0008. Jobs, events and schedules in SQLite, run by lane threads

- Status: Accepted
- Date: 2026-10-28

## Context
steward-ai needs background work (discovery, extraction, model calls, module
reactions to changes, periodic maintenance) that survives restarts and crashes, never
loses work, never finishes the same job twice, and runs without a separate broker
service (ADR 0001).

## Decision
- **Job queue** in the `jobs` table of `state.db`. Jobs have a type, a lane, a JSON
  payload, a priority and `run_after`. A worker **claims** a job atomically (one
  `UPDATE … RETURNING` under SQLite's write lock) and gets a **lease**. Crashed or hung
  workers are handled by lease expiry, not heartbeats: an expired lease lets another
  worker take the job over, and the original worker's late completion is ignored
  because completion checks the holder. Failures retry with exponential backoff until
  `max_attempts`, then the job is `dead` (kept, retryable). `PermanentJobError` skips
  the retries. A `dedupe_key` is unique among queued and running jobs.
- **Lanes** (`io`, `cpu`, `llm-local`, `llm-remote`, `events`, and `executor` in the
  API process) each get a configurable number of threads; handlers are plain
  synchronous functions run in those threads. A job whose type has no registered
  handler (its module is disabled) stays queued.
- **Events** are rows in the `events` outbox, written with `emit` in the same
  transaction as the change. A dispatcher keeps a **cursor per subscriber** and turns
  new matching events into delivery jobs (`core.deliver.<subscriber>`, dedupe key
  `event:<id>:<subscriber>`). A new subscriber starts from the newest event; a
  disabled one resumes from its cursor when re-enabled (within the retention period,
  90 days by default). Delivery is at-least-once and unordered across events.
- **Schedules** are registered by modules (`Every(interval)` or `Daily(time)`), can be
  re-timed or turned off per name in `pipeline.yaml`, and keep their state in the
  `schedules` table. Missed runs collapse into one; the job's dedupe key
  `schedule:<name>` prevents stacking.
- One housekeeping thread in the worker dispatches events and fires schedules.
- All timestamps are timezone-aware UTC, stored as fixed-width ISO 8601 text.

## Consequences
- No broker to run or back up; the queue is inspectable with `steward jobs list` and
  any SQLite browser.
- Every handler must be idempotent, since work can run again after a crash.
- SQLite allows one writer at a time: claims and completions are short transactions,
  and busy waits are handled by a 10-second busy timeout. Throughput is bounded by
  that, which suits a single-owner server.
- Long jobs must call `renew_lease()` before the lease (5 minutes by default) runs out.

# 0027. Eval sets ship with their modules; scores stored without model output

- Status: Accepted
- Date: 2026-10-30

## Context
Tagging and fields depend on a model the owner picks. Without a measurement, a changed
model or prompt shows up as worse tags weeks later. Unit tests use a fake model and say
nothing about this. The plan put the sets in a root `evals/` folder, but that wouldn't be
in the wheel or image, and a module installed from elsewhere couldn't have one.

## Decision
- **Sets live in the module that owns the prompt** (`modules/<name>/evals/<role>.yaml`), and
  a new extension point `eval_suite(role, run, cases=, primary=, prompt=)` registers them.
  `run(data, model)` yields `EvalCaseResult`s with metrics from 0 to 1, using the module's
  real prompt, schema and cleaning, so a score is about what indexing does.
- **Sets:** `fields` (accuracy, not guessing), `apply_tags` (F1, share of files tagged,
  leaving files nothing fits alone), `embed` (recall@3, MRR, rejecting unrelated queries),
  `caption` (concepts present, nothing invented; images drawn with Pillow at run time, so
  there are no binaries and no people in the repo). `generate_tags` has none: its output is
  a draft the owner edits. All text is made up, and calls go through the gateway with empty
  provenance.
- **`eval_runs` in `state.db`** (revision `0008`, in backups): role, model, prompt, set hash,
  cases, metrics, failed case ids, baseline flag. Never model output.
- **`steward eval <role>`** runs, compares with the baseline of the same set, and exits 1 if
  a metric fell by more than 0.05. A run ended by an unreachable model records nothing.
- **Structured answers and captions use temperature 0.** At the model's default, 5 of 69
  files changed tags on a re-run with nothing else different, which also made results differ
  after a rebuild. Afterwards two full re-tags changed nothing; rebuilding from scratch
  changed one tag of 69, on a borderline case, so 0 is close to stable, not exact.

The limit was 0.02 in the plan. Seven runs of one model, before temperature 0, spread
`apply_tags` F1 from 0.95 to 1.00: one case in 23 moves a score by up to 0.05, so 0.02 would
have raised false alarms.

## Reference scores
`qwen3-embedding:0.6b` (embed) and `gemma4:12b` (the rest, reasoning off), 2026-10-30:

| Role | Scores | Misses |
|---|---|---|
| `embed` | recall@3 1.00, MRR 1.00, unrelated rejected 1.00, above floor 1.00 | none |
| `fields` | accuracy 0.986, no guessing 1.00 | `bank-statement`: gave the statement period, not the issue date |
| `apply_tags` | F1 0.983, tagged 1.00, no forcing 1.00 | `vet-invoice`: adds or drops one of two tags |
| `caption` | concepts 0.944, described 1.00, nothing invented 1.00 | `document`, `checkerboard`: a concept left out |

The misses stay in the sets: labels were not changed to raise a score.

## Consequences
- `tagged` on the set measures the model, not a library: the share of a real library that
  gets a tag depends on its vocabulary. On a 69-file sample, all 52 documents got a tag and
  the four files nothing fit were left alone; 4 of 12 drawn test images did, and the rest
  fit no tag in the vocabulary. Real photos aren't measured here.
- A module's set grows the package a little. Sets built from an owner's corrections (§11.5
  of the plan) would hold personal data and need their own place; they come later.
- Changing a set changes its hash, so the old baseline stops counting.

## Alternatives considered
- A root `evals/` folder: not shipped (above).
- Storing answers for later reading: they can quote test data today, and would quote
  personal data once sets come from corrections.
- Asserting scores in CI: it would need a model server, and scores belong to a model.

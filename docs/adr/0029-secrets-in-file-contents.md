# 0029. Credentials in file contents hold the file back

- Status: Accepted
- Date: 2026-10-30

## Context
The patterns in `privacy.yaml` go by name: `.env`, `*.pem`, `*.kdbx`. A test on real files
showed what that misses: a `docker-compose.yml` with a database password was read, chunked
and sent to the embedding model like any other text. The privacy rule is about where
content may go, and a config file isn't sensitive by its name.

Chat is the next phase. Anything in the index can then end up on a screen or, with
`remote_llm`, leave the network.

## Decision
Text is looked through before it's stored, in `StageContext.write_text`. That's the one
door every stage's text goes through (`text`, `text:ocr`, `text:caption`, or a module's own),
so chunks, full-text search, embeddings, `fields` and tags never see it, and a module
gets this without asking, like the privacy gateway.

`find_secret` looks for private keys, tokens with a recognizable prefix, passwords in URLs,
and `name: value` lines where the name says it's a credential and the value looks real.
It returns the kind of credential and never the value, so results, events and logs can
name it. It prefers missing a weak password to flagging every config template: `${VAR}`,
`<token>`, `changeme`, `self.password` and plain words aren't findings.

On a find, `SecretFound` stops the stage and the file is **held back**. Every present copy
gets an `excluded` override with the actor `rule:secret-scan:<kind>`, the blob goes with
all that hangs off it (rows cascade; the FTS triggers fire), and a path scan drops the
hashes. From then on it's a normal excluded file: the name is searchable, the content isn't
read. Locked folders are scanned too, since vaults and keys shouldn't become embeddings
even locally (plan §9.1).

The way out:
- Editing the file: the scanner sees it differ from the recorded size or time, clears the
  override and reads it again.
- A false positive: add the path to `content_scan_ignore` in `privacy.yaml`, then `steward
  files sensitivity <file> clear`. Paths rather than a list of approved content hashes:
  no new table, and the decision is readable and reviewable with the other privacy rules.
- `content_scan: false` turns it off.

Measured on the reference data: no false positives in 92 real text files, one in 355
files of this repository (a test's fixture password), and all 10 files in a folder of
retired services that really held credentials were caught. A worst-case 16 MiB text
takes 3.5 s.

## Consequences
- A note that mentions a real password disappears from search by content and from chat.
  `/ui/health` (`core.secrets`) warns with the count and `steward files held-back` lists
  them, so this isn't silent.
- Detection is heuristic. A weak password in a plain word (`password: letmeinplease`) isn't
  found, and neither is one in free prose. Name patterns and folder types stay the
  stronger tools.
- A vision model sees an image before its OCR text is scanned, so a photo of a password
  isn't caught in time. The caption role only reaches trusted endpoints for private
  folders, but a `normal` photo can't be protected this way.
- A held-back file's own override replaces one you set (`local-only`), and clearing doesn't
  bring yours back.
- Improving the rules only affects text read afterwards; `steward reindex` applies them to
  everything.
- With a copy under `content_scan_ignore` and one outside it, the outside one is held back
  and the blob is deleted, then restored for the ignored copy by the path scan.

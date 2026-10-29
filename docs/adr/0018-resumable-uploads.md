# 0018. Resumable uploads

- Status: Accepted
- Date: 2026-10-28

## Context
The file manager must take very large files (the Phase 2 acceptance test is 5 GB through
a browser) and survive dropped connections, without ever overwriting a file and without
any code but the executor writing into libraries (ADR 0005). Browsers and reverse
proxies limit request sizes and time, so one long request isn't an option. tus is the
usual protocol for this; its full feature set (creation-with-upload, concatenation,
checksums, its own headers) is more than we need, and a client library would be our
first vendored JavaScript.

## Decision
- **Upload sessions are core**, in `steward.core.executor.uploads` (the executor package
  is the only writer). State revision `0006` adds `uploads` (`upload_id` = 128 random
  bits as hex, destination library/folder, name, size, `received`, status `open |
  finished | cancelled | expired`, `confirmed`, actor, journal `entry_id`).
- **The bytes go into `.steward-tmp-up-<upload_id>` in the destination folder**: the
  same disk as the target, so finishing is a `rename`; created exclusively without
  following symlinks, mode 0600 in locked folders. Names starting `.steward-tmp-` are
  hidden from every view and ignored by discovery.
- **A small tus-like protocol at `/api/v1/uploads`** (session only; CSRF token in
  `X-CSRF-Token`; API tokens can't upload): `POST` to start (`folder`, `name`, `size`,
  `scope`), `PUT /{id}` with `Upload-Offset` and the next piece as
  `application/offset+octet-stream`, `GET /{id}` for the offset, `POST /{id}/finish`
  (optionally a new `name`), `DELETE /{id}` to cancel, `GET` to list unfinished ones.
- **Whole pieces only.** A piece (at most `uploads.chunk_mib`, default 8 MiB) is written
  after its whole body arrived, then `fsync`ed and recorded, so the server's offset is
  always a piece boundary; a piece cut off by a dropped connection leaves nothing. A
  temp file longer than `received` (crash between write and record) is cut back. An
  offset that doesn't match answers 409 with the server's offset.
- **Checks at start:** visible in the caller's scope, writable folder, valid name, the
  name not taken (409 with a free name, "keep both"), size limit, free space (size plus
  64 MiB), at most `max_open` unfinished uploads. Uploading into a locked folder needs
  a recent password check at start; finishing doesn't ask again.
- **Finishing runs the journaled `file.upload` action**, which only moves a complete
  upload temp file to a free name beside it (never overwrites; a name taken meanwhile
  answers 409 with a suggestion). Its undo moves the file to the trash. Its post-state
  has no content hash, so finishing a large file doesn't re-read it under the write lock.
- **Expiry:** `core.uploads.expire` runs hourly on the `executor` lane (in `steward
  api`, since the worker mounts libraries read-only) and removes uploads untouched for
  `stale_hours`.
- **The browser client** (`files_ui/static/upload.js`, no framework) sends files one at
  a time, retries with backoff and re-reads the offset after failures, keeps upload IDs
  in `localStorage` (and also matches the server's unfinished uploads by folder, name
  and size), so choosing the same file again resumes it, even after a restart or from
  another browser.
- **No form upload without JavaScript:** multipart parsing would spool file content
  into the system temp folder, outside the libraries and the executor.

## Consequences
- Verified in headless Chromium: 5 GB with the network cut at 30% and the server
  restarted at 60% finished intact; a browser closed at a third resumed from there.
- Reverse proxies must accept request bodies of `chunk_mib` (nginx's default is 1 MiB).
- Unfinished uploads hold disk space until finished, cancelled or expired.
- `file.upload` can't use the shared executor-safety examples (it needs an upload temp
  file); its refusals are tested in `tests/integration/test_uploads.py`.

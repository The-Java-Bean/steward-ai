# 0020. Share links: a core-owned public route and the `shares` module

- Status: Accepted
- Date: 2026-10-28

## Context
Plan §14.4: built-in public links at `/s/<token>` to a file or a folder, the only
unauthenticated routes, serving nothing but the shared item. Shares are refused for
locked, `local-only`, `excluded` and non-shareable content; expiry defaults to 7 days,
with no maximum and "no expiry" allowed but sensitive; a password is on by default;
folder shares warn that later files are shared too and offer a snapshot; `/ui/shares`
lists active and past shares; built-in shares keep an access log. Shares are a module
(`shares`), so they may only use `steward.sdk` (ADR 0003, 0017), but a module must not
decide on its own what the public may read.

## Decision
- **The core owns the public route.** `GET|POST /s/{token}` and `/s/{token}/{rest}`
  are core routes at the `public` level; anything that isn't a 22-character URL-safe
  token is a 404 before any module runs. Requests go to the one handler registered with
  `Registry.share_links` (a second one stops startup; with none every link is a 404).
  The public route set in the security suite lists them explicitly.
- **The core checks the item, every request.** The handler gets a `ShareRequest` and
  opens the item with `open(path, kind, only_files=)`. The core then requires the item
  to exist as that kind, to be visible in normal views and to pass the sharing rule
  (`SharingRules`: not locked, not hidden, sensitivity `normal` counting folder types,
  `privacy.yaml` rules and per-file overrides, and a shareable folder type; the path is
  judged as a file and as a folder). Everything a `SharedItem` returns goes through a
  `Browser` in a new internal `shared` scope that reaches only the item and leaves out
  whatever inside fails the same rule (and, for a snapshot, files that weren't there).
  So a link stops working, or a file drops out of it, the moment the owner locks or
  marks it, with nothing to clean up. Public entries carry paths relative to the item;
  public pages extend a core `public.html` without navigation and get `no-referrer`,
  `noindex` and `no-store`.
- **Sharing rules for action types** come from `ReadOnlyFS.privacy(path)` →
  `PathPrivacy` (the same rule), and for pages from `Files.privacy(paths)`.
- **Shares are actions.** `shares.create` (proposal mode, no expiry → `Plan.sensitive`),
  `shares.update` and `shares.revoke` (auto mode) go through the executor, so they're
  journaled, need the password when sensitive and can be undone: undoing a share turns
  it off (`undone`); revoking is final (a new link is a new share); undoing an expiry
  change restores the old expiry, a password change can't be undone. They change only
  module rows, via the new `GuardedFS.write_state(op)`, which runs in the executor's
  transaction with the journal entry.
- **Secrets stay out of the journal.** `Plan.transient` carries values `apply` needs but
  that are never stored (the password, a snapshot's file list); the token is made in
  `apply`; journal params only say whether there is a password. Passwords are argon2id
  (two hashes at a time at most). A correct password sets a cookie scoped to the link's
  path for 12 hours: an expiry plus an HMAC keyed by the password hash, so a new
  password ends every earlier grant without a table of grants.
- **Module state access** becomes part of the SDK (`steward.sdk.state`): a
  `StateAccess` handle on `Web`, `ShareRequest`, `JobContext` and
  `ReadOnlyFS.read_state()`, plus `UTCDateTime` (moved from the core) for module
  migrations. Modules only touch their own `<module>_*` tables.
- **Tables** (`shares` migrations): `shares_links` (random `share_id`, token, item path
  and kind, password hash, `expires_at` or NULL, snapshot flag, created at/by, end time,
  reason (`revoked`/`expired`/`undone`) and actor, access count, last access),
  `shares_snapshot`, `shares_access` (time, address, user agent, `view`/`download`/
  `zip`/`password_failed`). Expiry is checked on every request; `shares.expire` (every
  15 minutes) records it. Wrong passwords: 10 per address or 100 per share in 15
  minutes, then 429. Unknown, expired, revoked and no-longer-shareable links all get
  the same 404 page.
- **Owner pages.** `/ui/shares` (Active, Past; filter by path and age), a page per share
  with its access log, edit and revoke; items now hidden in normal views are listed
  without their name, and the text filter ignores hidden names. The share dialog
  `/ui/shares/new?path=` is linked from the file manager through `Web.page_url`, so
  `files_ui` doesn't depend on `shares`. `ui_page(badge=)` puts the count of shares
  without expiry next to "Shares". `api.public_url` sets the links' address behind a
  proxy.

## Consequences
- Another share provider (a file manager integration) can replace the built-in one by
  registering `share_links`, and gets the same core checks; one making links in another
  program will need its own sync (later).
- Shares follow paths, not file identities: moving or renaming a shared item breaks its
  link (fails closed). Whole libraries can't be shared.
- The access log grows with use; there is no pruning yet.
- No `/api/v1/shares` yet, and the no-expiry reminder in the digest waits for the
  `digest` module.

# 0019. The Locked Folder view

- Status: Accepted
- Date: 2026-10-28

## Context
Plan §9.7: locked folders are hidden from every normal view and only reachable in a
**Locked Folder** view after re-entering the password, with full file management
inside, permanent locks that the UI and API can't remove, unlocking that shows what
becomes visible, and moves out that need a confirmation. ADR 0017 already gave module
pages a `locked` scope in `steward.sdk.web` (`Files` refuses it without a recent
password check) and the executor treats anything touching a locked folder as
sensitive (ADR 0014). What was missing: the view itself, a way to find the locked
folders, lock management in the file manager, and proof that no route leaks.

## Decision
- **Same module, second page.** `files_ui` registers `files_ui.locked` at `/ui/locked`
  next to `/ui/files`; both come from one `make_router(scope)` so every feature exists
  in both scopes and templates link with `base`/`scope`. Unlocking and the locked
  search exist only under `/ui/locked`.
- **A gate on the whole router.** Every `/ui/locked` route has a router dependency
  that asks for the password (`require_reauth`) unless `recently_authenticated()`;
  the scoped `Files` calls check again. Viewing uses `is_recent`, so with a re-auth
  window of 0 browsing doesn't use up the one-shot check, but each sensitive change
  does.
- **What the locked scope shows.** `Visibility` keeps a third set, `concealed`:
  folders hidden for reasons other than a lock (`visible: false`, trash type,
  copy-holding roles, `.steward`). The locked scope is `is_locked and not concealed`
  (`in_locked_view`), in listings and in the search SQL, so the locked part of the
  trash never shows in the Locked Folder. Its root lists `locked_roots()`: the
  outermost locked folders.
- **Locking.** "Lock folder…" on any non-root folder in Files runs `folder.lock`,
  optionally `permanent`. A permanent lock is sensitive (new `Plan.sensitive`: a plan
  can add sensitivity for some parameters) and has no inverse, so it can't be undone.
  An existing lock set on the folder itself can be made permanent from the Locked
  Folder. Only `steward folders unlock --permanent` on the host removes one;
  `folder.unlock` refuses permanent locks and inherited locks when planning.
- **Unlocking shows what becomes visible.** `Files.unlock_preview(path)` works it out
  on a copy of the policy without that folder's lock: counts and size of scanned files
  and folders that would become visible, a sample, nested folders that keep their own
  lock, and whether the folder's type is what locks it (unlocking then overrides the
  type for that folder). Unlocking is sensitive (password again).
- **Moving out** goes through a picker that switches to the normal scope; the form
  needs a "move them out" checkbox (else 409, nothing moved), and the move is
  sensitive like any action touching a locked folder. Normal views can't move into the
  locked scope (locked destinations don't exist there).
- **Proof.** `tests/privacy/test_locked_routes.py` enumerates every route of the app
  and calls it with locked paths in every parameter a route reads, as a session
  without a recent check and as an API token: no answer contains a locked file's name
  or content. With a recent check, only `/ui/locked`, the locked search, uploads and
  the journal may.

## Consequences
- The Locked Folder is a module feature: turning `files_ui` off removes it (locks still
  hide folders everywhere; manage them in folder settings or the CLI).
- Counts in the unlock preview cover what discovery has recorded, like search.
- Moving files *into* the Locked Folder from Files isn't offered yet; lock the folder,
  upload inside the Locked Folder, or move within it.
- `KindPage` became `FilePage` (alias kept); `PathPolicy` and `FolderListing` gained
  lock fields with defaults, so existing module code keeps working.

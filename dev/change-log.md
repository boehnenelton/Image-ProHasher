# Change Log

## V14 / PKG9 — 2026-11-13 (BEJSON CMS user/password auth system & Playwright Termux support)
**Authentication & Usability**
- Replaced cumbersome long-token requirement with a dual username & password system modeled after `BEJSON_CMS`.
- Config defaults: `viewer_user = "admin"`, `viewer_password = "changeme"`, with environment variable overrides (`VIEWER_USER` / `CMS_USER`, `VIEWER_PASSWORD` / `CMS_PASSWORD`) and CLI parameters (`--user`, `--password`).
- Login page (`login.html` v108) updated with clear username, password, and optional token entry fields; backwards-compatible token login (`?token=`, `X-API-Token`, token form) remains 100% operational.
- Added HTTP Basic Auth support across API and routes for headless/automated access.
- Fixed 401/404 console errors on pre-login favicon requests by registering explicit `/favicon.ico` route and gate exception.
- Removed `@view-transition { navigation: auto; }` in `becss-core.css` which caused aborted transition warnings on page switches in Chromium.

**Testing & Diagnostics**
- Updated `tests/ui_audit_playwright.py` to adaptively leverage native `termux_playwright` on Android Termux ARM64 while preserving standard `playwright` fallback.
- Added full username & password login lifecycle verification in Playwright UI audit across desktop, tablet, and mobile viewports.
- Added unit tests for username/password authentication and Basic Auth in `tests/test_prohasher.py` (59 unit tests passing, UI audit 100% clean).
**Versions**: project 14, package 9; viewer 108, login.html 108, becss-core.css 102.

## V13 / PKG8 — 2026-10-24 (refine + harden, measured)
**Scale (benchmarked on 30,000 records / 3,600 groups)**
- `reconcile()` 18.6 s -> 0.6 s: it re-parsed the whole manifest once per group (O(G^2)). New `Common.entity_path_map()` (one manifest read); also applied to prune and rebuild.
- Viewer group list 0.88 s -> 0.02 s warm (search 0.77 -> 0.01 s): cached summaries, invalidated by every mutation, manifest/resolved-file change, and a 10 s age limit.
- Quick-Group 30k records: 7.6-9 s, 130 MB.

**Data safety**
- `safe_move` / `safe_copy`: temp name + size check + atomic publish; cross-filesystem moves (internal <-> SD card) copy and verify BEFORE deleting the source; failure leaves no partial file. `shutil.move` could leave a truncated destination.
- Trash-log crash recovery: files stranded in trash/ by an interrupted commit are re-registered (original path from the index) so they appear in Trash and can be restored.
- Corrupt config is quarantined (`config.104a.bejson.corrupt-<ts>`), not silently overwritten (which also regenerated your token).
- Stale-run detection no longer misreads a slow batch as a crash, and no longer trusts a recycled Android PID: the PID must belong to one of our scripts (/proc cmdline) to count as running.

**Security**
- Cookie = random server-side session (12 h sliding), not the long-lived token; **Sign out** revokes it; the raw token is rejected as a cookie.
- `--rotate-token`; `Cache-Control: no-store` on API responses; Permissions-Policy, COOP/CORP headers.

**Quality**
- EXIF sub-second time is part of `exif_datetime`, so burst frames in the same second no longer form false quick groups.
- HEIC/HEIF: used automatically when `pillow-heif` is installed (browser gets a JPEG rendition); otherwise skipped files are counted and one clear install hint is printed (previously silent).
- UI: primary "Resolve" visible on phones, dialogs trap Tab and restore focus, Sign out in the sidebar.

**Tests**: 58 unit/integration tests (+15: cross-device move failure injection, recycled-PID simulation, stranded-trash recovery, session revocation, header checks, HEIC both ways, corrupt config, EXIF subsec, cache freshness). Browser audit adds Resolve visibility, focus trap, Sign out revocation.
**Versions**: project 13, package 8; scanner 105, grouper 106, viewer 107, common lib 2.

## V12 / PKG7 — 2026-10-23 (fix: "not allowed" when opening the viewer)
**Root causes found and fixed**
- **Sign-in form returned 403 Forbidden.** The form POST carries `Origin: null` (the app sets Referrer-Policy: no-referrer, so browsers null the Origin on form posts) and the CSRF check treated that as cross-site. Now only `/login` accepts a null Origin; the JSON API still rejects it. A wrong token is still refused.
- **Sign-in page rendered unstyled.** The auth gate exempted `/static/` but assets are served from `/gv_static/`, so CSS was answered with a JSON 401 before login. Gate now uses the app's real static path.
- **Refusals now say what happened.** Host mismatch: "Address not allowed ... open http://127.0.0.1:PORT/ instead (or --lan)". Cross-origin: "Cross-site request blocked". Previously plain "Bad Request"/"Forbidden".
- Sign-in page tells you where the token lives (config `viewer_api_token`) and how to reset it.
**Why it slipped through earlier**: every earlier browser run signed in via the `?token=` link, never the form. `tests/ui_audit_playwright.py` now signs in through the form (wrong token, then right token) and asserts the page is styled; 3 new unit tests cover null-Origin login, pre-login assets, and explanatory refusals.
**Versions**: project 12, package 7; group_viewer 106, login.html 106.

## V11 / PKG6 — 2026-10-20 (UI audit via Playwright, hardening, clean package)
Method: seeded 64-image / 14-group sandbox, drove the viewer in Chromium at 1280x800, 768x1024 and 390x844 with ~40 assertions (no horizontal overflow on any view, single-row toolbar, tap targets >= 44px, selection bar reachable, no stray text, no console errors, full bulk-resolve -> commit -> Undo cycle).

**Bugs found by the audit and fixed**
- Literal "null" text rendered in the toolbar/selection bar/review dialog (`replaceChildren(null)`); shared `clean()` filter.
- Phone header overflowed; its widened layout viewport pushed fixed elements (the pending cart) off-screen. Header rebuilt; search moved into the list view.
- Group list was an unreadable table on phones -> card list (3 thumbnails, file name, reason, folders, space to free).
- Selection actions sat off-screen at the end of the toolbar on phones -> thumb-reachable bottom selection bar; Ungroup moved into a menu and de-emphasised (it was red, next to Trash).
- BECSS `--primary` renders salmon (oklch); pinned to #DE2626.
- Reason chips all showed "Legacy group" (label contains " + ", broke the splitter).
- Verified identical copies collapsed into one tile, so you could not choose WHICH copy to keep -> "Each copy / Group copies" toggle, per-tile "same as N others".
- Undo restored files but left groups dissolved -> trash log now records group id/basis/siblings and Undo restores the group.

**New / extended (what comparable dedupe tools have)**
- Prev/next group, "n of N", browser Back returns to the list, J/K/A/T/V/R/Esc/? shortcuts, auto-advance after Resolve.
- Tiles: file name, folder, dimensions, size, date; "Largest" badge; KEEP / TRASH / MOVE / COPY ribbons that show what Commit will do.
- Review-before-commit dialog (counts, bytes, thumbnails, remove-per-item); **guard**: a group whose every copy is queued for trash is held back unless explicitly allowed (server-enforced); "freed X" + **Undo** toast.
- "Resolve exact copies" bulk action: only groups proven byte-identical (SHA-256) are touched; everything is staged, nothing deleted until Commit.
- Lightbox: prev/next, click-to-zoom, compare view marks the larger file/dimensions; queue-trash from the viewer.
- Thumbnail size S/M/L, session labels (folder name + profile) instead of ids, scan/grouping status banner, real empty states (no sessions / no groups yet / all caught up / no match), loading text, focus rings, ARIA roles (tiles are keyboard-operable checkboxes), reduced-motion support.

**Packaging**: removed four unused BECSS stylesheets (370 KB) the viewer no longer loads; added dev/verify_package_clean.py (+ tests) that fails on sessions, thumbnails, images, nested archives, bytecode, generated config, tokens, personal media paths, non-empty notes.
**Versions**: project 11, package 6; group_viewer 105, CLI_Image_Grouper 105, UI assets 105.

## V10 / PKG5 — 2026-10-18
**User-reported (updates.txt)**
- Viewer toolbar: single horizontally-scrolling row; selection-only actions (Trash/Move/Copy/Compare/Not dup/Invert/Clear + count chip) appear only while tiles are selected; "Selected/Selection" wording removed; four Keep buttons folded into one Keep menu; tap-to-select tiles.
- Speedhash: now truly heuristic. Speed scan = stat + header + EXIF only. New Quick-Group builds review groups from size+dimensions / EXIF time+camera / optional filename, labelled with the reason. Hashing happens only on demand (viewer Verify, per group) or via Deep.
- State management: see below.

**State management**
- KVState replaces BEJSONState: atomic writes, one row per key, heartbeat + PID crash detection (`running` no longer sticks forever), corrupt-file quarantine, legacy 104db migration.
- Profile no longer cached in session state (menu toggle was a no-op once a session started).
- Rescan resumes correctly (`rescan_active`), record_uuid is uuid5(session,path) so rescans no longer orphan groups, unchanged files reuse prior hashes.
- Grouper no longer trusts `phase1_complete`; incremental every run. Groups: entity written first, index derived, `reconcile()` repairs gaps; dissolve removes manifest row before data file.
- "Not a duplicate" is remembered (dismissed set); Resolve is now a real toggle (Reopen).
- UI state (session, open group, filters, view) survives refresh; staged queue keeps failures for retry.

**Performance**: batched MFDB writes (1 lock+write per batch, was 2 full-file rewrites per image); single directory walk (was 3); DCT 10k MACs w/ table (was 65k + `__import__` per MAC); pHash decodes once (was temp re-save + 4 full re-resizes); SHA-256 only on byte-size collisions; viewer entity cache (was full 1.5 MB re-parse per thumbnail); JPEG draft decode. Measured 2,000 files: v9 246 s → v10 speed 1.3 s.

**Security**: see dev/security-notes.md.
**Policy**: headers/RELATIONAL_IDs, credit corrected to boehnenelton2024@gmail.com, standard flags, exit codes, picker instead of typed paths, About + Debug notes (dev/persistent-notes.md), Python 3.10 grammar, no print() logging, no positional Values[] access in app code, package hygiene (§3.4).

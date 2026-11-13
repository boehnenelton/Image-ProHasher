# Image-ProHasher v13 (PKG8)

Scan → group → review duplicate images on Android/Termux (BEJSON/MFDB storage).

## Requirements
Python 3.10+, `Pillow`, `Flask`. Optional: `pillow-heif` (phone HEIC/HEIF photos — without it they are reported and skipped), `playwright` (browser audit).

## Fast path (Speed profile, default)
1. `python3 Menu_Image_Suite.py` → **1** New Session → **5** Folder Scan (browser folder picker) → **6** Start Scan.
2. Speed scan reads only file headers + EXIF (no pixel decode, no hashing) and, on completion, auto-runs **Quick Group**.
3. **14** Launch Viewer → review groups. Each group says *why* it was formed. Tap tiles to select, **Verify** hashes just that group.

## Quick-Group heuristics
| Basis | Rule |
|---|---|
| same size + dimensions | identical byte size and width×height |
| same EXIF time + camera | identical capture timestamp and camera model/make |
| same filename + dimensions | optional (`quick_group_use_filename`), normalised stem, ignores "(1)", "-copy" |

Overlapping buckets merge into one group. Buckets over `quick_group_max_bucket` (25) and merged clusters over 100 are skipped as mass-identical junk.

## Deep profile
`--Deep` scan (or menu **7** to toggle) adds a 4-orientation pHash per image and SHA-256 for files whose byte size collides. **Deep Group** backfills any missing hashes on a Speed index, flags exact duplicates, then clusters by Hamming distance (≤ `phash_hamming_threshold`).

## CLI
```
Cli_Image_Scanner.py  --New-Session | --Sel-Session ID | --Dir-Scan PATH | --Drive-Scan [PATH] | --Start-Scan [--Speed|--Deep] | --Stop-Scan | --Status
CLI_Image_Grouper.py  --Quick-Group [--Rebuild] | --Start-Grouping | --Stop-Grouping | --Verify-Group ID | --Prune | --View-Group ID | --Del-Group ID | --Reset-Dismissed
group_viewer.py       [--port N] [--lan] [--no-browser]
all                   --version --config --reset --register --help
```
Exit codes: 0 ok · 10 folder-picker failure · 20 environment/library missing · 30 schema error.

## Reviewing in the viewer
- **Groups** list: cards with thumbnails, file name, *why grouped*, folders and space you can free. Filter Open / Resolved / All, sort, search by file name.
- **Resolve exact copies ▾**: for every open group proven byte-identical, keep one (by resolution / size / date / path) and queue the rest. Similar-but-different groups are never touched.
- Inside a group: tap images to select. **Keep ▾** keeps one by a rule, **Verify** proves which files are identical, **Each copy** shows identical copies separately so you can pick which to keep. KEEP / TRASH ribbons show what will happen.
- Selected images: bottom bar **Trash / Move / Copy / ⋯** (Compare, Not a duplicate, Invert).
- **Review N pending** → counts, thumbnails, per-item remove → **Commit**. Trashed files can be restored (**Undo** right after commit, or the Trash page). A group whose every copy is queued is held back unless you tick the warning box.
- Shortcuts: J/K next/previous group, A select all, T trash selected, V verify, R resolve + next, Esc, ?.

## Viewer security
First run generates `viewer_api_token` in `config/config.104a.bejson` (chmod 600). Open the printed `?token=` link once; it sets an HttpOnly cookie and strips the token from the URL. Rotate by deleting that config row.

## If the viewer says something is blocked
| Message | Meaning / fix |
|---|---|
| Address not allowed | You opened it by an address other than 127.0.0.1 / localhost on the viewer's port (e.g. 0.0.0.0 or a LAN IP). Use the printed URL, or start with `--lan`. |
| Cross-site request blocked | A request came from a different page/origin. Reload the viewer tab. |
| Wrong token | Token = `viewer_api_token` in `config/config.104a.bejson`; delete that row and restart for a new one. |

## Sign-in and sessions
The browser cookie holds a random server-side session id (12 h sliding, revoked by **Sign out** or by restarting the viewer) — never the access token. `python3 group_viewer.py --rotate-token` issues a new token. Scripts may send the token as `X-API-Token`.

## Delivery check
`python3 dev/verify_package_clean.py <zip>` must print CLEAN (no sessions, thumbnails, images, tokens, caches).

## Tests
`python3 -m unittest discover -s tests -v`   (40 unit/integration tests)

Browser audit (optional, needs `pip install playwright && playwright install chromium`):
`python3 tests/ui_audit_playwright.py` — builds a throwaway project + synthetic photos in a temp folder, runs the viewer at desktop/tablet/phone sizes and checks overflow, tap targets, selection bar, ribbons, keyboard/Back, bulk resolve → commit → Undo, console errors.

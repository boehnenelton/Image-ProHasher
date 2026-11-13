# Suggested improvements (ranked by value for effort)

Verified gaps first, then ideas. "Effort": S = hours, M = a day or two, L = a week+.

## 1. Worth doing next
| # | Improvement | Why | Effort |
|---|---|---|---|
| 1 | **Keep-by-folder rules** ("always keep the copy in Camera; trash Backup / WhatsApp / Downloads") with a drag-to-rank folder list, applied by Keep and by bulk Resolve | Resolution/date rules cannot express *where* you want the survivor to live; every dedupe tool people like has this | M |
| 2 | **Undo for Move/Copy** via an append-only commit journal (what moved where, when) | Only Trash is undoable today; a mistaken Move needs manual repair. The journal doubles as an audit trail | M |
| 3 | **Progress + cancel for long viewer jobs** (bulk Resolve, Verify on big groups) via polling a job status endpoint | A 1-3 minute bulk resolve currently shows only a toast | M |
| 4 | **Hard-link / same-inode detection** | Trashing one of two hard links frees no space; show "same file (link)" and exclude from "can be freed" | S |
| 5 | **Cloud-sync awareness**: warn when a folder is a Google Photos / Drive sync root | Deleting there propagates to the cloud copy | S |
| 6 | **Production server (waitress)** instead of Flask's dev server, still single-user/local | Fewer stalls with many thumbnail requests; no dev-server banner | S |

## 2. Nice to have
- Dark theme (palette allows black background): CSS variables are already centralised.
- Automated accessibility scan (axe-core) inside `tests/ui_audit_playwright.py`.
- Scheduled incremental rescans through the existing Schedules library (Termux jobs), then auto Quick-Group.
- CSV/BEJSON export of a review session (what was kept/trashed/moved) for records.
- Video files: same size+duration quick groups (no hashing needed for a first pass).
- Per-group notes and a "snooze" state besides Resolved.
- Multi-select across groups ("select every Backup copy in all groups").
- i18n of UI strings.

## 3. Known limits (documented, not bugs)
- Deep clustering is pairwise (sliding window by aspect ratio): comfortable to ~20k images, slow beyond. Quick-Group handles 30k records in ~8 s.
- Viewer sessions are in memory: restarting the viewer signs every browser out (use the `?token=` link again).
- Moves update the index, but a crash *during* a commit can leave stale index paths until the next rescan (trash is auto-recovered; moves are not).
- HEIC/HEIF needs `pip install pillow-heif`; without it those files are reported and skipped.

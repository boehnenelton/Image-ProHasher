# Security Notes (audit of V9-PKG4 → V10-PKG5)

## Fixed
| # | Finding | Fix |
|---|---|---|
| 1 | **Live viewer token shipped in config** (`viewer_api_token`) | Package ships no config; token generated per install. **Rotate the old token if V9 was shared.** |
| 2 | Path traversal: raw `session` value joined into paths (`../..`) on trash/thumb/etc. | Every session/group/record id validated as UUID. |
| 3 | Token in URL, localStorage and image URLs (leaks via logs/history/Referer) | HttpOnly SameSite=Strict cookie; token stripped from URL; never in HTML. |
| 4 | Non-constant-time token compare | `hmac.compare_digest`. |
| 5 | Move/copy silently overwrote files of the same name (data loss) | `unique_destination()`; never overwrites. |
| 6 | One failing file aborted commit with half the moves unrecorded | Per-item try/except, bookkeeping always runs, failures stay queued. |
| 7 | DNS-rebinding / cross-origin POST | Host allow-list, Origin check, JSON-only POST, CSP, nosniff, frame deny. |
| 8 | Served arbitrary file path from index | Extension allow-list + root containment (symlinks resolved). |
| 9 | Brute-force login | 10 failures / 60 s / IP limit. |
| 10 | Package shipped user thumbnails, session indexes with full photo paths, nested old zip | Excluded (§3.4 / privacy). |
| 11 | Non-atomic state/group writes (corruption on crash) | Atomic writes; crash-safe ordering + reconcile(). |

| 12 | Queueing every copy of a group could delete all of them | Server-side guard at commit: such groups are held back unless `allow_delete_all_copies` is sent from the review dialog's explicit checkbox. |
| 13 | Package could silently ship user data | `dev/verify_package_clean.py` run on every delivery (and in the test suite). |

| 14 | Origin check rejected legitimate form sign-in (`Origin: null`) | `/login` alone accepts null Origin (login CSRF is harmless: needs the token); all JSON API calls stay strict. |\n\n| 15 | Cookie held the long-lived token | Random server-side session id, 12 h sliding, revocable (Sign out), cleared on restart; raw token rejected as cookie. |
| 16 | Partial/truncated file after a failed cross-device move | `safe_move`/`safe_copy` verify before deleting the source. |
| 17 | API responses cacheable | `Cache-Control: no-store` on `/api/*`. |
| 18 | Recycled PID treated a dead scan as running | PID must belong to one of our scripts. |
| 19 | Corrupt config silently replaced (token regenerated unnoticed) | Quarantined with a logged warning. |

## Residual / by design
- Plain HTTP on 127.0.0.1; `--lan` exposes move/trash endpoints to the network (warned at start).
- `viewer_allowed_roots` defaults to the whole shared storage; narrow it in config for tighter blast radius.
- Token stored plaintext in config (chmod 600 attempted; Android shared storage may ignore it).
- Core library is frozen; its `_sync_to_bejson` uses positional access internally (not modified).

# Dead / Removed Code (V10)
- Scanner: `count_qualifying_images` (second full walk), `BEJSONState` history persistence (never survived a process), `speedhash_enabled` session flag, unused imports (`json`), per-record `mfdb_core_add_entity_record` commit loop, temp-copy-and-reopen in hash path, `__import__("math")` DCT.
- Grouper: `phase1_complete/phase2_complete` flags, per-group source reload, per-row `mfdb_core_update_entity_record` loops, duplicated config/session helpers (now imported from Scanner).
- Menu: duplicate "Select Session (Grouper)" entry, typed-path prompts, state-caching toggle.
- Viewer: `withToken()` / localStorage token handling, inline `GV_SELECTED_SESSION` script, 14-button wrapping toolbar, `Select All / Clear Selection / ... Selected` labels.

## V11
- gv_static/css/becss-components.css, becss-headers.css, becss-sidebars.css, becss-footers.css: no longer referenced (viewer uses becss-core tokens + gv-app.css). Removed.
- app.js: fixed-position cart panel (replaced by review dialog + pending pill), `TILE_PX` inline sizing (now data-density CSS).

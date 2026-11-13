#!/usr/bin/env python3
"""
Name/Library:   Cli_Image_Scanner.py
Family:         Image-ProHasher (Tool 1 of 3)
Description:    Session-based CLI that walks a directory or full-drive target and
                 writes a per-session MFDB image index (Field Map Cache access only).
                 Two scan profiles:
                   SPEED (default) - metadata only: stat + header dimensions + EXIF.
                       No pixel decode, no hashing. The Grouper then builds quick
                       groups straight from size / EXIF-time / dimension heuristics so
                       candidates can be reviewed immediately; hashes are computed only
                       on demand (viewer "Verify") for the few files in a group.
                   DEEP - adds a 4-orientation DCT perceptual hash for every image and
                       a SHA-256 for every file whose byte size collides with another
                       file (a unique size cannot be an exact duplicate).
Version:        105
Library_Version:        103.31 (MFDB)
Date Created:   2026-08-25
Date Modified:  2026-10-24
Author:         Elton Boehnen
Contact:        boehnenelton2024@gmail.com | boehnenelton2024.pages.dev | github.com/boehnenelton
Format_Creator: Elton Boehnen
RELATIONAL_ID:  5e239281-c835-4e75-ab6e-99bb41666776

ASSUMPTIONS (Policy 5.2 / 7.1):
  - record_uuid is now uuid5(session, file_path): stable across rescans, so groups
    and staged actions built before a rescan stay valid (v9 minted a fresh uuid4
    per rescan, orphaning every group's thumbnails).
  - "Hashed" is inferred from data: sha256_hash / phash_original being non-null.
    No ImageRecord schema change, so v9 sessions load unchanged.
  - The session's stored speedhash flag (which made the menu toggle a no-op once a
    session had started) is gone: profile comes from config at every Start-Scan,
    overridable with --Speed / --Deep.
  - Hidden directories and Android cache dirs are skipped (config-controlled); the
    tool's own folder is always skipped so it never indexes its own thumb cache.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Self-locating script identity (Policy 4.1)
# ---------------------------------------------------------------------------

def get_script_path() -> Path:
    return Path(__file__).resolve().parent


SCRIPT_NAME = "Cli_Image_Scanner.py"
SCRIPT_PATH = get_script_path()
VERSION = 105
SCRIPT_VERSION = VERSION
RELATIONAL_ID = "7bdc2445-6004-46c9-a7b1-82761421cc59"

# ---------------------------------------------------------------------------
# Logging (Policy 7.3: never print() for logging). Configured before bootstrap
# so a missing-library failure can be logged properly.
# ---------------------------------------------------------------------------

ANSI_RESET = "\033[0m"
ANSI_DIM = "\033[2m"
ANSI_RED_BOLD = "\033[1m\033[38;2;222;38;38m"   # #DE2626
ANSI_RED = "\033[38;2;222;38;38m"


def important(text: str) -> str:
    return f"{ANSI_RED_BOLD}{text}{ANSI_RESET}"


class ColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: ANSI_DIM, logging.INFO: ANSI_RED,
        logging.WARNING: ANSI_RED_BOLD, logging.ERROR: ANSI_RED_BOLD, logging.CRITICAL: ANSI_RED_BOLD,
    }

    def format(self, record):
        return f"{self.LEVEL_COLORS.get(record.levelno, '')}{super().format(record)}{ANSI_RESET}"


Log = logging.getLogger("Cli_Image_Scanner")
if not Log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(ColorFormatter("[%(asctime)s] %(levelname)s Cli_Image_Scanner: %(message)s", datefmt="%H:%M:%S"))
    Log.addHandler(_handler)
Log.setLevel(logging.INFO)
Log.propagate = False

# ---------------------------------------------------------------------------
# Lib bootstrap (Policy 4.3)
# ---------------------------------------------------------------------------

LIB_CORE_PATH = SCRIPT_PATH / "lib" / "Core"
LIB_APP_PATH = SCRIPT_PATH / "lib" / "ProHasher"
FALLBACK_LIB_SOURCE = Path("/storage/emulated/0/Admin/libraries/Core")

if LIB_CORE_PATH.is_dir():
    sys.path.insert(0, str(LIB_CORE_PATH))
elif FALLBACK_LIB_SOURCE.is_dir():
    sys.path.insert(0, str(FALLBACK_LIB_SOURCE))
else:
    Log.critical(f"Core library not found at {LIB_CORE_PATH} or fallback {FALLBACK_LIB_SOURCE}")
    sys.exit(20)
if LIB_APP_PATH.is_dir():
    sys.path.insert(0, str(LIB_APP_PATH))
else:
    Log.critical(f"ProHasher library not found at {LIB_APP_PATH}")
    sys.exit(20)

from lib_bejson_Core_bejson_core import (  # noqa: E402
    bejson_core_atomic_write,
    bejson_core_load_file,
    bejson_core_get_field_map,
    bejson_core_create_104a,
)
from lib_bejson_Core_mfdb_core import (  # noqa: E402
    mfdb_core_create_database,
    mfdb_core_get_stats,
)
import lib_prohasher_common as Common  # noqa: E402
from lib_prohasher_common import KVState  # noqa: E402

_PIL_AVAILABLE = Common.PIL_AVAILABLE
if _PIL_AVAILABLE:
    from PIL import Image  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BATCH_WRITE_SIZE = 50
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".tif", ".webp", ".heic", ".heif"}
ENTITY_NAME = "ImageRecord"
SESSION_NAMESPACE = uuid.UUID("6f1d3c9e-1b7a-4c52-9a3e-2d8f5b7c1a40")

IMAGE_RECORD_FIELDS = [
    {"name": "record_uuid",            "type": "string"},
    {"name": "session_id",             "type": "string"},
    {"name": "file_path",              "type": "string"},
    {"name": "file_name",              "type": "string"},
    {"name": "file_size_bytes",        "type": "integer"},
    {"name": "width",                  "type": "integer"},
    {"name": "height",                 "type": "integer"},
    {"name": "squared_dimension",      "type": "number"},
    {"name": "aspect_ratio",           "type": "number"},
    {"name": "inverted_aspect_ratio",  "type": "number"},
    {"name": "camera_model",           "type": "string"},
    {"name": "exif_make",              "type": "string"},
    {"name": "exif_datetime",          "type": "string"},
    {"name": "exif_software",          "type": "string"},
    {"name": "exif_orientation",       "type": "integer"},
    {"name": "exif_iso",               "type": "integer"},
    {"name": "exif_focal_length",      "type": "string"},
    {"name": "exif_lens_model",        "type": "string"},
    {"name": "file_created_time",      "type": "string"},
    {"name": "file_modified_time",     "type": "string"},
    {"name": "phash_original",         "type": "string"},
    {"name": "phash_rotate_90",        "type": "string"},
    {"name": "phash_rotate_180",       "type": "string"},
    {"name": "phash_rotate_270",       "type": "string"},
    {"name": "sha256_hash",            "type": "string"},
    {"name": "group_id",               "type": "string"},
    {"name": "indexed_at",             "type": "string"},
]

# (name, default, description) - additive: missing keys are appended to existing configs.
CONFIG_DEFAULTS: List[Tuple[str, Any, str]] = [
    ("drive_scan_root", "/storage/emulated/0", "Root path used for --Drive-Scan."),
    ("batch_write_size", BATCH_WRITE_SIZE, "Images processed per DB commit batch."),
    ("log_level", "INFO", "Default log level."),
    ("speedhash_mode", True, "Speed profile: metadata-only scan, quick groups from heuristics, hash on demand. false = Deep profile."),
    ("scan_skip_hidden_dirs", True, "Skip directories whose name starts with '.' (Android caches, .thumbnails, .trash)."),
    ("scan_exclude_dirs", "Android/data,Android/obb", "Comma-separated path suffixes to skip."),
    ("scan_min_file_bytes", 0, "Skip image files smaller than this many bytes (0 = keep all)."),
    ("phash_use_draft", True, "Decode JPEGs at reduced size when hashing (large speedup, tiny hash drift)."),
    ("quick_group_max_bucket", 25, "Quick groups: ignore a heuristic bucket larger than this (mass-identical junk)."),
    ("quick_group_use_filename", False, "Quick groups: also bucket by normalised filename + dimensions (more false positives)."),
    ("auto_quick_group", True, "Menu: run Quick-Group automatically after a completed Speed scan."),
    ("phash_hamming_threshold", 10, "Deep grouping: max Hamming distance (of 64 bits) for 'same image'."),
]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_DIR = SCRIPT_PATH / "config"
CONFIG_PATH = CONFIG_DIR / "config.104a.bejson"
CURRENT_SESSION_PATH = CONFIG_DIR / "current_session.104a.bejson"
SESSIONS_ROOT = SCRIPT_PATH / "sessions"
CONFIG_FIELDS = [
    {"name": "setting_name", "type": "string"},
    {"name": "setting_value", "type": "any"},
    {"name": "description", "type": "string"},
]


def _write_default_config() -> dict:
    doc = bejson_core_create_104a(
        record_type="ScriptConfig", fields=CONFIG_FIELDS,
        values=[[n, v, d] for n, v, d in CONFIG_DEFAULTS],
    )
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    bejson_core_atomic_write(str(CONFIG_PATH), doc)
    return doc


def reset_config_to_defaults() -> None:
    _write_default_config()


def ensure_config() -> dict:
    """Load config, appending any default keys a v9-era file lacks (additive migration;
    existing values - including a viewer token - are never overwritten)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    doc = bejson_core_load_file(str(CONFIG_PATH)) if CONFIG_PATH.exists() else None
    if not doc:
        if CONFIG_PATH.exists():                      # unreadable/invalid: keep it (it holds your token + settings), don't silently overwrite
            aside = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.corrupt-{int(datetime.now().timestamp())}")
            try:
                os.replace(CONFIG_PATH, aside)
                Log.error(f"Config was unreadable; saved as {aside.name} and regenerated with defaults (a NEW viewer token will be created).")
            except OSError as exc:
                Log.error(f"Config unreadable and could not be moved aside: {exc}")
        return _write_default_config()
    fmap = bejson_core_get_field_map(doc)
    have = {row[fmap["setting_name"]] for row in doc.get("Values", [])}
    changed = False
    for name, value, desc in CONFIG_DEFAULTS:
        if name not in have:
            row = [None] * len(doc["Fields"])
            row[fmap["setting_name"]] = name
            row[fmap["setting_value"]] = value
            if "description" in fmap:
                row[fmap["description"]] = desc
            doc["Values"].append(row)
            changed = True
    if changed:
        bejson_core_atomic_write(str(CONFIG_PATH), doc)
    return doc


def get_config_value(doc: dict, setting_name: str, default=None):
    fmap = bejson_core_get_field_map(doc)
    name_i, val_i = fmap["setting_name"], fmap["setting_value"]
    for row in doc.get("Values", []):
        if row[name_i] == setting_name:
            return row[val_i]
    return default


def set_config_value(setting_name: str, value: Any, description: str = "") -> None:
    doc = ensure_config()
    fmap = bejson_core_get_field_map(doc)
    for row in doc["Values"]:
        if row[fmap["setting_name"]] == setting_name:
            row[fmap["setting_value"]] = value
            break
    else:
        row = [None] * len(doc["Fields"])
        row[fmap["setting_name"]] = setting_name
        row[fmap["setting_value"]] = value
        if "description" in fmap:
            row[fmap["description"]] = description
        doc["Values"].append(row)
    bejson_core_atomic_write(str(CONFIG_PATH), doc)


def apply_log_level(config_doc: dict) -> None:
    level = str(get_config_value(config_doc, "log_level", "INFO")).upper()
    Log.setLevel(getattr(logging, level, logging.INFO))


def get_selected_session_id() -> Optional[str]:
    if not CURRENT_SESSION_PATH.exists():
        return None
    doc = bejson_core_load_file(str(CURRENT_SESSION_PATH))
    if not doc:
        return None
    fmap = bejson_core_get_field_map(doc)
    for row in doc.get("Values", []):
        if row[fmap["setting_name"]] == "selected_session_id":
            sid = row[fmap["setting_value"]]
            return sid if Common.is_valid_uuid(sid) else None
    return None


def set_selected_session_id(session_id: str) -> None:
    Common.require_uuid(session_id, "session id")
    doc = bejson_core_create_104a(
        record_type="ScriptConfig",
        fields=[{"name": "setting_name", "type": "string"}, {"name": "setting_value", "type": "any"}],
        values=[["selected_session_id", session_id]],
    )
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    bejson_core_atomic_write(str(CURRENT_SESSION_PATH), doc)


# ---------------------------------------------------------------------------
# Session paths
# ---------------------------------------------------------------------------

def session_dir(session_id: str) -> Path:
    return SESSIONS_ROOT / Common.require_uuid(session_id, "session id")


def session_meta_path(session_id: str) -> Path:
    return session_dir(session_id) / "session_meta.104a.bejson"


def scan_state_path(session_id: str) -> Path:
    return session_dir(session_id) / "persist" / "scan_state.104a.bejson"


def _legacy_scan_state_path(session_id: str) -> Path:
    return session_dir(session_id) / "persist" / "scan_state.104db.bejson"


def stop_flag_path(session_id: str) -> Path:
    return session_dir(session_id) / "persist" / "stop.flag"


def index_root(session_id: str) -> Path:
    return session_dir(session_id) / "index"


def temp_index_root(session_id: str) -> Path:
    return session_dir(session_id) / "temp_index"


def manifest_path_for(root: Path) -> str:
    return str(root / "104a.mfdb.bejson")


def list_session_ids() -> List[str]:
    if not SESSIONS_ROOT.is_dir():
        return []
    return sorted(p.name for p in SESSIONS_ROOT.iterdir() if p.is_dir() and Common.is_valid_uuid(p.name))


# ---------------------------------------------------------------------------
# Scan state (KVState: atomic, one row per key, heartbeat + crash recovery)
# ---------------------------------------------------------------------------

SCAN_STATE_DEFAULTS: Dict[str, Any] = {
    "mode": None,
    "target_path": None,
    "status": "idle",
    "generation": 0,
    "profile": None,
    "rescan_active": False,
    "run_id": None,
    "run_pid": None,
    "heartbeat_at": None,
    "last_started_at": None,
    "last_completed_at": None,
    "last_checkpoint_at": None,
    "last_checkpoint_processed": 0,
    "unreadable_files": 0,
    "last_error": None,
}


def load_scan_state(session_id: str) -> KVState:
    """Loads (migrating the v9 104db file if present) and recovers a crashed run."""
    new_path, legacy = scan_state_path(session_id), _legacy_scan_state_path(session_id)
    if not new_path.exists() and legacy.exists():
        migrated = KVState(legacy, SCAN_STATE_DEFAULTS, "ScanState")
        migrated.path = new_path
        migrated.save()
        try:
            legacy.rename(legacy.with_name(legacy.name + ".migrated"))
        except OSError:
            pass
    state = KVState(new_path, SCAN_STATE_DEFAULTS, "ScanState")
    state.recover_interrupted()
    return state


def save_scan_state(session_id: str, state: KVState) -> None:
    state.save()


def cmd_new_session(args) -> None:
    session_id = str(uuid.uuid4())
    sdir = session_dir(session_id)
    (sdir / "persist").mkdir(parents=True, exist_ok=True)
    meta = bejson_core_create_104a(
        record_type="SessionMeta",
        fields=[{"name": "key", "type": "string"}, {"name": "value", "type": "string"}],
        values=[["session_id", session_id], ["created_at", Common.utc_now_iso()]],
    )
    bejson_core_atomic_write(str(session_meta_path(session_id)), meta)
    load_scan_state(session_id).save()
    set_selected_session_id(session_id)
    Log.info(f"New session created and selected: {session_id}")


def cmd_list_sessions(args) -> None:
    selected = get_selected_session_id()
    ids = list_session_ids()
    if not ids:
        Log.info("No sessions found. Use --New-Session to create one.")
        return
    for sid in ids:
        state = load_scan_state(sid)
        marker = " <- selected" if sid == selected else ""
        Log.info(f"{sid}  mode={state.get('mode') or '(none)'}  status={state.get('status')}{marker}")


def cmd_sel_session(args) -> None:
    sid = getattr(args, "session_id", None)
    try:
        Common.require_uuid(sid, "session id")
    except ValueError:
        Log.error("Provide --Sel-Session <session_uuid>")
        return
    if not session_dir(sid).is_dir():
        Log.error(f"Session not found: {sid}")
        return
    set_selected_session_id(sid)
    Log.info(f"Selected session: {sid}")


def cmd_del_session(args) -> None:
    sid = getattr(args, "session_id", None)
    try:
        Common.require_uuid(sid, "session id")
    except ValueError:
        Log.error("Provide --Del-Session <session_uuid>")
        return
    sdir = session_dir(sid)
    if not sdir.is_dir():
        Log.error(f"Session not found: {sid}")
        return
    if load_scan_state(sid).get("status") == "running":
        Log.error("Session has a scan running; stop it first (--Stop-Scan).")
        return
    shutil.rmtree(sdir)
    if get_selected_session_id() == sid:
        CURRENT_SESSION_PATH.unlink(missing_ok=True)
    Log.info(f"Deleted session: {sid}")


def require_selected_session() -> Optional[str]:
    sid = get_selected_session_id()
    if not sid:
        Log.error("No session selected. Use --New-Session or --Sel-Session first.")
        return None
    if not session_dir(sid).is_dir():
        Log.error(f"Selected session {sid} no longer exists on disk.")
        return None
    return sid


def cmd_drive_scan(args, config_doc: dict) -> None:
    sid = require_selected_session()
    if not sid:
        return
    target = getattr(args, "drive_path", None) or get_config_value(config_doc, "drive_scan_root", "/storage/emulated/0")
    _set_scan_mode(sid, "full", target)


def cmd_dir_scan(args) -> None:
    sid = require_selected_session()
    if not sid:
        return
    dir_path = getattr(args, "dir_path", None)
    if not dir_path:
        Log.error("Provide --Dir-Scan <directory_path>")
        return
    if not Path(dir_path).is_dir():
        Log.error(f"Not a directory: {dir_path}")
        return
    _set_scan_mode(sid, "directory", str(Path(dir_path).resolve()))


def _set_scan_mode(session_id: str, mode: str, target_path: str) -> None:
    state = load_scan_state(session_id)
    existing = state.get("mode")
    if existing and existing != mode:
        Log.error(f"Session already locked to mode '{existing}'. Create a new session instead.")
        return
    if state.get("status") == "running":
        Log.error("A scan is running on this session; stop it before changing the target.")
        return
    state.set_and_save(mode=mode, target_path=target_path)
    Log.info(f"Session {session_id} mode set to '{mode}' target='{target_path}'. Use --Start-Scan to begin.")


def cmd_stop_scan(args) -> None:
    sid = require_selected_session()
    if not sid:
        return
    (session_dir(sid) / "persist").mkdir(parents=True, exist_ok=True)
    stop_flag_path(sid).write_text(Common.utc_now_iso())
    Log.info(f"Stop flag set for session {sid}. The active scan halts after the current file.")


def cmd_status(args) -> None:
    sid = require_selected_session()
    if not sid:
        return
    st = load_scan_state(sid)
    mpath = manifest_path_for(index_root(sid))
    total: Any = "-"
    if Path(mpath).exists():
        try:
            total = mfdb_core_get_stats(mpath)["entities"][0]["record_count"]
        except Exception:
            total = "?"
    Log.info(f"session={sid} mode={st.get('mode')} target={st.get('target_path')} status={st.get('status')} "
             f"profile={st.get('profile')} indexed={total} generation={st.get('generation')}")


# ---------------------------------------------------------------------------
# Image record
# ---------------------------------------------------------------------------

def stable_record_uuid(session_id: str, file_path: str) -> str:
    return str(uuid.uuid5(SESSION_NAMESPACE, f"{session_id}|{file_path}"))


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def build_image_record(file_path: Path, size: int, mtime: float, ctime: float, session_id: str,
                       deep: bool, need_sha: bool, use_draft: bool,
                       prior: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    """Metadata always; hashes only when the profile asks. `prior` (an unchanged
    record from the previous index) donates already-computed hashes."""
    try:
        with Image.open(file_path) as img:
            width, height = img.size                   # header only - no pixel decode
            exif = Common.read_exif_summary(img)
    except Exception as exc:
        if file_path.suffix.lower() in Common.HEIF_EXTENSIONS and not Common.HEIF_SUPPORT:
            Log.debug(f"HEIC/HEIF skipped (no pillow-heif): {file_path}")      # summarised once at the end of the scan
        else:
            Log.warning(f"Skipping unreadable image {file_path}: {exc}")
        return None

    phashes: Tuple[Optional[str], ...] = (None, None, None, None)
    sha = None
    if prior:
        sha = prior.get("sha256_hash")
        phashes = (prior.get("phash_original"), prior.get("phash_rotate_90"),
                   prior.get("phash_rotate_180"), prior.get("phash_rotate_270"))
    try:
        if deep and not phashes[0]:
            phashes = Common.compute_phash_set(file_path, use_draft=use_draft)
        if need_sha and not sha:
            sha = Common.sha256_of_file(file_path)
    except Exception as exc:
        Log.warning(f"Hashing failed for {file_path}: {exc} (metadata kept)")

    return {
        "record_uuid": stable_record_uuid(session_id, str(file_path)),
        "session_id": session_id,
        "file_path": str(file_path),
        "file_name": file_path.name,
        "file_size_bytes": size,
        "width": width,
        "height": height,
        "squared_dimension": (width + height) / 2.0,
        "aspect_ratio": (width / height) if height else None,
        "inverted_aspect_ratio": (height / width) if width else None,
        **exif,
        "file_created_time": _iso_from_ts(ctime),
        "file_modified_time": _iso_from_ts(mtime),
        "phash_original": phashes[0],
        "phash_rotate_90": phashes[1],
        "phash_rotate_180": phashes[2],
        "phash_rotate_270": phashes[3],
        "sha256_hash": sha,
        "group_id": None,
        "indexed_at": Common.utc_now_iso(),
    }


# ---------------------------------------------------------------------------
# Index (MFDB) helpers
# ---------------------------------------------------------------------------

def ensure_index_db(root: Path, profile: str) -> str:
    root.mkdir(parents=True, exist_ok=True)
    mpath = manifest_path_for(root)
    if not Path(mpath).exists():
        mfdb_core_create_database(
            root_dir=str(root), db_name="ImageScannerIndex",
            db_description=f"Per-session image metadata index (created by {profile} profile).",
            entities=[{
                "name": ENTITY_NAME, "file_path": "data/image_record.bejson",
                "description": "One row per indexed image.", "primary_key": "record_uuid",
                "fields": IMAGE_RECORD_FIELDS,
            }],
        )
    return mpath


def load_index_records(root: Path) -> List[dict]:
    mpath = manifest_path_for(root)
    if not Path(mpath).exists():
        return []
    doc = bejson_core_load_file(Common.entity_file_path(mpath, ENTITY_NAME))
    if not doc:
        return []
    names = [f["name"] for f in doc.get("Fields", [])]
    return [dict(zip(names, row)) for row in doc.get("Values", [])]


def commit_batch(root: Path, batch: List[Dict[str, Any]]) -> None:
    n = Common.bulk_append_records(manifest_path_for(root), ENTITY_NAME, batch)
    Log.debug(f"Committed batch of {n} record(s) to {root.name}/")


def promote_temp_index(session_id: str) -> None:
    """Swap temp -> real via rename-aside so a crash can never leave NO index."""
    tmp_root, real_root = temp_index_root(session_id), index_root(session_id)
    old_root = session_dir(session_id) / "index.old"
    if old_root.exists():
        shutil.rmtree(old_root)
    if real_root.exists():
        real_root.rename(old_root)
    tmp_root.rename(real_root)
    shutil.rmtree(old_root, ignore_errors=True)
    Log.info("Rescan complete - temp index promoted to the active index.")


# ---------------------------------------------------------------------------
# Enumeration (single walk; size/mtime captured so no second pass is needed)
# ---------------------------------------------------------------------------

def _skip_dir(rel_parts: Tuple[str, ...], name: str, skip_hidden: bool, excludes: List[Tuple[str, ...]]) -> bool:
    if skip_hidden and name.startswith("."):
        return True
    cand = rel_parts + (name,)
    for ex in excludes:
        if len(cand) >= len(ex) and cand[-len(ex):] == ex:
            return True
    return False


def enumerate_image_files(root_path: str, config_doc: dict, progress=None) -> List[Tuple[Path, int, float, float]]:
    root = Path(root_path)
    own = SCRIPT_PATH.resolve()
    skip_hidden = bool(get_config_value(config_doc, "scan_skip_hidden_dirs", True))
    excludes = [tuple(p for p in e.strip().strip("/").split("/") if p)
                for e in str(get_config_value(config_doc, "scan_exclude_dirs", "") or "").split(",") if e.strip()]
    min_bytes = int(get_config_value(config_doc, "scan_min_file_bytes", 0) or 0)
    found: List[Tuple[Path, int, float, float]] = []
    denied = 0
    last_report = 0

    def _onerror(_err):
        nonlocal denied
        denied += 1

    for dirpath, dirnames, filenames in os.walk(root, onerror=_onerror):
        here = Path(dirpath)
        try:
            rel = here.relative_to(root).parts
        except ValueError:
            rel = ()
        dirnames[:] = sorted(
            d for d in dirnames
            if not _skip_dir(rel, d, skip_hidden, excludes) and (here / d).resolve() != own
        )
        for fname in sorted(filenames):
            if Path(fname).suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            fp = here / fname
            try:
                st = fp.stat()
            except OSError:
                continue
            if st.st_size < min_bytes:
                continue
            found.append((fp, st.st_size, st.st_mtime, st.st_ctime))
        if progress and len(found) - last_report >= 200:
            last_report = len(found)
            progress(last_report)
    if denied:
        Log.warning(f"{denied} directory(ies) could not be read (permissions) and were skipped.")
    return found


# ---------------------------------------------------------------------------
# Progress journal + terminal animation
# ---------------------------------------------------------------------------

SPINNER_FRAMES = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
JOURNAL_MAX_BYTES = 1_000_000


def journal_path(session_id: str) -> Path:
    return session_dir(session_id) / "persist" / "scan_journal.log"


def append_journal(session_id: str, line: str) -> None:
    p = journal_path(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.stat().st_size > JOURNAL_MAX_BYTES:
        p.replace(p.with_name(p.name + ".1"))
    with open(p, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def render_progress_bar(done: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return "[" + ("?" * width) + "]"
    filled = int(width * min(done, total) / total)
    return "[" + ("\u2588" * filled) + ("\u2591" * (width - filled)) + "]"


def report_progress(session_id: str, done: int, total: int, batch_size: int, spinner_idx: int) -> None:
    bar = render_progress_bar(done, total)
    pct = (done / total * 100.0) if total else 100.0
    sys.stdout.write(f"\r{SPINNER_FRAMES[spinner_idx % len(SPINNER_FRAMES)]} {bar} {done}/{total} ({pct:.1f}%)      ")
    sys.stdout.flush()
    append_journal(session_id, f"[{Common.utc_now_iso()}] {bar} {done}/{total} ({pct:.1f}%) - batch of {batch_size} committed")


# ---------------------------------------------------------------------------
# Scan driver
# ---------------------------------------------------------------------------

def cmd_start_scan(args, config_doc: dict) -> None:
    if not _PIL_AVAILABLE:
        Log.error("Pillow is required. Install with: pip install Pillow")
        return
    sid = require_selected_session()
    if not sid:
        return
    apply_log_level(config_doc)

    state = load_scan_state(sid)
    if state.get("status") == "running":
        Log.error("A scan is already running on this session (another process owns it).")
        return
    mode, target = state.get("mode"), state.get("target_path")
    if not mode or not target:
        Log.error("No scan mode set for this session. Use --Drive-Scan or --Dir-Scan first.")
        return
    if not Path(target).is_dir():
        Log.error(f"Scan target is not reachable: {target}")
        return

    override = getattr(args, "profile", None)
    deep = (override == "deep") if override else not bool(get_config_value(config_doc, "speedhash_mode", True))
    profile = "deep" if deep else "speed"
    use_draft = bool(get_config_value(config_doc, "phash_use_draft", True))
    try:
        batch_size = int(get_config_value(config_doc, "batch_write_size", BATCH_WRITE_SIZE))
        if batch_size < 1:
            raise ValueError
    except (TypeError, ValueError):
        Log.warning(f"Invalid batch_write_size in config; using {BATCH_WRITE_SIZE}.")
        batch_size = BATCH_WRITE_SIZE

    real_root = index_root(sid)
    resuming_rescan = bool(state.get("rescan_active"))
    rescan = resuming_rescan or (real_root.exists() and state.get("status") == "complete")
    active_root = temp_index_root(sid) if rescan else real_root
    prior_by_path: Dict[str, dict] = {}
    if rescan:
        prior_by_path = {r["file_path"]: r for r in load_index_records(real_root) if r.get("file_path")}
        if not resuming_rescan and temp_index_root(sid).exists():
            shutil.rmtree(temp_index_root(sid))                # stale temp from an abandoned rescan
        state.set_and_save(rescan_active=True)

    ensure_index_db(active_root, profile)
    skip_set = {r["file_path"] for r in load_index_records(active_root) if r.get("file_path")}

    Log.info(f"Enumerating images under {target} ...")
    files = enumerate_image_files(target, config_doc,
                                  progress=lambda n: (sys.stdout.write(f"\r  found {n} ..."), sys.stdout.flush()))
    sys.stdout.write("\n")
    total = len(files)
    Log.info(important(f"Found {total} image file(s). Profile: {profile.upper()}."))
    append_journal(sid, f"[{Common.utc_now_iso()}] Enumerated {total} files under {target}; profile={profile}; rescan={rescan}")

    size_counts = Counter(sz for _, sz, _, _ in files) if deep else Counter()
    flag = stop_flag_path(sid)
    flag.unlink(missing_ok=True)
    state.begin_run("running")
    state.set_and_save(profile=profile, last_started_at=Common.utc_now_iso(), last_error=None)

    batch: List[Dict[str, Any]] = []
    processed = reused = unreadable = heif_skipped = 0
    spinner_idx = 0
    was_stopped = False
    failed: Optional[str] = None

    def flush() -> None:
        nonlocal batch, spinner_idx
        if not batch:
            return
        commit_batch(active_root, batch)
        done = len(skip_set) + processed
        report_progress(sid, done, total, len(batch), spinner_idx)
        spinner_idx += 1
        state.heartbeat(last_checkpoint_at=Common.utc_now_iso(), last_checkpoint_processed=done, unreadable_files=unreadable)
        batch = []

    Log.info(f"Scan starting. mode={mode} rescan={rescan} (Ctrl+C or --Stop-Scan to stop; progress is resumable)")
    try:
        for fp, size, mtime, ctime in files:
            if flag.exists():
                was_stopped = True
                break
            if str(fp) in skip_set:
                continue
            prior = prior_by_path.get(str(fp))
            if prior and (prior.get("file_size_bytes") != size or prior.get("file_modified_time") != _iso_from_ts(mtime)):
                prior = None                                    # changed on disk -> rebuild
            rec = build_image_record(fp, size, mtime, ctime, sid, deep=deep,
                                     need_sha=deep and size_counts.get(size, 0) > 1,
                                     use_draft=use_draft, prior=prior)
            if rec is None:
                unreadable += 1
                if fp.suffix.lower() in Common.HEIF_EXTENSIONS and not Common.HEIF_SUPPORT:
                    heif_skipped += 1
                continue
            if prior:
                reused += 1
            batch.append(rec)
            processed += 1
            if len(batch) >= batch_size:
                flush()
    except KeyboardInterrupt:
        was_stopped = True
    except Exception as exc:                                    # never leave status 'running'
        failed = f"{type(exc).__name__}: {exc}"
        Log.error(f"Scan aborted by error: {failed}")
    finally:
        try:
            flush()
        except Exception as exc:
            failed = failed or f"flush failed: {exc}"
            Log.error(f"Final flush failed: {exc}")
        sys.stdout.write("\n")
        flag.unlink(missing_ok=True)

    if failed:
        state.end_run("error", last_error=failed, unreadable_files=unreadable)
        append_journal(sid, f"[{Common.utc_now_iso()}] ERROR {failed}")
        return
    if was_stopped:
        state.end_run("stopped", unreadable_files=unreadable)
        Log.warning(important(f"Scan stopped. Processed {processed} image(s) this run; resume with --Start-Scan."))
        append_journal(sid, f"[{Common.utc_now_iso()}] Stopped after {processed} image(s) this run.")
        return

    state.set("generation", int(state.get("generation") or 0) + 1)
    state.set("rescan_active", False)
    state.end_run("complete", last_completed_at=Common.utc_now_iso(), unreadable_files=unreadable)
    if rescan:
        promote_temp_index(sid)
    stats = mfdb_core_get_stats(manifest_path_for(index_root(sid)))
    total_indexed = stats["entities"][0]["record_count"] if stats.get("entities") else "unknown"
    msg = (f"Scan complete [{profile}]. Processed {processed} this run"
           f"{f' ({reused} reused unchanged)' if reused else ''}; {unreadable} unreadable. Total indexed: {total_indexed}.")
    Log.info(important(msg))
    append_journal(sid, f"[{Common.utc_now_iso()}] {msg}")
    if heif_skipped:
        Log.warning(important(f"{heif_skipped} HEIC/HEIF photo(s) were SKIPPED because HEIC support is not installed. "
                              "Install it with:  pip install pillow-heif   then run --Start-Scan again."))
    if profile == "speed":
        Log.info("Next: build quick groups with CLI_Image_Grouper.py --Quick-Group (or the menu).")


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=SCRIPT_NAME, description="BEJSON/MFDB image indexing scanner.")
    p.add_argument("--New-Session", action="store_true")
    p.add_argument("--List-Session", action="store_true")
    p.add_argument("--Sel-Session", dest="session_id", metavar="SESSION_UUID")
    p.add_argument("--Del-Session", dest="del_session_id", metavar="SESSION_UUID")
    p.add_argument("--Drive-Scan", dest="drive_path", nargs="?", const="__default__", metavar="ROOT_PATH")
    p.add_argument("--Dir-Scan", dest="dir_path", metavar="DIRECTORY_PATH")
    p.add_argument("--Start-Scan", action="store_true")
    p.add_argument("--Stop-Scan", action="store_true")
    p.add_argument("--Status", action="store_true", help="Show the selected session's scan state.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--Speed", dest="profile", action="store_const", const="speed", help="Force the metadata-only profile for this run.")
    g.add_argument("--Deep", dest="profile", action="store_const", const="deep", help="Force the hashing profile for this run.")
    Common.add_standard_flags(p)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    rc = Common.handle_standard_flags(args, SCRIPT_NAME, VERSION, SCRIPT_PATH, CONFIG_PATH, reset_config_to_defaults)
    if rc is not None:
        return rc

    config_doc = ensure_config()
    apply_log_level(config_doc)

    if args.New_Session:
        cmd_new_session(args)
    elif args.List_Session:
        cmd_list_sessions(args)
    elif args.del_session_id:
        args.session_id = args.del_session_id
        cmd_del_session(args)
    elif args.session_id:
        cmd_sel_session(args)
    elif args.dir_path:
        cmd_dir_scan(args)
    elif args.drive_path is not None:
        args.drive_path = None if args.drive_path == "__default__" else args.drive_path
        cmd_drive_scan(args, config_doc)
    elif args.Stop_Scan:
        cmd_stop_scan(args)
    elif args.Status:
        cmd_status(args)
    elif args.Start_Scan:
        cmd_start_scan(args, config_doc)
    else:
        parser.print_help()
    return Common.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

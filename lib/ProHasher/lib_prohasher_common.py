#!/usr/bin/env python3
"""
Name/Library:   lib_prohasher_common.py
Family:         ProHasher (app-owned family library - NOT part of the frozen Core family)
Description:    Shared helpers for Image-ProHasher's scanner, grouper, viewer and menu.
                 Built exclusively on Core's PUBLIC API (bejson_core_*, mfdb_core_*,
                 ResilientPIDLock) - no Core file is modified. Provides:
                   - batched MFDB writes (one lock + one atomic write per batch instead of
                     one full-file rewrite per record)
                 - a thread-safe, mtime-invalidated entity cache for read-heavy viewer calls
                 - KVState: an atomic BEJSON-104a key/value state store with heartbeat +
                   stale-run detection (replaces the in-memory-only BEJSONState history)
                 - session-id / path containment validators
                 - pixel-free (stat/EXIF only) and pixel (phash/sha256) image probes
                 - standard CLI flags (--version, --register, --config, --reset)
                 Field Map Cache access only - no positional Values[] lookups.
Version:        2
Library_Version: 103.31 (MFDB) compatible
Date Created:   2026-10-18
Author:         Elton Boehnen
Contact:        boehnenelton2024@gmail.com | boehnenelton2024.pages.dev | github.com/boehnenelton
Format_Creator: Elton Boehnen
RELATIONAL_ID:  e76a302f-1796-4364-a5ac-a07f532014ad
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from lib_bejson_Core_bejson_core import (
    ResilientPIDLock,
    bejson_core_atomic_write,
    bejson_core_create_104a,
    bejson_core_get_field_map,
    bejson_core_load_file,
)
from lib_bejson_Core_mfdb_core import mfdb_core_sync_manifest_count

VERSION = 2
Log = logging.getLogger("prohasher_common")

# ---------------------------------------------------------------------------
# Deterministic exit codes (Policy 8.7)
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_PICKER_FAILURE = 10
EXIT_ENV_FAILURE = 20
EXIT_SCHEMA_FAILURE = 30

PROJECT_NAME = "Image-ProHasher"
AUTHOR_CREDIT = (
    "Elton Boehnen \u00b7 boehnenelton2024@gmail.com \u00b7 "
    "boehnenelton2024.pages.dev \u00b7 github.com/boehnenelton"
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Validation / containment
# ---------------------------------------------------------------------------
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_GROUP_ENTITY_RE = re.compile(r"^Group_[0-9a-f]{12}$")


def is_valid_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def require_uuid(value: Any, what: str = "id") -> str:
    """Raises ValueError unless value is a canonical UUID string. Session ids,
    record ids and group ids all flow into filesystem paths, so every entry
    point validates them here first (closes the path-traversal class where a
    request supplied session='../..')."""
    if not is_valid_uuid(value):
        raise ValueError(f"invalid {what}")
    return str(value).lower()


def is_group_entity_name(name: Any) -> bool:
    return isinstance(name, str) and bool(_GROUP_ENTITY_RE.match(name))


def path_within(candidate: Path | str, roots: Iterable[Path]) -> bool:
    """True if candidate (symlinks resolved) is inside any root."""
    try:
        resolved = Path(candidate).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def unique_destination(dest_dir: Path, name: str) -> Path:
    """Collision-safe destination: never silently overwrites an existing file.
    'a.jpg' -> 'a (1).jpg' -> 'a (2).jpg' ..."""
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    for n in range(1, 10000):
        candidate = dest_dir / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"no free name for {name} in {dest_dir}")


def safe_copy(src: Path | str, dest: Path | str) -> None:
    """Copy to a temp name, verify the size, then atomically publish. A failed or short copy never leaves a
    partial file under the final name and never touches the source."""
    src, dest = Path(src), Path(dest)
    if os.path.lexists(dest):
        raise FileExistsError(str(dest))
    tmp = dest.with_name(f"{dest.name}.part-{uuid.uuid4().hex[:8]}")
    try:
        shutil.copy2(src, tmp)
        if os.path.getsize(tmp) != os.path.getsize(src):
            raise OSError(f"size mismatch after copying {src.name} (disk full?)")
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def safe_move(src: Path | str, dest: Path | str) -> None:
    """Move without ever losing the file. Same filesystem: one atomic rename. Across filesystems (internal
    storage <-> SD card, common on Android): verified copy FIRST, source removed only afterwards. shutil.move
    would leave a truncated destination if the copy died midway."""
    src, dest = Path(src), Path(dest)
    if os.path.lexists(dest):
        raise FileExistsError(str(dest))
    try:
        os.rename(src, dest)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    safe_copy(src, dest)
    os.unlink(src)


# ---------------------------------------------------------------------------
# MFDB helpers (public-API only)
# ---------------------------------------------------------------------------

def _manifest_rows(manifest_path: str) -> Tuple[dict, Dict[str, int], List[list]]:
    doc = bejson_core_load_file(manifest_path)
    if not doc:
        raise FileNotFoundError(f"manifest unreadable: {manifest_path}")
    return doc, bejson_core_get_field_map(doc), doc.get("Values", [])


def entity_file_path(manifest_path: str, entity_name: str) -> str:
    doc, fmap, rows = _manifest_rows(manifest_path)
    for row in rows:
        if row[fmap["entity_name"]] == entity_name:
            rel = row[fmap["file_path"]]
            return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(manifest_path)), rel))
    raise KeyError(f"entity not in manifest: {entity_name}")


def entity_path_map(manifest_path: str) -> Dict[str, str]:
    """{entity_name: absolute file path} from ONE manifest read. entity_file_path() re-parses the whole
    manifest on every call, so looping it over G groups is O(G^2) (reconcile took 18 s at 3,600 groups)."""
    doc, fmap, rows = _manifest_rows(manifest_path)
    base = os.path.dirname(os.path.abspath(manifest_path))
    return {row[fmap["entity_name"]]: os.path.normpath(os.path.join(base, row[fmap["file_path"]])) for row in rows}


def manifest_entity_names(manifest_path: str) -> List[str]:
    if not Path(manifest_path).exists():
        return []
    doc, fmap, rows = _manifest_rows(manifest_path)
    return [row[fmap["entity_name"]] for row in rows]


def _field_order(doc: dict) -> List[str]:
    fmap = bejson_core_get_field_map(doc)
    return sorted(fmap.keys(), key=lambda k: fmap[k])


def bulk_append_records(manifest_path: str, entity_name: str, records: List[Dict[str, Any]]) -> int:
    """Append many dict records with ONE lock + ONE atomic write (+ one manifest
    count sync). Core's mfdb_core_add_entity_record rewrites the whole entity
    file and the manifest per record, which makes a scan O(n^2) in file I/O."""
    if not records:
        return 0
    path = entity_file_path(manifest_path, entity_name)
    with ResilientPIDLock(path, timeout_seconds=30):
        doc = bejson_core_load_file(path)
        if not doc:
            raise OSError(f"entity unreadable: {path}")
        order = _field_order(doc)
        values = doc.setdefault("Values", [])
        for rec in records:
            values.append([rec.get(f) for f in order])
        if not bejson_core_atomic_write(path, doc):
            raise OSError(f"atomic write failed: {path}")
    mfdb_core_sync_manifest_count(manifest_path, entity_name)
    return len(records)


def bulk_update_fields(manifest_path: str, entity_name: str, updates: Dict[str, Dict[str, Any]],
                       key_field: str = "record_uuid") -> int:
    """updates = {key_value: {field: new_value, ...}}. One lock, one write."""
    if not updates:
        return 0
    path = entity_file_path(manifest_path, entity_name)
    changed = 0
    with ResilientPIDLock(path, timeout_seconds=30):
        doc = bejson_core_load_file(path)
        if not doc:
            raise OSError(f"entity unreadable: {path}")
        fmap = bejson_core_get_field_map(doc)
        key_i = fmap[key_field]
        for row in doc.get("Values", []):
            upd = updates.get(row[key_i])
            if not upd:
                continue
            for fname, val in upd.items():
                if fname in fmap:
                    row[fmap[fname]] = val
            changed += 1
        if changed and not bejson_core_atomic_write(path, doc):
            raise OSError(f"atomic write failed: {path}")
    return changed


def bulk_remove_records(manifest_path: str, entity_name: str, keys: set, key_field: str = "record_uuid") -> int:
    if not keys:
        return 0
    path = entity_file_path(manifest_path, entity_name)
    with ResilientPIDLock(path, timeout_seconds=30):
        doc = bejson_core_load_file(path)
        if not doc:
            raise OSError(f"entity unreadable: {path}")
        fmap = bejson_core_get_field_map(doc)
        key_i = fmap[key_field]
        before = len(doc.get("Values", []))
        doc["Values"] = [row for row in doc.get("Values", []) if row[key_i] not in keys]
        removed = before - len(doc["Values"])
        if removed and not bejson_core_atomic_write(path, doc):
            raise OSError(f"atomic write failed: {path}")
    if removed:
        mfdb_core_sync_manifest_count(manifest_path, entity_name)
    return removed


def bulk_create_entities(manifest_path: str, specs: List[Dict[str, Any]]) -> int:
    """Create many entity files and register ALL of them with a single manifest
    write. specs: [{name, fields, description, primary_key, file_path_rel, records:[dict]}].
    Mirrors the on-disk shape mfdb_core_create_entity_file produces (104 entity doc
    with Parent_Hierarchy back-link + manifest row) without rewriting the manifest
    once per entity."""
    if not specs:
        return 0
    manifest_dir = os.path.dirname(os.path.abspath(manifest_path))
    written: List[str] = []
    try:
        for spec in specs:
            rel = spec.get("file_path_rel") or f"data/{spec['name'].lower()}.bejson"
            resolved = os.path.normpath(os.path.join(manifest_dir, rel))
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            order = [f["name"] for f in spec["fields"]]
            entity_doc = {
                "Format": "BEJSON",
                "Format_Version": "104",
                "Format_Creator": "Elton Boehnen",
                "Parent_Hierarchy": os.path.relpath(manifest_path, os.path.dirname(resolved)),
                "Records_Type": [spec["name"]],
                "Fields": spec["fields"],
                "Values": [[rec.get(f) for f in order] for rec in spec.get("records", [])],
            }
            if not bejson_core_atomic_write(resolved, entity_doc):
                raise OSError(f"write failed: {resolved}")
            written.append(resolved)

        with ResilientPIDLock(manifest_path, timeout_seconds=30):
            mdoc = bejson_core_load_file(manifest_path)
            fn_list = [f["name"] for f in mdoc["Fields"]]
            for spec in specs:
                rel = spec.get("file_path_rel") or f"data/{spec['name'].lower()}.bejson"
                row = []
                for fn in fn_list:
                    if fn == "entity_name":
                        row.append(spec["name"])
                    elif fn == "file_path":
                        row.append(rel)
                    elif fn == "description":
                        row.append(spec.get("description") or None)
                    elif fn == "record_count":
                        row.append(len(spec.get("records", [])))
                    elif fn == "schema_version":
                        row.append("1.0")
                    elif fn == "primary_key":
                        row.append(spec.get("primary_key") or None)
                    else:
                        row.append(None)
                mdoc["Values"].append(row)
            if not bejson_core_atomic_write(manifest_path, mdoc):
                raise OSError(f"manifest write failed: {manifest_path}")
    except Exception:
        # Manifest never referenced these files -> remove the orphans so a retry is clean.
        for p in written:
            try:
                os.unlink(p)
            except OSError:
                pass
        raise
    return len(specs)


def remove_entity_registration(manifest_path: str, entity_name: str) -> bool:
    """Crash-safe entity removal: the manifest row is removed FIRST (one atomic
    write), the data file second. A crash in between leaves a harmless orphan
    file, never a manifest row pointing at a missing file (the old order did)."""
    with ResilientPIDLock(manifest_path, timeout_seconds=30):
        mdoc = bejson_core_load_file(manifest_path)
        if not mdoc:
            return False
        fmap = bejson_core_get_field_map(mdoc)
        name_i, path_i = fmap["entity_name"], fmap["file_path"]
        target_rel = None
        kept = []
        for row in mdoc["Values"]:
            if row[name_i] == entity_name:
                target_rel = row[path_i]
            else:
                kept.append(row)
        if target_rel is None:
            return False
        mdoc["Values"] = kept
        if not bejson_core_atomic_write(manifest_path, mdoc):
            raise OSError(f"manifest write failed: {manifest_path}")
    data_file = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(manifest_path)), target_rel))
    try:
        os.unlink(data_file)
    except OSError:
        pass
    return True


# ---------------------------------------------------------------------------
# Entity cache (read path for the viewer)
# ---------------------------------------------------------------------------

class EntityCache:
    """mtime+size invalidated cache of parsed entity files. The viewer used to
    re-parse the full 1.5 MB ImageRecord file for every thumbnail request and
    every group file for every lookup; this makes those O(1) after first read."""

    def __init__(self, max_entries: int = 256):
        self._lock = threading.RLock()
        self._data: "OrderedDict[str, tuple]" = OrderedDict()
        self._max = max_entries

    def load(self, path: str) -> Tuple[List[dict], Dict[str, dict]]:
        try:
            st = os.stat(path)
        except OSError:
            return [], {}
        sig = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._data.get(path)
            if hit and hit[0] == sig:
                self._data.move_to_end(path)
                return hit[1], hit[2]
        doc = bejson_core_load_file(path)
        if not doc:
            return [], {}
        names = [f["name"] for f in doc.get("Fields", [])]
        rows = [dict(zip(names, r)) for r in doc.get("Values", [])]
        by_uuid = {r["record_uuid"]: r for r in rows if r.get("record_uuid")}
        with self._lock:
            self._data[path] = (sig, rows, by_uuid)
            self._data.move_to_end(path)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
        return rows, by_uuid

    def invalidate(self, path: Optional[str] = None) -> None:
        with self._lock:
            if path is None:
                self._data.clear()
            else:
                self._data.pop(path, None)


# ---------------------------------------------------------------------------
# KVState - atomic 104a key/value state with heartbeat + stale-run detection
# ---------------------------------------------------------------------------

def pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


OWNER_MARKERS = ("Cli_Image_Scanner", "CLI_Image_Grouper", "Menu_Image_Suite", "group_viewer")


def pid_is_ours(pid: Optional[int], markers: Tuple[str, ...] = OWNER_MARKERS) -> bool:
    """Android recycles PIDs aggressively; 'alive' alone can be some unrelated app. Where /proc is readable,
    require the process command line to be one of our scripts. If it cannot be read, fall back to 'alive'."""
    if not pid_alive(pid):
        return False
    try:
        cmd = Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
    except OSError:
        return True
    return any(m in cmd for m in markers)


class KVState:
    """Why this replaces BEJSONState for lifecycle state:
      * BEJSONState keeps its History rows in memory only; every load_* call
        built a fresh object, so the claimed undo trail never survived a
        process boundary, while each set() appended a full snapshot row that
        was then rewritten on every batch (unbounded growth, non-atomic write).
      * This store holds exactly one row per key, writes atomically, tolerates
        a corrupt file (quarantines it, falls back to defaults), migrates the
        legacy 104db StateNode layout, and carries run metadata (run_id, pid,
        heartbeat) so a crash is detected instead of leaving 'running' forever.
    """

    FIELDS = [
        {"name": "key", "type": "string"},
        {"name": "value", "type": "any"},
        {"name": "updated_at", "type": "string"},
    ]
    STALE_HEARTBEAT_SECONDS = 120

    def __init__(self, path: Path, defaults: Dict[str, Any], record_type: str = "State"):
        self.path = Path(path)
        self.defaults = dict(defaults)
        self.record_type = record_type
        self.data: Dict[str, Any] = dict(defaults)
        self._stamps: Dict[str, str] = {}
        self._lock = threading.RLock()
        self.load()

    # -- io ---------------------------------------------------------------
    def load(self) -> "KVState":
        with self._lock:
            self.data = dict(self.defaults)
            self._stamps = {}
            if not self.path.exists():
                return self
            try:
                doc = json.loads(self.path.read_text(encoding="utf-8"))
                fmap = bejson_core_get_field_map(doc)
                if "Record_Type_Parent" in fmap:          # legacy 104db StateNode layout
                    rt_i, k_i, v_i = fmap["Record_Type_Parent"], fmap["key"], fmap["value"]
                    for row in doc.get("Values", []):
                        if row[rt_i] == "StateNode" and row[k_i] is not None:
                            raw = row[v_i]
                            self.data[row[k_i]] = json.loads(raw) if isinstance(raw, str) else raw
                else:
                    k_i, v_i = fmap["key"], fmap["value"]
                    t_i = fmap.get("updated_at")
                    for row in doc.get("Values", []):
                        self.data[row[k_i]] = row[v_i]
                        if t_i is not None:
                            self._stamps[row[k_i]] = row[t_i]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                quarantine = self.path.with_name(self.path.name + f".corrupt-{int(datetime.now().timestamp())}")
                try:
                    os.replace(self.path, quarantine)
                except OSError:
                    pass
                Log.error(f"State file unreadable ({exc}); quarantined as {quarantine.name}, using defaults.")
                self.data = dict(self.defaults)
            return self

    def save(self) -> None:
        with self._lock:
            now = utc_now_iso()
            for k in self.data:
                self._stamps.setdefault(k, now)
            doc = bejson_core_create_104a(
                record_type=self.record_type,
                fields=self.FIELDS,
                values=[[k, v, self._stamps.get(k, now)] for k, v in self.data.items()],
                Schema_Version="2",
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not bejson_core_atomic_write(str(self.path), doc):
                raise OSError(f"state write failed: {self.path}")

    # -- access -----------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.data[key] = value
            self._stamps[key] = utc_now_iso()

    def update(self, **kv: Any) -> None:
        with self._lock:
            for k, v in kv.items():
                self.set(k, v)

    def set_and_save(self, **kv: Any) -> None:
        self.update(**kv)
        self.save()

    # -- run lifecycle ----------------------------------------------------
    def begin_run(self, status: str = "running") -> str:
        run_id = str(uuid.uuid4())
        self.set_and_save(status=status, run_id=run_id, run_pid=os.getpid(), heartbeat_at=utc_now_iso())
        return run_id

    def heartbeat(self, **extra: Any) -> None:
        self.update(heartbeat_at=utc_now_iso(), **extra)
        self.save()

    def end_run(self, status: str, **extra: Any) -> None:
        self.set_and_save(status=status, run_pid=None, heartbeat_at=utc_now_iso(), **extra)

    def recover_interrupted(self, running_statuses: Tuple[str, ...] = ("running",)) -> bool:
        """If the file says 'running' but its owner process is gone (or its
        heartbeat is stale) the run crashed: mark it 'interrupted' so the next
        start resumes deliberately instead of trusting a lie."""
        status = self.get("status")
        if status not in running_statuses and not str(status).startswith("running"):
            return False
        pid = self.get("run_pid")
        hb = self.get("heartbeat_at")
        stale = True
        if hb:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb)).total_seconds()
                stale = age > self.STALE_HEARTBEAT_SECONDS
            except ValueError:
                stale = True
        if pid and pid == os.getpid():
            return False  # this very process owns the run (e.g. the menu's worker thread)
        if pid and pid_is_ours(pid):
            return False  # one of our scripts still owns it, even if a slow batch made the heartbeat stale
        self.set_and_save(status="interrupted", run_pid=None)
        return True


# ---------------------------------------------------------------------------
# Image probing - cheap (no pixel decode) and expensive (hash) paths
# ---------------------------------------------------------------------------
try:
    from PIL import Image, ExifTags, ImageOps
    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    PIL_AVAILABLE = False

HEIF_SUPPORT = False
if PIL_AVAILABLE:
    try:                                   # optional: pip install pillow-heif  (phones shoot HEIC; stock Pillow cannot open it)
        import pillow_heif
        pillow_heif.register_heif_opener()
        HEIF_SUPPORT = True
    except Exception:                      # not installed / unsupported platform: HEIC files are reported, not silently dropped
        HEIF_SUPPORT = False
HEIF_EXTENSIONS = (".heic", ".heif")

PHASH_SRC = 32
PHASH_LOW = 8
_COS = [[math.cos((math.pi / PHASH_SRC) * (i + 0.5) * k) for i in range(PHASH_SRC)] for k in range(PHASH_LOW)]
_SCALE = [1.0 / math.sqrt(2.0)] + [2.0] * (PHASH_LOW - 1)  # identical scaling to the v9 transform

_EXIF_IFD = 0x8769
_BAD_EXIF_DATES = ("0000:00:00", "    :  :  ", "0000-00-00")


def _exif_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    value = str(value).replace("\x00", "").strip()
    return value or None


def read_exif_summary(pil_image) -> Dict[str, Any]:
    """Header-only EXIF read via the public getexif() API (the v9 code used the
    private, JPEG-only _getexif(), so PNG/WebP/HEIC EXIF was silently dropped)."""
    out: Dict[str, Any] = {
        "camera_model": None, "exif_make": None, "exif_datetime": None, "exif_software": None,
        "exif_orientation": None, "exif_iso": None, "exif_focal_length": None, "exif_lens_model": None,
    }
    try:
        exif = pil_image.getexif()
    except Exception:
        return out
    if not exif:
        return out
    merged = dict(exif)
    try:
        merged.update(exif.get_ifd(_EXIF_IFD))
    except Exception:
        pass
    tags = {ExifTags.TAGS.get(k, k): v for k, v in merged.items()}
    out["camera_model"] = _exif_text(tags.get("Model"))
    sub = _exif_text(merged.get(0x9291))                       # SubSecTimeOriginal: tells burst frames apart
    out["exif_make"] = _exif_text(tags.get("Make"))
    stamp = _exif_text(tags.get("DateTimeOriginal")) or _exif_text(tags.get("DateTimeDigitized")) or _exif_text(tags.get("DateTime"))
    if stamp and not stamp.startswith(_BAD_EXIF_DATES):
        out["exif_datetime"] = f"{stamp}.{sub}" if sub and sub.isdigit() else stamp
    out["exif_software"] = _exif_text(tags.get("Software"))
    orient = tags.get("Orientation")
    out["exif_orientation"] = int(orient) if isinstance(orient, int) else None
    iso = tags.get("ISOSpeedRatings") or tags.get("PhotographicSensitivity")
    if isinstance(iso, (tuple, list)):
        iso = iso[0] if iso else None
    out["exif_iso"] = int(iso) if isinstance(iso, (int, float)) else None
    focal = tags.get("FocalLength")
    out["exif_focal_length"] = str(focal) if focal is not None else None
    out["exif_lens_model"] = _exif_text(tags.get("LensModel"))
    return out


def sha256_of_file(path: Path | str, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _dct_low_freq(matrix: List[List[float]]) -> List[float]:
    """Top-left 8x8 block of the 32x32 2-D DCT, computed directly (10k MACs)
    instead of the full transform (65k MACs with a __import__('math') in the
    innermost loop). Same formula and scaling as v9 (verified to ~1e-11 on the
    coefficients). Note the INPUT pixels differ from v9 hashes: v9 hashed a
    lossy re-saved temp copy, v10 hashes the decoded original."""
    n, k = PHASH_SRC, PHASH_LOW
    rows = []
    for y in range(n):
        row = matrix[y]
        rows.append([sum(row[i] * _COS[kx][i] for i in range(n)) * _SCALE[kx] for kx in range(k)])
    flat: List[float] = []
    for ky in range(k):
        cos_row = _COS[ky]
        for kx in range(k):
            flat.append(sum(rows[y][kx] * cos_row[y] for y in range(n)) * _SCALE[ky])
    return flat


def _bits_to_hex(flat: List[float]) -> str:
    no_dc = flat[1:]
    median = sorted(no_dc)[len(no_dc) // 2]
    bits = "".join("1" if v > median else "0" for v in flat)
    return f"{int(bits, 2):016x}"


def _rot90(matrix: List[List[float]]) -> List[List[float]]:
    return [list(r) for r in zip(*matrix[::-1])]


def compute_phash_set(path: Path | str, use_draft: bool = True) -> Tuple[str, str, str, str]:
    """(orig, rot90, rot180, rot270). Decodes the file ONCE, shrinks to 32x32
    greyscale, then rotates the tiny matrix (v9 re-saved a temp copy, re-opened
    it and re-resized the full image four times)."""
    with Image.open(path) as img:
        if use_draft:
            try:
                img.draft("L", (PHASH_SRC * 4, PHASH_SRC * 4))
            except Exception:
                pass
        small = img.convert("L").resize((PHASH_SRC, PHASH_SRC), Image.LANCZOS)
    px = list(small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata())
    m0 = [[float(px[y * PHASH_SRC + x]) for x in range(PHASH_SRC)] for y in range(PHASH_SRC)]
    m1 = _rot90(m0)
    m2 = _rot90(m1)
    m3 = _rot90(m2)
    return tuple(_bits_to_hex(_dct_low_freq(m)) for m in (m0, m1, m2, m3))  # type: ignore[return-value]


def hamming_hex(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def phash_min_distance(rec_a: dict, rec_b: dict) -> Optional[int]:
    """Min Hamming distance over the 4 relative orientations; None if either
    record has no perceptual hash yet."""
    a0 = rec_a.get("phash_original")
    if not a0:
        return None
    a = int(a0, 16)
    best = 64
    seen = False
    for f in ("phash_original", "phash_rotate_90", "phash_rotate_180", "phash_rotate_270"):
        v = rec_b.get(f)
        if not v:
            continue
        seen = True
        d = (a ^ int(v, 16)).bit_count()
        if d < best:
            best = d
    return best if seen else None


# ---------------------------------------------------------------------------
# Standard CLI flags (Policy 4.7): --version, --register, --config, --reset
# ---------------------------------------------------------------------------

def add_standard_flags(parser) -> None:
    g = parser.add_argument_group("standard flags")
    g.add_argument("--version", action="store_true", help="Print version and exit.")
    g.add_argument("--register", action="store_true", help="Register this project with the master project database.")
    g.add_argument("--config", action="store_true", help="Show the config file path and its current settings.")
    g.add_argument("--reset", action="store_true", help="Back up and reset config to defaults (sessions are NOT touched).")


def _project_index_root() -> Optional[str]:
    root = os.environ.get("PROJECT_INDEX_ROOT")
    if root:
        return root
    for cand in (Path("/storage/emulated/0/.env/user/paths.json"),):
        if cand.exists():
            try:
                doc = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(doc, dict):
                    return doc.get("PROJECT_INDEX_ROOT")
                fmap = bejson_core_get_field_map(doc)
                for row in doc.get("Values", []):
                    if row[fmap.get("name", 0)] == "PROJECT_INDEX_ROOT":
                        return row[fmap.get("value", 1)]
            except Exception:
                continue
    return None


def handle_standard_flags(args, script_name: str, version: int, script_path: Path,
                          config_path: Path, config_reset: Callable[[], None]) -> Optional[int]:
    """Returns an exit code if a standard flag was handled, else None."""
    if getattr(args, "version", False):
        sys.stdout.write(f"{script_name} v{version} ({PROJECT_NAME})\n")
        return EXIT_OK
    if getattr(args, "config", False):
        sys.stdout.write(f"config file: {config_path}\n")
        doc = bejson_core_load_file(str(config_path)) if config_path.exists() else None
        if doc:
            fmap = bejson_core_get_field_map(doc)
            for row in doc.get("Values", []):
                name, val = row[fmap["setting_name"]], row[fmap["setting_value"]]
                if "token" in str(name).lower():
                    val = "********"
                sys.stdout.write(f"  {name} = {val}\n")
        else:
            sys.stdout.write("  (not created yet - runs with defaults)\n")
        return EXIT_OK
    if getattr(args, "reset", False):
        if config_path.exists():
            backups = script_path / "backups"
            backups.mkdir(parents=True, exist_ok=True)
            dest = backups / f"{config_path.name}.{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak"
            shutil.copy2(config_path, dest)
            Log.info(f"Config backed up to {dest}")
        config_reset()
        Log.info("Config reset to defaults.")
        return EXIT_OK
    if getattr(args, "register", False):
        root = _project_index_root()
        reg = Path(root) / "cli_project_register.py" if root else None
        if not reg or not reg.exists():
            Log.error("PROJECT_INDEX_ROOT not resolvable (env or paths.json); cannot register.")
            return EXIT_ENV_FAILURE
        rc = subprocess.run([sys.executable, str(reg), "--register", str(script_path)]).returncode
        return EXIT_OK if rc == 0 else EXIT_ENV_FAILURE
    return None

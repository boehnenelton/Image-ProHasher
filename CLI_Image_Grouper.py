#!/usr/bin/env python3
"""
Name/Library:   CLI_Image_Grouper.py
Family:         Image-ProHasher (Tool 2 of 3)
Description:    Builds review groups from a Cli_Image_Scanner session index.
                 QUICK-GROUP (the Speed profile's grouper, seconds not hours):
                     buckets images by cheap metadata heuristics - identical byte size +
                     dimensions, identical EXIF capture time + camera, optionally same
                     filename + dimensions - merges overlapping buckets, and writes review
                     groups immediately. Each group records WHY it was formed
                     (match_basis) so you can judge candidates fast. Nothing is hashed.
                     The viewer's Verify action hashes only the files in one group.
                 START-GROUPING (Deep): backfills missing hashes (SHA-256 for size
                     collisions, 4-orientation pHash), flags exact duplicates, then
                     clusters by pHash Hamming distance (sliding window over the aspect
                     ratio, union-find, 4 relative-orientation comparisons).
                 Group writes are crash-safe: group entities are created first, the
                 GroupIndex is derived from them, and reconcile() repairs any gap.
                 Field Map Cache access only - no positional Values[] lookups.
Version:        106
Library_Version:        103.31 (MFDB)
Date Created:   2026-08-25
Date Modified:  2026-10-24
Author:         Elton Boehnen
Contact:        boehnenelton2024@gmail.com | boehnenelton2024.pages.dev | github.com/boehnenelton
Format_Creator: Elton Boehnen
RELATIONAL_ID:  0dd9a3a3-9a91-4e53-a033-85ff486d1ead

ASSUMPTIONS (Policy 5.2 / 7.1):
  - Quick-Group only considers images not already in a group and not "dismissed"
    (the viewer's "Not a duplicate" decision is remembered in persist/dismissed so a
    re-run does not re-propose it). --Rebuild dissolves unresolved groups first.
  - Heuristic buckets larger than quick_group_max_bucket are ignored (a bucket of 400
    identical-size icons is junk, not a duplicate set); merged clusters over 100 are skipped.
  - Deep clustering compares the original hash of A against all 4 orientations of B.
    Comparing 4x4 pairs (v9) is redundant: relative rotation r(A,B) is already covered.
  - Phase "complete" flags are gone: every run is incremental over ungrouped rows, so a
    rescan that adds files is picked up instead of ignored (v9 trusted phase1_complete).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import Cli_Image_Scanner as Scanner          # also performs the lib/Core + lib/ProHasher bootstrap
from Cli_Image_Scanner import (               # noqa: F401  (re-exported for the menu/viewer)
    SESSIONS_ROOT, CONFIG_DIR, CONFIG_PATH, CURRENT_SESSION_PATH,
    get_selected_session_id, set_selected_session_id, session_dir, ensure_config, get_config_value,
    IMAGE_RECORD_FIELDS as SOURCE_IMAGE_FIELDS,
)
import lib_prohasher_common as Common
from lib_prohasher_common import KVState
from lib_bejson_Core_bejson_core import (
    bejson_core_atomic_write, bejson_core_load_file, bejson_core_get_field_map, bejson_core_create_104a,
)
from lib_bejson_Core_mfdb_core import mfdb_core_create_database, mfdb_core_get_stats

SCRIPT_NAME = "CLI_Image_Grouper.py"
SCRIPT_PATH = Path(__file__).resolve().parent
VERSION = 106
SCRIPT_VERSION = VERSION
RELATIONAL_ID = "2d81f634-1e27-4532-8f4f-a4ded5e25dc8"

important = Scanner.important
Log = logging.getLogger("CLI_Image_Grouper")
if not Log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(Scanner.ColorFormatter("[%(asctime)s] %(levelname)s CLI_Image_Grouper: %(message)s", datefmt="%H:%M:%S"))
    Log.addHandler(_h)
Log.setLevel(logging.INFO)
Log.propagate = False

# ---------------------------------------------------------------------------
# Constants / schemas
# ---------------------------------------------------------------------------

GROUP_INDEX_ENTITY = "GroupIndex"
SOURCE_ENTITY = Scanner.ENTITY_NAME
QUICK_CLUSTER_CAP = 100
PHASH_RATIO_TOLERANCE = 0.03
CHUNK_GROUPS = 300

B_SIZE = "same size + dimensions"
B_EXIF = "same EXIF time + camera"
B_NAME = "same filename + dimensions"
B_SHA = "identical content (SHA-256)"
B_PHASH = "visually similar (pHash)"

GROUP_INDEX_FIELDS = [
    {"name": "record_uuid",       "type": "string"},
    {"name": "session_id",        "type": "string"},
    {"name": "file_path",         "type": "string"},
    {"name": "sha256_hash",       "type": "string"},
    {"name": "phash_original",    "type": "string"},
    {"name": "phash_rotate_90",   "type": "string"},
    {"name": "phash_rotate_180",  "type": "string"},
    {"name": "phash_rotate_270",  "type": "string"},
    {"name": "is_duplicate",      "type": "boolean"},
    {"name": "duplicate_of",      "type": "string"},
    {"name": "group_id",          "type": "string"},
    {"name": "added_at",          "type": "string"},
]
# Per-group entity = full source record + the two grouper columns (appended, never reordered).
GROUP_ENTITY_FIELDS = list(SOURCE_IMAGE_FIELDS) + [
    {"name": "is_duplicate", "type": "boolean"},
    {"name": "match_basis",  "type": "string"},
]
IMAGE_RECORD_FIELDS = GROUP_ENTITY_FIELDS     # v9 name kept for callers

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def source_index_manifest(session_id: str) -> str:
    return str(session_dir(session_id) / "index" / "104a.mfdb.bejson")


def group_db_root(session_id: str) -> Path:
    return session_dir(session_id) / "groups"


def group_db_manifest(session_id: str) -> str:
    return str(group_db_root(session_id) / "104a.mfdb.bejson")


def group_state_path(session_id: str) -> Path:
    return session_dir(session_id) / "persist" / "group_state.104a.bejson"


def group_stop_flag_path(session_id: str) -> Path:
    return session_dir(session_id) / "persist" / "group_stop.flag"


def image_holder_path(session_id: str) -> Path:
    return session_dir(session_id) / "image_holder"


def dismissed_path(session_id: str) -> Path:
    return session_dir(session_id) / "persist" / "dismissed.104a.bejson"


def require_selected_session() -> Optional[str]:
    sid = get_selected_session_id()
    if not sid:
        Log.error("No session selected. Use --Sel-Session <uuid> (session must exist from Cli_Image_Scanner.py).")
        return None
    if not session_dir(sid).is_dir():
        Log.error(f"Selected session {sid} no longer exists on disk.")
        return None
    if not Path(source_index_manifest(sid)).exists():
        Log.error(f"Session {sid} has no image index yet. Run Cli_Image_Scanner.py --Start-Scan first.")
        return None
    return sid


# ---------------------------------------------------------------------------
# Group state (KVState)
# ---------------------------------------------------------------------------
GROUP_STATE_DEFAULTS: Dict[str, Any] = {
    "status": "idle", "method": None, "run_id": None, "run_pid": None, "heartbeat_at": None,
    "last_started_at": None, "last_completed_at": None, "groups_formed_last_run": 0,
    "source_generation": 0, "last_error": None,
}


def load_group_state(session_id: str) -> KVState:
    st = KVState(group_state_path(session_id), GROUP_STATE_DEFAULTS, "GroupState")
    st.recover_interrupted(("running", "running_phase1", "running_phase2", "running_quick"))
    return st


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def load_source_records(session_id: str) -> List[dict]:
    doc = bejson_core_load_file(Common.entity_file_path(source_index_manifest(session_id), SOURCE_ENTITY))
    if not doc:
        return []
    names = [f["name"] for f in doc.get("Fields", [])]
    return [dict(zip(names, r)) for r in doc.get("Values", [])]


def ensure_group_db(session_id: str) -> str:
    root = group_db_root(session_id)
    root.mkdir(parents=True, exist_ok=True)
    mpath = group_db_manifest(session_id)
    if not Path(mpath).exists():
        mfdb_core_create_database(
            root_dir=str(root), db_name="ImageGrouperIndex",
            db_description="Review-group index for this session (quick heuristic + deep hash groups).",
            entities=[{
                "name": GROUP_INDEX_ENTITY, "file_path": "data/group_index.bejson",
                "description": "One row per source image: dup flag, then group_id once grouped.",
                "primary_key": "record_uuid", "fields": GROUP_INDEX_FIELDS,
            }],
        )
    return mpath


def group_index_rows(session_id: str) -> List[dict]:
    mpath = group_db_manifest(session_id)
    if not Path(mpath).exists():
        return []
    doc = bejson_core_load_file(Common.entity_file_path(mpath, GROUP_INDEX_ENTITY))
    if not doc:
        return []
    names = [f["name"] for f in doc.get("Fields", [])]
    return [dict(zip(names, r)) for r in doc.get("Values", [])]


def group_entity_names(session_id: str) -> List[str]:
    return [n for n in Common.manifest_entity_names(group_db_manifest(session_id)) if Common.is_group_entity_name(n)]


def entity_name_for(group_id: str) -> str:
    return f"Group_{group_id.replace('-', '')[:12]}"


def _find_group_entity_name(session_id: str, group_id: str) -> Optional[str]:
    if not Common.is_valid_uuid(group_id):
        return None
    mpath = group_db_manifest(session_id)
    if not Path(mpath).exists():
        return None
    target = entity_name_for(group_id)
    return target if target in Common.manifest_entity_names(mpath) else None


def load_group_members(session_id: str, entity_name: str, path_map: Optional[Dict[str, str]] = None) -> List[dict]:
    path = (path_map or {}).get(entity_name) or Common.entity_file_path(group_db_manifest(session_id), entity_name)
    doc = bejson_core_load_file(path)
    if not doc:
        return []
    names = [f["name"] for f in doc.get("Fields", [])]
    return [dict(zip(names, r)) for r in doc.get("Values", [])]


# ---- dismissed ("Not a duplicate") ----------------------------------------

def load_dismissed(session_id: str) -> Set[str]:
    p = dismissed_path(session_id)
    doc = bejson_core_load_file(str(p)) if p.exists() else None
    if not doc:
        return set()
    fmap = bejson_core_get_field_map(doc)
    return {row[fmap["record_uuid"]] for row in doc.get("Values", [])}


def add_dismissed(session_id: str, record_uuids: List[str]) -> None:
    current = load_dismissed(session_id) | set(record_uuids)
    doc = bejson_core_create_104a("DismissedRecord", [{"name": "record_uuid", "type": "string"}],
                                  [[u] for u in sorted(current)])
    (session_dir(session_id) / "persist").mkdir(parents=True, exist_ok=True)
    bejson_core_atomic_write(str(dismissed_path(session_id)), doc)


def clear_dismissed(session_id: str) -> None:
    dismissed_path(session_id).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# GroupIndex sync / reconcile
# ---------------------------------------------------------------------------

def sync_group_index(session_id: str, source: Optional[List[dict]] = None) -> int:
    """Add GroupIndex rows for source records that lack one (bulk, one write)."""
    ensure_group_db(session_id)
    source = source if source is not None else load_source_records(session_id)
    have = {r["record_uuid"] for r in group_index_rows(session_id)}
    now = Common.utc_now_iso()
    new_rows = [{
        "record_uuid": s["record_uuid"], "session_id": session_id, "file_path": s.get("file_path"),
        "sha256_hash": s.get("sha256_hash"), "phash_original": s.get("phash_original"),
        "phash_rotate_90": s.get("phash_rotate_90"), "phash_rotate_180": s.get("phash_rotate_180"),
        "phash_rotate_270": s.get("phash_rotate_270"), "is_duplicate": False, "duplicate_of": None,
        "group_id": None, "added_at": now,
    } for s in source if s["record_uuid"] not in have]
    Common.bulk_append_records(group_db_manifest(session_id), GROUP_INDEX_ENTITY, new_rows)
    return len(new_rows)


def reconcile(session_id: str) -> Dict[str, int]:
    """Make GroupIndex.group_id agree with the group entities (entities are the truth).
    Repairs any gap left by a crash between 'entity written' and 'index updated'."""
    mpath = group_db_manifest(session_id)
    uuid_to_gid: Dict[str, str] = {}
    empties: List[str] = []
    pmap = Common.entity_path_map(mpath)
    for name in [n for n in pmap if Common.is_group_entity_name(n)]:
        rows = load_group_members(session_id, name, pmap)
        if not rows:
            empties.append(name)
            continue
        for r in rows:
            uuid_to_gid[r["record_uuid"]] = r.get("group_id")
    for name in empties:
        Common.remove_entity_registration(mpath, name)
    fixes: Dict[str, Dict[str, Any]] = {}
    for row in group_index_rows(session_id):
        want = uuid_to_gid.get(row["record_uuid"])
        if row.get("group_id") != want:
            fixes[row["record_uuid"]] = {"group_id": want}
    Common.bulk_update_fields(mpath, GROUP_INDEX_ENTITY, fixes)
    return {"index_rows_fixed": len(fixes), "empty_groups_removed": len(empties)}


# ---------------------------------------------------------------------------
# Group create / dissolve (atomic ordering)
# ---------------------------------------------------------------------------

def create_groups_bulk(session_id: str, clusters: List[dict], source_by_uuid: Dict[str, dict]) -> int:
    """clusters: [{members:[uuid], dups:set(uuid), basis:str}]. Entities first (one manifest
    write per chunk), GroupIndex second."""
    mpath = group_db_manifest(session_id)
    existing = set(Common.manifest_entity_names(mpath))
    formed = 0
    for start in range(0, len(clusters), CHUNK_GROUPS):
        specs, assign = [], {}
        for cl in clusters[start:start + CHUNK_GROUPS]:
            members = [u for u in cl["members"] if u in source_by_uuid]
            if len(members) < 2:
                continue
            gid = cl.get("group_id") or str(uuid.uuid4())
            name = entity_name_for(gid)
            while name in existing and not cl.get("group_id"):
                gid = str(uuid.uuid4())
                name = entity_name_for(gid)
            existing.add(name)
            records = []
            for u in members:
                rec = dict(source_by_uuid[u])
                rec["group_id"] = gid
                rec["is_duplicate"] = u in cl.get("dups", set())
                rec["match_basis"] = cl["basis"]
                records.append(rec)
                assign[u] = {"group_id": gid}
            specs.append({
                "name": name, "fields": GROUP_ENTITY_FIELDS, "primary_key": "record_uuid",
                "description": f"Group {gid} ({cl['basis']})", "records": records,
                "file_path_rel": f"data/{name.lower()}.bejson",
            })
        Common.bulk_create_entities(mpath, specs)
        Common.bulk_update_fields(mpath, GROUP_INDEX_ENTITY, assign)
        formed += len(specs)
    return formed


def restore_group(session_id: str, group_id: str, member_uuids: List[str], basis: str) -> bool:
    """Undo support: bring a group back (same group_id) after trashed files were restored.
    Members that vanished from disk or joined another group in the meantime are left out;
    needs at least 2 members. If the group still exists, missing members are appended."""
    source = {r["record_uuid"]: r for r in load_source_records(session_id)}
    index = {r["record_uuid"]: r for r in group_index_rows(session_id)}
    usable = [u for u in dict.fromkeys(member_uuids)
              if u in source and os.path.exists(source[u].get("file_path") or "")
              and index.get(u, {}).get("group_id") in (None, group_id)]
    name = _find_group_entity_name(session_id, group_id)
    mpath = group_db_manifest(session_id)
    if name:
        present = {m["record_uuid"] for m in load_group_members(session_id, name)}
        add = [dict(source[u], group_id=group_id, is_duplicate=False, match_basis=basis) for u in usable if u not in present]
        Common.bulk_append_records(mpath, name, add)
        Common.bulk_update_fields(mpath, GROUP_INDEX_ENTITY, {r["record_uuid"]: {"group_id": group_id} for r in add})
        return True
    if len(usable) < 2:
        return False
    return create_groups_bulk(session_id, [{"members": usable, "dups": set(), "basis": basis or "restored group", "group_id": group_id}], source) == 1


def perform_del_group(session_id: str, group_id: str) -> bool:
    """Dissolve a group. Entity registration is removed FIRST, the index second: a crash
    in between leaves index rows pointing at a missing entity, which reconcile() clears.
    (v9 deleted the data file first, then the manifest row - a crash there left a manifest
    row pointing at a missing file, i.e. a corrupt database.)"""
    name = _find_group_entity_name(session_id, group_id)
    if not name:
        return False
    mpath = group_db_manifest(session_id)
    members = [m["record_uuid"] for m in load_group_members(session_id, name)]
    Common.remove_entity_registration(mpath, name)
    Common.bulk_update_fields(mpath, GROUP_INDEX_ENTITY, {u: {"group_id": None} for u in members})
    return True


def remove_members(session_id: str, group_id: str, record_uuids: List[str], dismiss: bool = False) -> int:
    """Remove records from a group; dissolve it if fewer than 2 remain. Returns members removed."""
    name = _find_group_entity_name(session_id, group_id)
    if not name:
        return 0
    mpath = group_db_manifest(session_id)
    removed = Common.bulk_remove_records(mpath, name, set(record_uuids))
    Common.bulk_update_fields(mpath, GROUP_INDEX_ENTITY, {u: {"group_id": None} for u in record_uuids})
    if dismiss and record_uuids:
        add_dismissed(session_id, list(record_uuids))
    if len(load_group_members(session_id, name)) < 2:
        perform_del_group(session_id, group_id)
    return removed


# ---------------------------------------------------------------------------
# Quick heuristics
# ---------------------------------------------------------------------------

_COPY_SUFFIX_RE = re.compile(r"(\s*\(\d+\)|[\s_-]+copy(\s*\d+)?|[\s_-]+\d{1,2})$", re.IGNORECASE)
_GENERIC_STEMS = {"image", "img", "photo", "picture", "screenshot", "download", "unnamed", "untitled"}


def normalized_stem(file_name: str) -> str:
    stem = Path(file_name).stem.lower().strip()
    prev = None
    while prev != stem:
        prev = stem
        stem = _COPY_SUFFIX_RE.sub("", stem).strip()
    return stem


class _UnionFind:
    def __init__(self):
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def quick_candidates(records: List[dict], max_bucket: int, use_filename: bool) -> Tuple[List[dict], Dict[str, int]]:
    """Pure function (unit-tested): records -> [{members, basis_set}], stats."""
    buckets: Dict[Tuple, List[dict]] = defaultdict(list)
    for r in records:
        w, h = r.get("width"), r.get("height")
        size = r.get("file_size_bytes")
        if size and w and h:
            buckets[("size", size, w, h)].append(r)
        stamp = r.get("exif_datetime")
        if stamp:
            cam = r.get("camera_model") or r.get("exif_make") or ""
            buckets[("exif", stamp, cam)].append(r)
        if use_filename and w and h:
            stem = normalized_stem(r.get("file_name") or "")
            if len(stem) >= 6 and stem not in _GENERIC_STEMS:
                buckets[("name", stem, w, h)].append(r)

    uf = _UnionFind()
    reasons: Dict[str, Set[str]] = defaultdict(set)
    label = {"size": B_SIZE, "exif": B_EXIF, "name": B_NAME}
    skipped_big = 0
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        if len(members) > max_bucket:
            skipped_big += 1
            continue
        first = members[0]["record_uuid"]
        for m in members[1:]:
            uf.union(first, m["record_uuid"])
        reasons[first].add(label[key[0]])
        for m in members[1:]:
            reasons[m["record_uuid"]].add(label[key[0]])

    clusters: Dict[str, dict] = {}
    for u in list(uf.parent):
        root = uf.find(u)
        c = clusters.setdefault(root, {"members": [], "basis_set": set()})
        c["members"].append(u)
        c["basis_set"] |= reasons.get(u, set())
    out, oversized = [], 0
    for c in clusters.values():
        if len(c["members"]) < 2:
            continue
        if len(c["members"]) > QUICK_CLUSTER_CAP:
            oversized += 1
            continue
        out.append(c)
    return out, {"buckets_skipped_too_big": skipped_big, "clusters_skipped_oversized": oversized}


def basis_text(basis_set: Set[str]) -> str:
    order = [B_SHA, B_SIZE, B_NAME, B_EXIF, B_PHASH]
    return " + ".join(b for b in order if b in basis_set) or "heuristic"


# ---------------------------------------------------------------------------
# Quick-Group driver
# ---------------------------------------------------------------------------

def _eligible(session_id: str, source: List[dict]) -> List[dict]:
    """Ungrouped, non-dismissed, still-on-disk records."""
    grouped = {r["record_uuid"] for r in group_index_rows(session_id) if r.get("group_id")}
    dismissed = load_dismissed(session_id)
    return [s for s in source
            if s["record_uuid"] not in grouped and s["record_uuid"] not in dismissed
            and s.get("file_path") and os.path.exists(s["file_path"])]


def _dissolve_unresolved(session_id: str) -> int:
    resolved = _load_resolved(session_id)
    n = 0
    pmap = Common.entity_path_map(group_db_manifest(session_id))
    for name in [x for x in pmap if Common.is_group_entity_name(x)]:
        members = load_group_members(session_id, name, pmap)
        if not members:
            continue
        gid = members[0].get("group_id")
        if gid and gid not in resolved and perform_del_group(session_id, gid):
            n += 1
    return n


def _load_resolved(session_id: str) -> Set[str]:
    p = session_dir(session_id) / "persist" / "resolved_groups.104a.bejson"
    doc = bejson_core_load_file(str(p)) if p.exists() else None
    if not doc:
        return set()
    fmap = bejson_core_get_field_map(doc)
    return {row[fmap["group_id"]] for row in doc.get("Values", [])}


def run_quick_group(session_id: str, rebuild: bool = False) -> int:
    cfg = ensure_config()
    max_bucket = int(get_config_value(cfg, "quick_group_max_bucket", 25) or 25)
    use_name = bool(get_config_value(cfg, "quick_group_use_filename", False))
    source = load_source_records(session_id)
    ensure_group_db(session_id)
    sync_group_index(session_id, source)
    rec = reconcile(session_id)
    if rec["index_rows_fixed"] or rec["empty_groups_removed"]:
        Log.info(f"Reconciled group index: {rec}")
    if rebuild:
        Log.info(f"Rebuild: dissolved {_dissolve_unresolved(session_id)} unresolved group(s).")
    eligible = _eligible(session_id, source)
    Log.info(f"Quick-Group: {len(eligible)} eligible image(s) of {len(source)} indexed.")
    cands, stats = quick_candidates(eligible, max_bucket, use_name)
    clusters = [{"members": c["members"], "dups": set(), "basis": basis_text(c["basis_set"])} for c in cands]
    by_uuid = {s["record_uuid"]: s for s in eligible}
    formed = create_groups_bulk(session_id, clusters, by_uuid)
    if stats["buckets_skipped_too_big"] or stats["clusters_skipped_oversized"]:
        Log.info(f"Skipped {stats['buckets_skipped_too_big']} over-large bucket(s) and "
                 f"{stats['clusters_skipped_oversized']} oversized cluster(s) (likely mass-identical junk).")
    return formed


def cmd_quick_group(args) -> None:
    sid = require_selected_session()
    if not sid:
        return
    state = load_group_state(sid)
    if state.get("status", "").startswith("running"):
        Log.error("Grouping is already running on this session.")
        return
    state.begin_run("running_quick")
    state.set_and_save(method="quick", last_started_at=Common.utc_now_iso(), last_error=None)
    try:
        formed = run_quick_group(sid, rebuild=bool(getattr(args, "rebuild", False)))
    except Exception as exc:
        state.end_run("error", last_error=f"{type(exc).__name__}: {exc}")
        Log.error(f"Quick-Group failed: {exc}")
        return
    state.end_run("complete", last_completed_at=Common.utc_now_iso(), groups_formed_last_run=formed)
    Log.info(important(f"Quick-Group complete: {formed} group(s) formed. Open the viewer to review them."))


# ---------------------------------------------------------------------------
# Deep grouping: backfill -> exact dups -> pHash clustering
# ---------------------------------------------------------------------------

def run_backfill(session_id: str, stop_flag: Path, need_phash: bool = True) -> bool:
    """Compute missing hashes (SHA-256 only where byte size collides). Resumable: each
    batch is written to the source index + GroupIndex before the next one starts."""
    cfg = ensure_config()
    use_draft = bool(get_config_value(cfg, "phash_use_draft", True))
    source = load_source_records(session_id)
    sizes = Counter(s.get("file_size_bytes") for s in source)
    todo = []
    for s in source:
        if not s.get("file_path") or not os.path.exists(s["file_path"]):
            continue
        need_sha = sizes[s.get("file_size_bytes")] > 1 and not s.get("sha256_hash")
        need_ph = need_phash and not s.get("phash_original")
        if need_sha or need_ph:
            todo.append((s, need_sha, need_ph))
    if not todo:
        return True
    Log.info(f"Backfill: hashing {len(todo)} file(s) ...")
    src_manifest, grp_manifest = source_index_manifest(session_id), group_db_manifest(session_id)
    batch: Dict[str, Dict[str, Any]] = {}

    def flush() -> None:
        nonlocal batch
        if batch:
            Common.bulk_update_fields(src_manifest, SOURCE_ENTITY, batch)
            Common.bulk_update_fields(grp_manifest, GROUP_INDEX_ENTITY, batch)
            batch = {}

    done = 0
    for s, need_sha, need_ph in todo:
        if stop_flag.exists():
            flush()
            Log.info("Stop flag detected during backfill; progress saved.")
            return False
        upd: Dict[str, Any] = {}
        try:
            if need_sha:
                upd["sha256_hash"] = Common.sha256_of_file(s["file_path"])
            if need_ph:
                p = Common.compute_phash_set(s["file_path"], use_draft=use_draft)
                upd.update(phash_original=p[0], phash_rotate_90=p[1], phash_rotate_180=p[2], phash_rotate_270=p[3])
        except Exception as exc:
            Log.warning(f"Hashing failed for {s['file_path']}: {exc}")
            continue
        batch[s["record_uuid"]] = upd
        done += 1
        if len(batch) >= 25:
            flush()
            sys.stdout.write(f"\r  hashed {done}/{len(todo)}   ")
            sys.stdout.flush()
    flush()
    sys.stdout.write("\n")
    return True


def flag_exact_duplicates(session_id: str, dismissed: Set[str]) -> int:
    """Among ungrouped rows with a SHA-256: first by path is canonical, the rest are
    flagged is_duplicate/duplicate_of. Recomputed from scratch each run (idempotent)."""
    rows = [r for r in group_index_rows(session_id) if not r.get("group_id") and r["record_uuid"] not in dismissed]
    by_sha: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        if r.get("sha256_hash"):
            by_sha[r["sha256_hash"]].append(r)
    fixes: Dict[str, Dict[str, Any]] = {}
    for members in by_sha.values():
        members.sort(key=lambda r: r.get("file_path") or "")
        canon = members[0]
        for r in members:
            want_dup = r is not canon
            want_of = canon["record_uuid"] if want_dup else None
            if bool(r.get("is_duplicate")) != want_dup or r.get("duplicate_of") != want_of:
                fixes[r["record_uuid"]] = {"is_duplicate": want_dup, "duplicate_of": want_of}
    for r in rows:                                   # rows that lost their sha twin
        if r["record_uuid"] not in fixes and r.get("is_duplicate") and not r.get("sha256_hash"):
            fixes[r["record_uuid"]] = {"is_duplicate": False, "duplicate_of": None}
    Common.bulk_update_fields(group_db_manifest(session_id), GROUP_INDEX_ENTITY, fixes)
    return len(fixes)


def cluster_phash(rows: List[dict], threshold: int, stop_flag: Optional[Path] = None) -> Optional[List[List[str]]]:
    """Union-find over rows with a pHash. Sorted by orientation-invariant aspect ratio so
    the inner loop is a sliding window. Returns clusters of record_uuids (None if stopped)."""
    items = []
    for r in rows:
        h0 = r.get("phash_original")
        if not h0:
            continue
        hs = tuple(int(r[f], 16) for f in ("phash_original", "phash_rotate_90", "phash_rotate_180", "phash_rotate_270") if r.get(f))
        w, h = r.get("width") or 0, r.get("height") or 0
        ratio = (min(w, h) / max(w, h)) if (w and h) else 0.0
        items.append((ratio, r["record_uuid"], int(h0, 16), hs))
    items.sort(key=lambda t: t[0])
    uf = _UnionFind()
    n = len(items)
    for i in range(n):
        if stop_flag is not None and i % 100 == 0 and stop_flag.exists():
            return None
        ratio_i, uid_i, a, _ = items[i]
        for j in range(i + 1, n):
            ratio_j, uid_j, _, hs_j = items[j]
            if ratio_j - ratio_i > PHASH_RATIO_TOLERANCE:
                break
            if uf.find(uid_i) == uf.find(uid_j):
                continue
            for hj in hs_j:
                if (a ^ hj).bit_count() <= threshold:
                    uf.union(uid_i, uid_j)
                    break
        if n >= 2000 and i % max(1, n // 20) == 0:
            Log.info(f"  clustering {100 * i // n}% ...")
    groups: Dict[str, List[str]] = defaultdict(list)
    for _, uid, _, _ in items:
        groups[uf.find(uid)].append(uid)
    return [g for g in groups.values() if len(g) > 1]


def run_deep_group(session_id: str, stop_flag: Path) -> bool:
    cfg = ensure_config()
    threshold = int(get_config_value(cfg, "phash_hamming_threshold", 10) or 10)
    if not run_backfill(session_id, stop_flag):
        return False
    source = load_source_records(session_id)
    by_uuid = {s["record_uuid"]: s for s in source}
    sync_group_index(session_id, source)
    reconcile(session_id)
    dismissed = load_dismissed(session_id)
    n_flagged = flag_exact_duplicates(session_id, dismissed)
    Log.info(f"Exact-duplicate pass: {n_flagged} row(s) updated.")
    rows = [r for r in group_index_rows(session_id) if not r.get("group_id") and r["record_uuid"] not in dismissed]
    canon = [r for r in rows if not r.get("is_duplicate")]
    dups_of: Dict[str, List[str]] = defaultdict(list)
    for r in rows:
        if r.get("is_duplicate") and r.get("duplicate_of"):
            dups_of[r["duplicate_of"]].append(r["record_uuid"])

    ph_clusters = cluster_phash(canon, threshold, stop_flag)
    if ph_clusters is None:
        Log.info("Stop flag detected during clustering; nothing partially written.")
        return False
    in_cluster = {u for c in ph_clusters for u in c}
    clusters_raw = [c for c in ph_clusters]
    clusters_raw += [[r["record_uuid"]] for r in canon if r["record_uuid"] not in in_cluster and dups_of.get(r["record_uuid"])]

    clusters = []
    for members in clusters_raw:
        dup_ids = [d for m in members for d in dups_of.get(m, [])]
        basis: Set[str] = set()
        if len(members) > 1:
            basis.add(B_PHASH)
        if dup_ids:
            basis.add(B_SHA)
        clusters.append({"members": members + dup_ids, "dups": set(dup_ids), "basis": basis_text(basis)})
    formed = create_groups_bulk(session_id, clusters, by_uuid)
    Log.info(important(f"Deep grouping: {formed} group(s) formed."))
    return True


def cmd_start_grouping(args) -> None:
    sid = require_selected_session()
    if not sid:
        return
    state = load_group_state(sid)
    if state.get("status", "").startswith("running"):
        Log.error("Grouping is already running on this session.")
        return
    ensure_group_db(sid)
    flag = group_stop_flag_path(sid)
    flag.unlink(missing_ok=True)
    state.begin_run("running_phase2")
    state.set_and_save(method="deep", last_started_at=Common.utc_now_iso(), last_error=None)
    try:
        finished = run_deep_group(sid, flag)
    except KeyboardInterrupt:
        finished = False
    except Exception as exc:
        state.end_run("error", last_error=f"{type(exc).__name__}: {exc}")
        Log.error(f"Grouping failed: {exc}")
        return
    finally:
        flag.unlink(missing_ok=True)
    if not finished:
        state.end_run("stopped")
        return
    state.end_run("complete", last_completed_at=Common.utc_now_iso())
    stats = mfdb_core_get_stats(group_db_manifest(sid))
    Log.info(important(f"Grouping complete. {len(group_entity_names(sid))} group(s) in this session ({len(stats['entities'])} entities)."))


def cmd_stop_grouping(args) -> None:
    sid = require_selected_session()
    if not sid:
        return
    (session_dir(sid) / "persist").mkdir(parents=True, exist_ok=True)
    group_stop_flag_path(sid).write_text(Common.utc_now_iso())
    Log.info(f"Stop flag set for session {sid}.")


# ---------------------------------------------------------------------------
# Verify (hash only one group's members, on demand)
# ---------------------------------------------------------------------------

def verify_group(session_id: str, group_id: str) -> Optional[dict]:
    """Hash this group's members (SHA-256 + pHash if missing), persist the hashes, and
    classify each member. Returns None if the group does not exist."""
    name = _find_group_entity_name(session_id, group_id)
    if not name:
        return None
    use_draft = bool(get_config_value(ensure_config(), "phash_use_draft", True))
    members = load_group_members(session_id, name)
    updates: Dict[str, Dict[str, Any]] = {}
    for m in members:
        path = m.get("file_path")
        if not path or not os.path.exists(path):
            continue
        upd: Dict[str, Any] = {}
        try:
            if not m.get("sha256_hash"):
                upd["sha256_hash"] = Common.sha256_of_file(path)
            if not m.get("phash_original") and Common.PIL_AVAILABLE:
                p = Common.compute_phash_set(path, use_draft=use_draft)
                upd.update(phash_original=p[0], phash_rotate_90=p[1], phash_rotate_180=p[2], phash_rotate_270=p[3])
        except Exception as exc:
            Log.warning(f"Verify: hashing failed for {path}: {exc}")
        if upd:
            updates[m["record_uuid"]] = upd
            m.update(upd)
    if updates:
        Common.bulk_update_fields(group_db_manifest(session_id), name, updates)
        Common.bulk_update_fields(source_index_manifest(session_id), SOURCE_ENTITY, updates)
        Common.bulk_update_fields(group_db_manifest(session_id), GROUP_INDEX_ENTITY, updates)

    by_sha: Dict[str, List[str]] = defaultdict(list)
    for m in members:
        if m.get("sha256_hash"):
            by_sha[m["sha256_hash"]].append(m["record_uuid"])
    threshold = int(get_config_value(ensure_config(), "phash_hamming_threshold", 10) or 10)
    out_members, worst = [], 0
    biggest_sha = max(by_sha.values(), key=len)[0] if by_sha else None
    anchor = next((m for m in members if m["record_uuid"] == biggest_sha), members[0] if members else None)
    for m in members:
        twins = [u for u in by_sha.get(m.get("sha256_hash") or "", []) if u != m["record_uuid"]]
        dist = Common.phash_min_distance(anchor, m) if anchor is not None and m is not anchor else 0
        if dist is not None and dist > worst:
            worst = dist
        out_members.append({
            "record_uuid": m["record_uuid"], "identical_to": twins,
            "phash_distance_to_anchor": dist,
            "similar": (dist is not None and dist <= threshold),
        })
    distinct = len(by_sha)
    if members and distinct == 1 and len(by_sha[next(iter(by_sha))]) == len(members):
        verdict = "all_identical"
    elif out_members and all(o["similar"] for o in out_members):
        verdict = "all_similar"
    elif any(o["similar"] and o["record_uuid"] != anchor["record_uuid"] for o in out_members):
        verdict = "mixed"
    else:
        verdict = "unrelated"
    return {"group_id": group_id, "verdict": verdict, "members": out_members, "threshold": threshold,
            "hashed_now": len(updates)}


def cmd_verify_group(args) -> None:
    sid = require_selected_session()
    if not sid:
        return
    res = verify_group(sid, args.group_id)
    if res is None:
        Log.error(f"Group not found: {args.group_id}")
        return
    Log.info(important(f"Group {args.group_id}: {res['verdict']} (hashed {res['hashed_now']} file(s) now)"))


# ---------------------------------------------------------------------------
# Prune (drop vanished files; dissolve undersized groups)
# ---------------------------------------------------------------------------

def prune_missing(session_id: str) -> Dict[str, int]:
    removed = dissolved = 0
    pmap = Common.entity_path_map(group_db_manifest(session_id))
    for name in [x for x in pmap if Common.is_group_entity_name(x)]:
        members = load_group_members(session_id, name, pmap)
        if not members:
            continue
        gid = members[0].get("group_id")
        gone = [m["record_uuid"] for m in members if not (m.get("file_path") and os.path.exists(m["file_path"]))]
        if not gone:
            continue
        removed += len(gone)
        remaining = len(members) - len(gone)
        remove_members(session_id, gid, gone, dismiss=False)
        if remaining < 2:
            dissolved += 1
    rec = reconcile(session_id)
    return {"members_removed": removed, "groups_dissolved": dissolved, **rec}


def cmd_prune(args) -> None:
    sid = require_selected_session()
    if not sid:
        return
    Log.info(important(f"Prune complete: {prune_missing(sid)}"))


# ---------------------------------------------------------------------------
# View / Delete group (CLI)
# ---------------------------------------------------------------------------

def cmd_view_group(args) -> None:
    sid = require_selected_session()
    if not sid:
        return
    gid = getattr(args, "group_id", None)
    name = _find_group_entity_name(sid, gid) if gid else None
    if not name:
        Log.error(f"Group not found: {gid}")
        return
    holder = image_holder_path(sid)
    if holder.exists():
        shutil.rmtree(holder)
    holder.mkdir(parents=True, exist_ok=True)
    members = load_group_members(sid, name)
    copied = 0
    for m in members:
        src = Path(m["file_path"])
        if not src.exists():
            Log.warning(f"Missing on disk, skipped: {src}")
            continue
        shutil.copy2(src, holder / f"{m['record_uuid']}_{src.name}")
        copied += 1
    Log.info(f"Group {gid}: copied {copied}/{len(members)} image(s) into {holder}")


def cmd_del_group(args) -> None:
    sid = require_selected_session()
    if not sid:
        return
    if not getattr(args, "group_id", None) or not perform_del_group(sid, args.group_id):
        Log.error(f"Group not found: {getattr(args, 'group_id', None)}")
        return
    Log.info(f"Deleted group {args.group_id}. Member rows returned to ungrouped state.")


def cmd_list_sessions(args) -> None:
    selected = get_selected_session_id()
    ids = Scanner.list_session_ids()
    if not ids:
        Log.info("No sessions found. Sessions are created via Cli_Image_Scanner.py.")
        return
    for sid in ids:
        has_index = Path(source_index_manifest(sid)).exists()
        n = len(group_entity_names(sid)) if Path(group_db_manifest(sid)).exists() else 0
        Log.info(f"{sid}  indexed={has_index}  groups={n}{' <- selected' if sid == selected else ''}")


def cmd_sel_session(args) -> None:
    Scanner.cmd_sel_session(args)


def cmd_reset_dismissed(args) -> None:
    sid = require_selected_session()
    if sid:
        clear_dismissed(sid)
        Log.info("Cleared 'not a duplicate' decisions for this session.")


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=SCRIPT_NAME, description="Quick (heuristic) and deep (hash) grouping over a scanner session.")
    p.add_argument("--List-Session", action="store_true")
    p.add_argument("--Sel-Session", dest="session_id", metavar="SESSION_UUID")
    p.add_argument("--Quick-Group", action="store_true", help="Fast heuristic grouping (size/EXIF/dimensions). No hashing.")
    p.add_argument("--Rebuild", dest="rebuild", action="store_true", help="With --Quick-Group: dissolve unresolved groups first.")
    p.add_argument("--Start-Grouping", action="store_true", help="Deep grouping: backfill hashes, exact dups, pHash clusters.")
    p.add_argument("--Stop-Grouping", action="store_true")
    p.add_argument("--Verify-Group", dest="group_id_verify", metavar="GROUP_ID")
    p.add_argument("--View-Group", dest="group_id_view", metavar="GROUP_ID")
    p.add_argument("--Del-Group", dest="group_id_del", metavar="GROUP_ID")
    p.add_argument("--Prune", action="store_true", help="Drop members whose files vanished; dissolve undersized groups.")
    p.add_argument("--Reset-Dismissed", action="store_true")
    Common.add_standard_flags(p)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    rc = Common.handle_standard_flags(args, SCRIPT_NAME, VERSION, SCRIPT_PATH, CONFIG_PATH, Scanner.reset_config_to_defaults)
    if rc is not None:
        return rc
    Scanner.apply_log_level(ensure_config())

    if args.List_Session:
        cmd_list_sessions(args)
    elif args.session_id:
        cmd_sel_session(args)
    elif args.group_id_view:
        args.group_id = args.group_id_view
        cmd_view_group(args)
    elif args.group_id_del:
        args.group_id = args.group_id_del
        cmd_del_group(args)
    elif args.group_id_verify:
        args.group_id = args.group_id_verify
        cmd_verify_group(args)
    elif args.Stop_Grouping:
        cmd_stop_grouping(args)
    elif args.Quick_Group:
        cmd_quick_group(args)
    elif args.Start_Grouping:
        cmd_start_grouping(args)
    elif args.Prune:
        cmd_prune(args)
    elif args.Reset_Dismissed:
        cmd_reset_dismissed(args)
    else:
        parser.print_help()
    return Common.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

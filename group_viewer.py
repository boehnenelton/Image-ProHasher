#!/usr/bin/env python3
"""
Name/Library:   group_viewer.py
Family:         Image-ProHasher (Tool 3 of 3)
Description:    Flask review UI for the groups/ MFDB built by CLI_Image_Grouper.py.
                 v105: card list, tap-to-select tiles with KEEP/TRASH ribbons, bottom selection bar,
                 prev/next group + shortcuts, bulk 'resolve exact copies' (byte-identical only),
                 review-before-commit dialog with every-copy-trashed guard, Undo that restores groups.
                 Group list (filter by state / match basis, sorted by reclaimable space),
                 tap-to-select tile grid, side-by-side compare, one-click keep strategies,
                 on-demand Verify (hashes only the files in one group), a staged-action
                 review cart (trash / move / copy executed only on Commit), soft-delete
                 trash with Restore + explicit Empty, a folder browser for move/copy
                 destinations, and a debug-notes panel synced to dev/persistent-notes.md.
                 Field Map Cache access only - no positional Values[] lookups.
Version:        107
Library_Version:        103.31 (MFDB)
Date Created:   2026-08-28
Date Modified:  2026-10-24
Author:         Elton Boehnen
Contact:        boehnenelton2024@gmail.com | boehnenelton2024.pages.dev | github.com/boehnenelton
Format_Creator: Elton Boehnen
RELATIONAL_ID:  4d4572fb-fa03-4c97-b97a-2f969a1c6f4e

SECURITY MODEL (personal single-user tool, hardened proportionally):
  - Shared-secret token, compared in constant time. Entered once on /login, then carried
    in an HttpOnly + SameSite=Strict cookie. The token is never embedded in page HTML,
    never stored in localStorage and never put in image URLs (v9 did all three).
    ?token= / X-API-Token remain accepted for scripts.
  - Every session / group / record id is validated as a UUID before it touches a path
    (v9 joined raw request values into filesystem paths: session='../..' escaped).
  - Host-header allow-list (DNS-rebinding defence; off only with --lan), Origin check on
    state-changing requests, JSON-only POST bodies, login rate-limit, CSP + nosniff.
  - Served files must be images inside the allowed roots; move/copy never overwrite.
"""

from __future__ import annotations

import argparse
import hmac
import io
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid as _uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

SCRIPT_NAME = "group_viewer.py"
SCRIPT_PATH = Path(__file__).resolve().parent
VERSION = 108
SCRIPT_VERSION = VERSION
RELATIONAL_ID = "95ef0689-1643-4c34-a440-f2f8829d4ff0"
DATE_MODIFIED = "2026-11-13"
DEFAULT_PORT = 8420

sys.path.insert(0, str(SCRIPT_PATH))

from flask import Flask, abort, jsonify, make_response, redirect, render_template, request, send_file  # noqa: E402

import CLI_Image_Grouper as Grouper   # noqa: E402  (also bootstraps lib/Core + lib/ProHasher)
import Cli_Image_Scanner as Scanner   # noqa: E402
import lib_prohasher_common as Common  # noqa: E402
from lib_bejson_Core_bejson_core import (  # noqa: E402
    bejson_core_atomic_write, bejson_core_create_104a, bejson_core_get_field_map, bejson_core_load_file,
)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s group_viewer: %(message)s", datefmt="%H:%M:%S")
Log = logging.getLogger("group_viewer")

if Common.PIL_AVAILABLE:
    from PIL import Image, ImageOps

app = Flask(__name__, static_folder=str(SCRIPT_PATH / "gv_static"), template_folder=str(SCRIPT_PATH / "gv_templates"))
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024
CACHE = Common.EntityCache(max_entries=512)
_SUMMARY_CACHE: Dict[str, tuple] = {}          # sid -> (signature, built_at, items)
_GENERATION = 0
SUMMARY_MAX_AGE_SECONDS = 10                   # bounds staleness if another process (the CLI) changes groups under us


def invalidate_caches() -> None:
    """Every mutating endpoint calls this: drops parsed entities AND the cached group list."""
    global _GENERATION
    _GENERATION += 1
    CACHE.invalidate()
    _SUMMARY_CACHE.clear()
MUTATE_LOCK = threading.RLock()
THUMB_SIZE = (240, 240)
THUMB_CACHE_DIRNAME = "thumb_cache"
NOTES_PATH = SCRIPT_PATH / "dev" / "persistent-notes.md"
NOTES_MAX_BYTES = 200_000
COOKIE_NAME = "gv_token"

# ---------------------------------------------------------------------------
# Config / security bootstrap
# ---------------------------------------------------------------------------

def ensure_viewer_security_config(config_doc: dict) -> dict:
    fmap = bejson_core_get_field_map(config_doc)
    existing = {row[fmap["setting_name"]] for row in config_doc.get("Values", [])}
    changed = False
    for name, value, desc in (
        ("viewer_user", "admin", "Username for group_viewer web UI (like BEJSON CMS)."),
        ("viewer_password", "changeme", "Password for group_viewer web UI (like BEJSON CMS)."),
        ("viewer_api_token", secrets.token_urlsafe(24), "Shared-secret token for group_viewer (cookie after /login). Rotate by deleting this row."),
        ("viewer_allowed_roots", Scanner.get_config_value(config_doc, "drive_scan_root", "/storage/emulated/0"),
         "Comma-separated roots group_viewer may browse/move/copy/serve within."),
    ):
        if name not in existing:
            row = [None] * len(config_doc["Fields"])
            row[fmap["setting_name"]], row[fmap["setting_value"]] = name, value
            if "description" in fmap:
                row[fmap["description"]] = desc
            config_doc["Values"].append(row)
            changed = True
    if changed:
        bejson_core_atomic_write(str(Scanner.CONFIG_PATH), config_doc)
        try:
            os.chmod(Scanner.CONFIG_PATH, 0o600)
        except OSError:
            pass
    return config_doc


_config_doc = ensure_viewer_security_config(Scanner.ensure_config())
VIEWER_API_TOKEN: str = Scanner.get_config_value(_config_doc, "viewer_api_token") or ""
VIEWER_USER: str = str(os.environ.get("VIEWER_USER") or os.environ.get("CMS_USER") or Scanner.get_config_value(_config_doc, "viewer_user", "admin") or "admin")
VIEWER_PASSWORD: str = str(os.environ.get("VIEWER_PASSWORD") or os.environ.get("CMS_PASSWORD") or Scanner.get_config_value(_config_doc, "viewer_password", "changeme") or "changeme")
_GLOBAL_ALLOWED_ROOTS = [Path(p.strip()).resolve()
                         for p in str(Scanner.get_config_value(_config_doc, "viewer_allowed_roots", "/storage/emulated/0") or "").split(",")
                         if p.strip()]
ALLOWED_HOSTS: set = set()      # filled in main(); empty = unrestricted (--lan)
_login_failures: Dict[str, List[float]] = defaultdict(list)


def session_allowed_roots(session_id: Optional[str] = None) -> List[Path]:
    roots = list(_GLOBAL_ALLOWED_ROOTS)
    if session_id:
        try:
            target = Scanner.load_scan_state(session_id).get("target_path")
            if target:
                roots.append(Path(target).resolve())
        except Exception:
            pass
    return roots


def path_is_allowed(candidate, session_id: Optional[str] = None) -> bool:
    return Common.path_within(candidate, session_allowed_roots(session_id))


SESSION_TTL_SECONDS = 12 * 3600
_SESSIONS: Dict[str, float] = {}               # random cookie value -> expiry. In memory: restarting the viewer signs everyone out.
_SESSIONS_LOCK = threading.Lock()


def _user_pass_ok(user: Optional[str], pwd: Optional[str]) -> bool:
    if not user or not pwd:
        return False
    user_clean = str(user).strip()
    pwd_clean = str(pwd).strip()
    user_ok = hmac.compare_digest(user_clean, VIEWER_USER)
    pass_ok = hmac.compare_digest(pwd_clean, VIEWER_PASSWORD) or (bool(VIEWER_API_TOKEN) and hmac.compare_digest(pwd_clean, VIEWER_API_TOKEN))
    return user_ok and pass_ok


def _token_ok(provided: Optional[str]) -> bool:
    if not provided:
        return False
    p = str(provided).strip()
    return (bool(VIEWER_API_TOKEN) and hmac.compare_digest(p, VIEWER_API_TOKEN)) or (bool(VIEWER_PASSWORD) and hmac.compare_digest(p, VIEWER_PASSWORD))


def _basic_auth_ok() -> bool:
    auth = request.authorization
    if not auth:
        return False
    if _user_pass_ok(auth.username, auth.password):
        return True
    if _token_ok(auth.password) or _token_ok(auth.username):
        return True
    return False


def _new_session() -> str:
    """The cookie carries a random session id, never the long-lived token (a leaked cookie expires; a leaked token would not)."""
    sid, now = secrets.token_urlsafe(32), time.time()
    with _SESSIONS_LOCK:
        for k in [k for k, exp in _SESSIONS.items() if exp < now]:
            del _SESSIONS[k]
        _SESSIONS[sid] = now + SESSION_TTL_SECONDS
    return sid


def _session_ok(value: Optional[str]) -> bool:
    if not value:
        return False
    with _SESSIONS_LOCK:
        exp = _SESSIONS.get(value)
        if exp is None or exp < time.time():
            _SESSIONS.pop(value, None)
            return False
        _SESSIONS[value] = time.time() + SESSION_TTL_SECONDS      # sliding expiry
        return True


def _authed() -> bool:
    return (_session_ok(request.cookies.get(COOKIE_NAME))
            or _basic_auth_ok()
            or _token_ok(request.headers.get("X-API-Token"))
            or _token_ok(request.args.get("token")))


def _blocked(status: int, title: str, detail: str):
    """Human-readable refusal (plain 'Bad Request' / 'Forbidden' pages gave no clue what to change)."""
    if request.path.startswith("/api/") or request.is_json:
        return jsonify({"error": f"{title}: {detail}"}), status
    page = ("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><title>%s</title>"
            "<body style='font-family:sans-serif;max-width:560px;margin:10vh auto;padding:0 16px'>"
            "<h2 style='color:#DE2626'>%s</h2><p>%s</p></body>") % (title, title, detail)
    return make_response(page, status)


@app.before_request
def _gate():
    if ALLOWED_HOSTS and request.host.lower() not in ALLOWED_HOSTS:
        good = sorted(ALLOWED_HOSTS)[0]
        return _blocked(400, "Address not allowed",
                        f"You opened the viewer as '{request.host}'. For safety it only answers on {', '.join(sorted(ALLOWED_HOSTS))}. "
                        f"Open http://{good}/ instead (or start group_viewer.py with --lan to allow other addresses).")
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("Origin")
        # A browser form POST (the sign-in page) sends 'Origin: null' because of Referrer-Policy no-referrer, so only
        # /login accepts it. Every JSON API call is a fetch() and always carries the real origin.
        null_ok = request.path == "/login" and origin == "null"
        if origin and not null_ok and urlparse(origin).netloc.lower() != request.host.lower():
            return _blocked(403, "Cross-site request blocked", f"The request came from '{origin}', not from this viewer page. Reload the viewer and try again.")
        if request.path != "/login" and not request.is_json:
            return jsonify({"error": "JSON body required"}), 415
    static_prefix = "/" + (app.static_url_path or "static").strip("/") + "/"      # this app serves assets from /gv_static/, not /static/
    if request.path.startswith(static_prefix) or request.path in ("/login", "/", "/favicon.ico"):
        return None
    if not _authed():
        return jsonify({"error": "unauthorized"}), 401


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.after_request
def _headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"          # file paths and queue contents must not sit in any cache
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                               "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    return resp


@app.errorhandler(ValueError)
def _bad_value(exc):
    return jsonify({"error": str(exc)}), 400


@app.errorhandler(413)
def _too_big(_e):
    return jsonify({"error": "payload too large"}), 413


def body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def session_label(sid: str) -> str:
    """Human label for a session: scanned folder name + profile + status (ids mean nothing to people)."""
    try:
        st = Scanner.load_scan_state(sid)
        target = st.get("target_path")
        name = Path(target).name or target if target else "(no folder yet)"
        return f"{name} \u00b7 {st.get('profile') or 'not scanned'} \u00b7 {sid[:4]}"
    except Exception:
        return sid[:8]


def session_arg(source: Optional[dict] = None) -> str:
    raw = (source or {}).get("session") if source is not None else request.args.get("session")
    sid = Common.require_uuid(raw, "session")
    if not Scanner.session_dir(sid).is_dir():
        abort(404)
    return sid


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def _set_auth_cookie(resp):
    resp.set_cookie(COOKIE_NAME, _new_session(), httponly=True, samesite="Strict", max_age=SESSION_TTL_SECONDS, path="/")
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", script_version=VERSION, error=None, default_user=VIEWER_USER)
    ip = request.remote_addr or "?"
    now = time.time()
    _login_failures[ip] = [t for t in _login_failures[ip] if now - t < 60]
    if len(_login_failures[ip]) >= 10:
        return render_template("login.html", script_version=VERSION, error="Too many attempts. Wait a minute.", default_user=VIEWER_USER), 429
    
    supplied_token = (request.form.get("token") or "").strip()
    supplied_user = (request.form.get("username") or "").strip()
    supplied_pwd = (request.form.get("password") or "").strip()

    if supplied_user and supplied_pwd and _user_pass_ok(supplied_user, supplied_pwd):
        return _set_auth_cookie(make_response(redirect("/")))
    if supplied_token and _token_ok(supplied_token):
        return _set_auth_cookie(make_response(redirect("/")))
    if supplied_pwd and not supplied_token and _token_ok(supplied_pwd):
        return _set_auth_cookie(make_response(redirect("/")))
    if supplied_user and not supplied_pwd and not supplied_token and _token_ok(supplied_user):
        return _set_auth_cookie(make_response(redirect("/")))

    _login_failures[ip].append(now)
    return render_template("login.html", script_version=VERSION, error="Wrong token or invalid credentials.", default_user=VIEWER_USER), 403


@app.route("/api/logout", methods=["POST"])
def api_logout():
    value = request.cookies.get(COOKIE_NAME)
    with _SESSIONS_LOCK:
        _SESSIONS.pop(value, None)
    resp = jsonify({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.route("/")
def index():
    if _token_ok(request.args.get("token")):                      # one-shot login link -> cookie, token stripped from URL
        return _set_auth_cookie(make_response(redirect("/")))
    if not _authed():
        return redirect("/login")
    ids = Scanner.list_session_ids()
    selected = request.args.get("session") or Scanner.get_selected_session_id()
    if selected not in ids:
        selected = ids[0] if ids else None
    sessions = [{"id": i, "label": session_label(i)} for i in ids]
    resp = make_response(render_template(
        "index.html", sessions=sessions, selected_session=selected or "", script_version=VERSION,
        date_modified=DATE_MODIFIED, author_credit=Common.AUTHOR_CREDIT))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# Paths + cached data access
# ---------------------------------------------------------------------------

def staged_actions_path(sid: str) -> Path:
    return Grouper.session_dir(sid) / "persist" / "staged_actions.104a.bejson"


def trash_dir(sid: str) -> Path:
    return Grouper.session_dir(sid) / "trash"


def trash_log_path(sid: str) -> Path:
    return trash_dir(sid) / "trash_log.104a.bejson"


def resolved_groups_path(sid: str) -> Path:
    return Grouper.session_dir(sid) / "persist" / "resolved_groups.104a.bejson"


def thumb_cache_dir(sid: str) -> Path:
    return Grouper.session_dir(sid) / THUMB_CACHE_DIRNAME


def _src_path(sid: str) -> str:
    return str(Grouper.session_dir(sid) / "index" / "data" / "image_record.bejson")


def _grp_path(sid: str, entity_name: str) -> str:
    return str(Grouper.group_db_root(sid) / "data" / f"{entity_name.lower()}.bejson")


def source_by_uuid(sid: str) -> Dict[str, dict]:
    return CACHE.load(_src_path(sid))[1]


def group_rows(sid: str, entity_name: str) -> List[dict]:
    return CACHE.load(_grp_path(sid, entity_name))[0]


def group_entity_names(sid: str) -> List[str]:
    return Grouper.group_entity_names(sid)


def find_record_in_any_group(sid: str, record_uuid: str):
    for name in group_entity_names(sid):
        hit = CACHE.load(_grp_path(sid, name))[1].get(record_uuid)
        if hit:
            return name, hit
    return None, None


STAGE_FIELDS = [
    {"name": "stage_id", "type": "string"}, {"name": "action", "type": "string"},
    {"name": "record_uuid", "type": "string"}, {"name": "group_id", "type": "string"},
    {"name": "file_path", "type": "string"}, {"name": "dest_dir", "type": "string"},
    {"name": "staged_at", "type": "string"},
]
TRASH_FIELDS = [
    {"name": "record_uuid", "type": "string"}, {"name": "group_id", "type": "string"},
    {"name": "original_path", "type": "string"}, {"name": "trash_path", "type": "string"},
    {"name": "trashed_at", "type": "string"},
    {"name": "group_basis", "type": "string"}, {"name": "group_siblings", "type": "string"},
]


def _read_rows(path: Path) -> List[dict]:
    doc = bejson_core_load_file(str(path)) if path.exists() else None
    if not doc:
        return []
    names = [f["name"] for f in doc.get("Fields", [])]
    return [dict(zip(names, r)) for r in doc.get("Values", [])]


def _write_rows(path: Path, record_type: str, fields: List[dict], rows: List[dict]) -> None:
    order = [f["name"] for f in fields]
    doc = bejson_core_create_104a(record_type, fields, [[r.get(f) for f in order] for r in rows])
    path.parent.mkdir(parents=True, exist_ok=True)
    if not bejson_core_atomic_write(str(path), doc):
        raise OSError(f"write failed: {path}")


def load_staged(sid: str) -> List[dict]:
    return _read_rows(staged_actions_path(sid))


def save_staged(sid: str, rows: List[dict]) -> None:
    _write_rows(staged_actions_path(sid), "StagedAction", STAGE_FIELDS, rows)


def load_resolved_set(sid: str) -> set:
    return {r["group_id"] for r in _read_rows(resolved_groups_path(sid))}


def save_resolved_set(sid: str, resolved: set) -> None:
    _write_rows(resolved_groups_path(sid), "ResolvedGroup", [{"name": "group_id", "type": "string"}],
                [{"group_id": g} for g in sorted(resolved)])


# ---------------------------------------------------------------------------
# Group data assembly
# ---------------------------------------------------------------------------

def _rep(members: List[dict]) -> dict:
    return max(members, key=lambda m: (_pixels(m), m.get("file_size_bytes") or 0))


SHORT_BASIS = {Grouper.B_SIZE: "Same size", Grouper.B_EXIF: "Same photo time", Grouper.B_NAME: "Same name",
               Grouper.B_SHA: "Identical", Grouper.B_PHASH: "Looks alike"}


def short_basis(basis: str) -> str:
    """Known labels are matched as substrings: they contain ' + ' themselves, so splitting on it breaks them."""
    found = [short for full, short in SHORT_BASIS.items() if full in (basis or "")]
    return " + ".join(found) or "Legacy group"


def _pixels(m: dict) -> int:
    return (m.get("width") or 0) * (m.get("height") or 0)


def _summary_signature(sid: str) -> tuple:
    def sig(p: Path):
        try:
            st = p.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return (0, 0)
    return (_GENERATION, sig(Path(Grouper.group_db_manifest(sid))), sig(resolved_groups_path(sid)))


def group_summaries(sid: str) -> List[dict]:
    """Cached per session: building it touches every group file, which made each list request ~0.8 s at
    3,600 groups. Invalidated by any viewer mutation, by manifest/resolved-file changes, and after 10 s."""
    sig = _summary_signature(sid)
    hit = _SUMMARY_CACHE.get(sid)
    if hit and hit[0] == sig and time.time() - hit[1] < SUMMARY_MAX_AGE_SECONDS:
        return [dict(g) for g in hit[2]]
    items = _build_group_summaries(sid)
    _SUMMARY_CACHE[sid] = (sig, time.time(), items)
    return [dict(g) for g in items]


def _build_group_summaries(sid: str) -> List[dict]:
    resolved = load_resolved_set(sid)
    out = []
    for name in group_entity_names(sid):
        members = group_rows(sid, name)
        if not members:
            continue
        gid = members[0]["group_id"]
        sizes = [m.get("file_size_bytes") or 0 for m in members]
        keys = {m.get("sha256_hash") or m["record_uuid"] for m in members}
        ranked = sorted(members, key=lambda m: (_pixels(m), m.get("file_size_bytes") or 0), reverse=True)
        folders = list(dict.fromkeys(Path(m.get("file_path") or "").parent.name for m in ranked))[:4]
        basis = members[0].get("match_basis") or "legacy (v9) group"
        out.append({
            "group_id": gid, "entity_name": name, "member_count": len(members),
            "distinct_image_count": len(keys), "total_size_bytes": sum(sizes),
            "reclaimable_bytes": max(sum(sizes) - max(sizes), 0), "resolved": gid in resolved,
            "representative_uuid": ranked[0]["record_uuid"], "thumb_uuids": [m["record_uuid"] for m in ranked[:3]],
            "match_basis": basis, "reason": short_basis(basis), "folders": folders,
            "sample_name": ranked[0].get("file_name") or "",
        })
    return out


def group_members_collapsed(sid: str, group_id: str, expand: bool = False) -> Optional[dict]:
    name = Grouper._find_group_entity_name(sid, group_id)
    if not name:
        return None
    members = group_rows(sid, name)
    clusters: Dict[str, List[dict]] = {}
    sha_counts: Dict[str, int] = defaultdict(int)
    for m in members:
        if m.get("sha256_hash"):
            sha_counts[m["sha256_hash"]] += 1
        # expand=True: every file is its own tile so you can pick WHICH identical copy to keep
        clusters.setdefault(m["record_uuid"] if expand else (m.get("sha256_hash") or m["record_uuid"]), []).append(m)   # unhashed files never collapse together
    tiles = []
    for key, grp in clusters.items():
        grp = sorted(grp, key=lambda m: m.get("file_size_bytes") or 0, reverse=True)
        rep = dict(grp[0])
        rep["missing"] = not os.path.exists(rep.get("file_path") or "")
        twins = (sha_counts.get(rep.get("sha256_hash") or "", 1) - 1) if expand else len(grp) - 1
        tiles.append({"key": key, "representative": rep, "copies": [c["record_uuid"] for c in grp],
                      "copy_count": len(grp), "is_exact_dup_cluster": len(grp) > 1, "twins": twins})
    tiles.sort(key=lambda t: t["representative"].get("file_size_bytes") or 0, reverse=True)
    return {"entity_name": name, "tiles": tiles, "member_count": len(members),
            "match_basis": (members[0].get("match_basis") if members else None) or "legacy (v9) group",
            "resolved": group_id in load_resolved_set(sid)}


# ---------------------------------------------------------------------------
# Routes: read API
# ---------------------------------------------------------------------------

@app.route("/api/sessions")
def api_sessions():
    out = []
    for sid in Scanner.list_session_ids():
        out.append({"session_id": sid, "label": session_label(sid), "has_groups": Path(Grouper.group_db_manifest(sid)).exists(),
                    "status": Scanner.load_scan_state(sid).get("status")})
    return jsonify(out)


@app.route("/api/groups")
def api_groups():
    sid = session_arg()
    items = group_summaries(sid)
    q = (request.args.get("q") or "").strip().lower()
    state = request.args.get("state", "open")
    basis = request.args.get("basis") or ""
    sort = request.args.get("sort", "reclaimable")
    basis_counts: Dict[str, int] = defaultdict(int)
    for g in items:
        basis_counts[g["match_basis"]] += 1
    if q:
        items = [g for g in items if q in g["group_id"].lower() or q in g["sample_name"].lower()]
    if state == "open":
        items = [g for g in items if not g["resolved"]]
    elif state == "resolved":
        items = [g for g in items if g["resolved"]]
    if basis:
        items = [g for g in items if g["match_basis"] == basis]
    keyfn = {"reclaimable": lambda g: -g["reclaimable_bytes"], "members": lambda g: -g["member_count"],
             "size": lambda g: -g["total_size_bytes"]}.get(sort, lambda g: -g["reclaimable_bytes"])
    items.sort(key=keyfn)
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        raise ValueError("limit/offset must be integers")
    return jsonify({"total": len(items), "items": items[offset:offset + limit], "basis_counts": basis_counts,
                    "reclaimable_total": sum(g["reclaimable_bytes"] for g in items)})


@app.route("/api/group/<group_id>/members")
def api_group_members(group_id):
    sid = session_arg()
    data = group_members_collapsed(sid, Common.require_uuid(group_id, "group id"), expand=request.args.get("expand") == "1")
    if data is None:
        return jsonify({"error": "group not found"}), 404
    return jsonify(data)


# ---------------------------------------------------------------------------
# Routes: images
# ---------------------------------------------------------------------------

def _record_for_serving(sid: str, record_uuid: str) -> dict:
    rec = source_by_uuid(sid).get(record_uuid)
    if not rec:
        _, rec = find_record_in_any_group(sid, record_uuid)   # source row may lag a rescan; group row is a copy
    if not rec or not rec.get("file_path"):
        abort(404)
    path = Path(rec["file_path"])
    if path.suffix.lower() not in Scanner.IMAGE_EXTENSIONS:
        abort(415)
    if not path.is_file():
        abort(404)
    if not path_is_allowed(path, sid):
        Log.warning(f"Blocked out-of-roots file serve: {path}")
        abort(403)
    return rec


def _browser_safe_rendition(sid: str, ruid: str, rec: dict) -> str:
    """Browsers cannot display HEIC/HEIF: serve a cached JPEG (max 2400 px, EXIF-rotated) instead of the original bytes."""
    if Path(rec["file_path"]).suffix.lower() not in Common.HEIF_EXTENSIONS:
        return rec["file_path"]
    if not Common.HEIF_SUPPORT:
        abort(415)
    out = thumb_cache_dir(sid) / f"full_{ruid}.jpg"
    if not (out.exists() and out.stat().st_mtime >= os.stat(rec["file_path"]).st_mtime):
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f"full_{ruid}.{secrets.token_hex(4)}.tmp")
        try:
            with Image.open(rec["file_path"]) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")
                img.thumbnail((2400, 2400))
                img.save(tmp, "JPEG", quality=88)
            os.replace(tmp, out)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            Log.warning(f"HEIC conversion failed for {rec['file_path']}: {exc}")
            abort(500)
    return str(out)


@app.route("/image/<session_id>/<record_uuid>")
def full_image(session_id, record_uuid):
    sid = Common.require_uuid(session_id, "session")
    ruid = Common.require_uuid(record_uuid, "record id")
    rec = _record_for_serving(sid, ruid)
    resp = send_file(_browser_safe_rendition(sid, ruid, rec), conditional=True, max_age=3600)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.route("/thumb/<session_id>/<record_uuid>")
def thumb_image(session_id, record_uuid):
    if not Common.PIL_AVAILABLE:
        abort(500)
    sid = Common.require_uuid(session_id, "session")
    ruid = Common.require_uuid(record_uuid, "record id")
    cache_path = thumb_cache_dir(sid) / f"{ruid}.jpg"
    rec = _record_for_serving(sid, ruid)
    src_mtime = os.stat(rec["file_path"]).st_mtime
    if not (cache_path.exists() and cache_path.stat().st_mtime >= src_mtime):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_name(f"{ruid}.{secrets.token_hex(4)}.tmp")
        try:
            with Image.open(rec["file_path"]) as img:
                try:
                    img.draft("RGB", (THUMB_SIZE[0] * 2, THUMB_SIZE[1] * 2))
                except Exception:
                    pass
                img = ImageOps.exif_transpose(img).convert("RGB")
                img.thumbnail(THUMB_SIZE)
                img.save(tmp, "JPEG", quality=80)
            os.replace(tmp, cache_path)                 # atomic: concurrent requests never see a partial file
        except Exception as exc:
            Log.warning(f"Thumbnail failed for {rec['file_path']}: {exc}")
            tmp.unlink(missing_ok=True)
            abort(500)
    resp = send_file(cache_path, conditional=True, max_age=86400)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


# ---------------------------------------------------------------------------
# Routes: staging cart
# ---------------------------------------------------------------------------

def _stage_rows(sid: str, action: str, record_uuids: List[str], dest_dir: Optional[str]) -> Dict[str, Any]:
    if action not in ("trash", "move", "copy"):
        raise ValueError("action must be trash|move|copy")
    if action in ("move", "copy"):
        if not dest_dir:
            raise ValueError("dest_dir required for move/copy")
        if not path_is_allowed(dest_dir, sid):
            return {"error": "destination is outside the allowed roots for this session", "_status": 403}
    rows = load_staged(sid)
    staged, skipped = 0, []
    with MUTATE_LOCK:
        by_record = {r["record_uuid"]: r for r in rows}
        for ruid in record_uuids:
            ruid = Common.require_uuid(ruid, "record id")
            _, member = find_record_in_any_group(sid, ruid)
            if not member:
                skipped.append({"record_uuid": ruid, "reason": "not in any group"})
                continue
            if not path_is_allowed(member["file_path"], sid):
                skipped.append({"record_uuid": ruid, "reason": "outside allowed roots"})
                continue
            by_record[ruid] = {                         # one pending action per record; latest wins
                "stage_id": str(_uuid.uuid4()), "action": action, "record_uuid": ruid,
                "group_id": member["group_id"], "file_path": member["file_path"],
                "dest_dir": dest_dir if action != "trash" else None, "staged_at": Common.utc_now_iso(),
            }
            staged += 1
        save_staged(sid, list(by_record.values()))
    return {"ok": True, "staged": staged, "skipped": skipped}


@app.route("/api/stage", methods=["POST"])
def api_stage():
    b = body()
    sid = session_arg(b)
    uuids = b.get("record_uuids") or ([b["record_uuid"]] if b.get("record_uuid") else [])
    if not isinstance(uuids, list) or not uuids:
        return jsonify({"error": "record_uuids required"}), 400
    res = _stage_rows(sid, b.get("action"), uuids, b.get("dest_dir"))
    return jsonify(res), res.pop("_status", 200)


@app.route("/api/stage/summary")
def api_stage_summary():
    sid = session_arg()
    rows = load_staged(sid)
    return jsonify({"pending": rows, "counts": {a: sum(1 for r in rows if r["action"] == a) for a in ("trash", "move", "copy")}})


@app.route("/api/stage/clear", methods=["POST"])
def api_stage_clear():
    sid = session_arg(body())
    with MUTATE_LOCK:
        save_staged(sid, [])
    return jsonify({"ok": True})


@app.route("/api/stage/remove", methods=["POST"])
def api_stage_remove():
    b = body()
    sid = session_arg(b)
    stage_id = b.get("stage_id")
    with MUTATE_LOCK:
        save_staged(sid, [r for r in load_staged(sid) if r["stage_id"] != stage_id])
    return jsonify({"ok": True})


def _apply_moves(sid: str, moved: List[dict]) -> None:
    """moved: [{record_uuid, group_id, new_path}] -> group entity + source index + group index."""
    by_group: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    all_updates: Dict[str, Dict[str, Any]] = {}
    for m in moved:
        upd = {"file_path": m["new_path"], "file_name": Path(m["new_path"]).name}
        by_group[m["group_id"]][m["record_uuid"]] = upd
        all_updates[m["record_uuid"]] = upd
    for gid, ups in by_group.items():
        name = Grouper._find_group_entity_name(sid, gid)
        if name:
            Common.bulk_update_fields(Grouper.group_db_manifest(sid), name, ups)
    Common.bulk_update_fields(Grouper.source_index_manifest(sid), Grouper.SOURCE_ENTITY, all_updates)
    Common.bulk_update_fields(Grouper.group_db_manifest(sid), Grouper.GROUP_INDEX_ENTITY,
                              {u: {"file_path": v["file_path"]} for u, v in all_updates.items()})
    invalidate_caches()


def _groups_losing_every_copy(sid: str, rows: List[dict]) -> Dict[str, int]:
    """group_id -> file count, for groups where EVERY still-existing member is queued for trash."""
    trash_by_group: Dict[str, set] = defaultdict(set)
    for r in rows:
        if r["action"] == "trash":
            trash_by_group[r["group_id"]].add(r["record_uuid"])
    doomed: Dict[str, int] = {}
    for gid, uuids in trash_by_group.items():
        name = Grouper._find_group_entity_name(sid, gid)
        if not name:
            continue
        alive = {m["record_uuid"] for m in group_rows(sid, name) if os.path.exists(m.get("file_path") or "")}
        if alive and alive <= uuids:
            doomed[gid] = len(alive)
    return doomed


@app.route("/api/stage/review")
def api_stage_review():
    """Everything the commit dialog needs: per-action counts, bytes, and groups that would lose every copy."""
    sid = session_arg()
    rows = load_staged(sid)
    sizes = {a: 0 for a in ("trash", "move", "copy")}
    for r in rows:
        try:
            sizes[r["action"]] += os.path.getsize(r["file_path"])
        except OSError:
            pass
    return jsonify({"counts": {a: sum(1 for r in rows if r["action"] == a) for a in sizes}, "bytes": sizes,
                    "groups_losing_every_copy": _groups_losing_every_copy(sid, rows)})


@app.route("/api/stage/commit", methods=["POST"])
def api_stage_commit():
    b = body()
    sid = session_arg(b)
    force = bool(b.get("allow_delete_all_copies", False))
    with MUTATE_LOCK:
        rows = load_staged(sid)
        if not rows:
            return jsonify({"ok": True, "committed": 0, "failed": [], "dissolved_groups": [], "freed_bytes": 0,
                            "trashed_record_uuids": [], "blocked_groups": []})
        blocked = [] if force else list(_groups_losing_every_copy(sid, rows))
        if blocked:
            rows_to_run = [r for r in rows if not (r["action"] == "trash" and r["group_id"] in blocked)]
        else:
            rows_to_run = rows
        held_back = [r for r in rows if r not in rows_to_run]
        rows = rows_to_run
        trash_dir(sid).mkdir(parents=True, exist_ok=True)
        reconcile_trash(sid)
        done_ids, failed, trash_entries, moved = set(), [], [], []
        freed = 0
        trashed_by_group: Dict[str, List[str]] = defaultdict(list)
        for r in rows:
            try:
                src = Path(r["file_path"])
                if not src.exists():
                    done_ids.add(r["stage_id"])
                    failed.append({"file": src.name, "error": "missing on disk (dropped from queue)"})
                    continue
                if not path_is_allowed(src, sid):
                    raise PermissionError("source outside allowed roots")
                if r["action"] in ("move", "copy"):
                    if not path_is_allowed(r["dest_dir"], sid):
                        raise PermissionError("destination outside allowed roots")
                    dest_dir = Path(r["dest_dir"])
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest = Common.unique_destination(dest_dir, src.name)
                    if r["action"] == "move":
                        Common.safe_move(src, dest)
                        moved.append({"record_uuid": r["record_uuid"], "group_id": r["group_id"], "new_path": str(dest)})
                    else:
                        Common.safe_copy(src, dest)
                else:
                    dest = Common.unique_destination(trash_dir(sid), f"{r['record_uuid']}_{src.name}")
                    freed += src.stat().st_size
                    Common.safe_move(src, dest)
                    gname = Grouper._find_group_entity_name(sid, r["group_id"])
                    gmembers = group_rows(sid, gname) if gname else []
                    trash_entries.append({"record_uuid": r["record_uuid"], "group_id": r["group_id"],
                                          "original_path": str(src), "trash_path": str(dest), "trashed_at": Common.utc_now_iso(),
                                          "group_basis": (gmembers[0].get("match_basis") if gmembers else None),
                                          "group_siblings": ",".join(m["record_uuid"] for m in gmembers)})
                    trashed_by_group[r["group_id"]].append(r["record_uuid"])
                done_ids.add(r["stage_id"])
            except Exception as exc:
                Log.warning(f"Commit failed for {r.get('file_path')}: {exc}")
                failed.append({"file": Path(r.get("file_path") or "?").name, "error": f"{type(exc).__name__}: {exc}"})

        # Bookkeeping runs even if some items failed: whatever physically moved is recorded.
        if trash_entries:
            _write_rows(trash_log_path(sid), "TrashLogEntry", TRASH_FIELDS, _read_rows(trash_log_path(sid)) + trash_entries)
        if moved:
            _apply_moves(sid, moved)
        dissolved = []
        for gid, uuids in trashed_by_group.items():
            name_before = Grouper._find_group_entity_name(sid, gid)
            Grouper.remove_members(sid, gid, uuids, dismiss=False)
            if name_before and not Grouper._find_group_entity_name(sid, gid):
                dissolved.append(gid)
        invalidate_caches()
        save_staged(sid, [r for r in rows if r["stage_id"] not in done_ids] + held_back)   # failures + guarded items stay queued
    ok_count = len([1 for r in rows if r["stage_id"] in done_ids]) - len([f for f in failed if "missing" in f["error"]])
    return jsonify({"ok": not failed or ok_count > 0, "committed": ok_count, "failed": failed, "dissolved_groups": dissolved,
                    "freed_bytes": freed, "trashed_record_uuids": [e["record_uuid"] for e in trash_entries],
                    "blocked_groups": blocked})


# ---------------------------------------------------------------------------
# Routes: trash (list / restore / empty)
# ---------------------------------------------------------------------------

_TRASH_NAME_RE = re.compile(r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})_(.+)$")


def reconcile_trash(sid: str) -> int:
    """Crash recovery: commit moves files into trash/ and writes the log afterwards, so a crash in between left
    files you could neither see nor restore. Any file named <record_uuid>_<name> that the log does not know is
    re-registered, with its original path looked up from the index."""
    tdir = trash_dir(sid)
    if not tdir.is_dir():
        return 0
    entries = _read_rows(trash_log_path(sid))
    known = {e["trash_path"] for e in entries}
    index = source_by_uuid(sid)
    added = []
    for f in sorted(tdir.iterdir()):
        if not f.is_file() or f.is_symlink() or f.name == trash_log_path(sid).name or ".part-" in f.name or str(f) in known:
            continue
        m = _TRASH_NAME_RE.match(f.name)
        if not m:
            continue
        ruid = m.group(1).lower()
        rec = index.get(ruid) or {}
        added.append({"record_uuid": ruid, "group_id": rec.get("group_id"), "original_path": rec.get("file_path") or "",
                      "trash_path": str(f), "trashed_at": Common.utc_now_iso(), "group_basis": None, "group_siblings": None})
    if added:
        _write_rows(trash_log_path(sid), "TrashLogEntry", TRASH_FIELDS, entries + added)
        Log.warning(f"Recovered {len(added)} trashed file(s) that an interrupted commit left unlogged.")
    return len(added)


@app.route("/api/trash")
def api_trash_list():
    sid = session_arg()
    with MUTATE_LOCK:
        reconcile_trash(sid)
    entries = _read_rows(trash_log_path(sid))
    for e in entries:
        e["exists"] = os.path.exists(e["trash_path"] or "")
        e["size"] = os.path.getsize(e["trash_path"]) if e["exists"] else 0
    return jsonify({"entries": entries, "total_bytes": sum(e["size"] for e in entries)})


@app.route("/api/trash/restore", methods=["POST"])
def api_trash_restore():
    b = body()
    sid = session_arg(b)
    wanted = set(b.get("record_uuids") or [])
    restored, failed, regroup = 0, [], {}
    with MUTATE_LOCK:
        reconcile_trash(sid)
        entries = _read_rows(trash_log_path(sid))
        keep = []
        tdir = trash_dir(sid).resolve()
        for e in entries:
            if wanted and e["record_uuid"] not in wanted:
                keep.append(e)
                continue
            try:
                tp = Path(e["trash_path"])
                if not Common.path_within(tp, [tdir]) or not tp.exists():
                    raise FileNotFoundError("not in trash anymore")
                if not e.get("original_path"):
                    raise FileNotFoundError("original location unknown (recovered after an interrupted commit)")
                orig = Path(e["original_path"])
                if not path_is_allowed(orig.parent, sid):
                    raise PermissionError("original folder outside allowed roots")
                orig.parent.mkdir(parents=True, exist_ok=True)
                dest = Common.unique_destination(orig.parent, orig.name)
                Common.safe_move(tp, dest)
                restored += 1
                if dest != orig:                                   # original name was taken: keep the index truthful
                    upd = {e["record_uuid"]: {"file_path": str(dest), "file_name": dest.name}}
                    Common.bulk_update_fields(Grouper.source_index_manifest(sid), Grouper.SOURCE_ENTITY, upd)
                if e.get("group_id"):
                    regroup.setdefault(e["group_id"], {"basis": e.get("group_basis"), "members": set()})
                    regroup[e["group_id"]]["members"].update((e.get("group_siblings") or "").split(",") + [e["record_uuid"]])
            except Exception as exc:
                failed.append({"file": Path(e["original_path"]).name, "error": str(exc)})
                keep.append(e)
        _write_rows(trash_log_path(sid), "TrashLogEntry", TRASH_FIELDS, keep)
        regrouped = 0
        for gid, info in regroup.items():
            try:
                regrouped += 1 if Grouper.restore_group(sid, gid, [u for u in info["members"] if u], info["basis"] or "") else 0
            except Exception as exc:
                Log.warning(f"Could not restore group {gid}: {exc}")
        invalidate_caches()
    return jsonify({"ok": True, "restored": restored, "failed": failed, "groups_restored": regrouped,
                    "note": "Groups were put back where possible."})


@app.route("/api/trash/empty", methods=["POST"])
def api_trash_empty():
    sid = session_arg(body())
    removed = 0
    with MUTATE_LOCK:
        tdir = trash_dir(sid)
        if tdir.is_dir():
            for f in tdir.iterdir():
                if f.is_file() and not f.is_symlink() and f.name != trash_log_path(sid).name:
                    f.unlink()
                    removed += 1
        trash_log_path(sid).unlink(missing_ok=True)
    return jsonify({"ok": True, "permanently_removed": removed})


# ---------------------------------------------------------------------------
# Routes: per-group actions
# ---------------------------------------------------------------------------

@app.route("/api/group/<group_id>/remove_member", methods=["POST"])
def api_remove_member(group_id):
    b = body()
    sid = session_arg(b)
    gid = Common.require_uuid(group_id, "group id")
    uuids = b.get("record_uuids") or ([b["record_uuid"]] if b.get("record_uuid") else [])
    uuids = [Common.require_uuid(u, "record id") for u in uuids]
    if not uuids:
        return jsonify({"error": "record_uuids required"}), 400
    with MUTATE_LOCK:
        removed = Grouper.remove_members(sid, gid, uuids, dismiss=True)
        invalidate_caches()
    return jsonify({"ok": True, "removed": removed, "group_exists": bool(Grouper._find_group_entity_name(sid, gid))})


@app.route("/api/group/<group_id>/ungroup", methods=["POST"])
def api_ungroup(group_id):
    sid = session_arg(body())
    with MUTATE_LOCK:
        ok = Grouper.perform_del_group(sid, Common.require_uuid(group_id, "group id"))
        invalidate_caches()
    return (jsonify({"ok": True}) if ok else (jsonify({"error": "group not found"}), 404))


@app.route("/api/group/<group_id>/resolve", methods=["POST"])
def api_resolve(group_id):
    b = body()
    sid = session_arg(b)
    gid = Common.require_uuid(group_id, "group id")
    value = bool(b.get("resolved", True))
    with MUTATE_LOCK:
        resolved = load_resolved_set(sid)
        (resolved.add if value else resolved.discard)(gid)
        save_resolved_set(sid, resolved)
    return jsonify({"ok": True, "resolved": value})


@app.route("/api/group/<group_id>/verify", methods=["POST"])
def api_verify(group_id):
    sid = session_arg(body())
    with MUTATE_LOCK:
        res = Grouper.verify_group(sid, Common.require_uuid(group_id, "group id"))
        invalidate_caches()
    return (jsonify(res) if res else (jsonify({"error": "group not found"}), 404))


KEEP_STRATEGIES = {"highest_res", "newest", "oldest", "largest_size", "shortest_path"}


def pick_keeper(members: List[dict], strategy: str) -> dict:
    if strategy == "highest_res":
        return max(members, key=lambda m: (_pixels(m), m.get("file_size_bytes") or 0))
    if strategy == "largest_size":
        return max(members, key=lambda m: m.get("file_size_bytes") or 0)
    if strategy == "newest":
        return max(members, key=lambda m: m.get("file_modified_time") or "")
    if strategy == "oldest":
        return min(members, key=lambda m: m.get("file_modified_time") or "")
    return min(members, key=lambda m: (len((m.get("file_path") or "").split("/")), len(m.get("file_path") or "")))


@app.route("/api/group/<group_id>/auto_keep", methods=["POST"])
def api_auto_keep(group_id):
    """Stages trash for every member except the keeper. Does NOT commit."""
    b = body()
    sid = session_arg(b)
    gid = Common.require_uuid(group_id, "group id")
    strategy = b.get("strategy")
    if strategy not in KEEP_STRATEGIES:
        return jsonify({"error": f"strategy must be one of {sorted(KEEP_STRATEGIES)}"}), 400
    name = Grouper._find_group_entity_name(sid, gid)
    if not name:
        return jsonify({"error": "group not found"}), 404
    members = [m for m in group_rows(sid, name) if os.path.exists(m.get("file_path") or "")]
    if len(members) < 2:
        return jsonify({"error": "fewer than 2 files of this group still exist"}), 409
    keeper = pick_keeper(members, strategy)
    res = _stage_rows(sid, "trash", [m["record_uuid"] for m in members if m["record_uuid"] != keeper["record_uuid"]], None)
    return jsonify({"ok": True, "keeper_record_uuid": keeper["record_uuid"], "staged_for_trash": res["staged"]})


BULK_TIME_BUDGET_SECONDS = 90


@app.route("/api/groups/auto_keep_all", methods=["POST"])
def api_auto_keep_all():
    """Bulk 'resolve exact copies': for every open group whose files are proven byte-identical
    (SHA-256, computed here if needed) keep one by the chosen rule and stage the rest for trash.
    Groups that are merely similar are NEVER touched - those need a human. Nothing is committed."""
    b = body()
    sid = session_arg(b)
    strategy = b.get("strategy")
    if strategy not in KEEP_STRATEGIES:
        return jsonify({"error": f"strategy must be one of {sorted(KEEP_STRATEGIES)}"}), 400
    started = time.time()
    applied = staged = skipped_not_identical = 0
    partial = False
    for g in group_summaries(sid):
        if g["resolved"]:
            continue
        if time.time() - started > BULK_TIME_BUDGET_SECONDS:
            partial = True
            break
        with MUTATE_LOCK:
            verdict = Grouper.verify_group(sid, g["group_id"])
            invalidate_caches()
        if not verdict or verdict["verdict"] != "all_identical":
            skipped_not_identical += 1
            continue
        members = [m for m in group_rows(sid, g["entity_name"]) if os.path.exists(m.get("file_path") or "")]
        if len(members) < 2:
            continue
        keeper = pick_keeper(members, strategy)
        res = _stage_rows(sid, "trash", [m["record_uuid"] for m in members if m["record_uuid"] != keeper["record_uuid"]], None)
        staged += res.get("staged", 0)
        applied += 1
    return jsonify({"ok": True, "groups_applied": applied, "files_staged_for_trash": staged,
                    "groups_skipped_not_identical": skipped_not_identical, "partial": partial})


# ---------------------------------------------------------------------------
# Routes: folder browser + notes
# ---------------------------------------------------------------------------

@app.route("/api/browse")
def api_browse():
    sid = session_arg()
    raw = request.args.get("path")
    if not raw:
        raw = Scanner.get_config_value(_config_doc, "drive_scan_root", "/storage/emulated/0")
        if not Path(raw).is_dir():
            target = Scanner.load_scan_state(sid).get("target_path")
            if target and Path(target).is_dir():
                raw = target
    target = Path(raw)
    if not target.is_dir():
        return jsonify({"error": f"'{raw}' is not accessible. On Termux run termux-setup-storage, or check viewer_allowed_roots."}), 400
    if not path_is_allowed(target, sid):
        return jsonify({"error": "path is outside the allowed roots"}), 403
    try:
        entries = [{"name": c.name, "path": str(c)} for c in sorted(target.iterdir(), key=lambda p: p.name.lower())
                   if c.is_dir() and not c.name.startswith(".")]
    except PermissionError:
        return jsonify({"error": "permission denied"}), 403
    parent = target.parent if target != target.parent and path_is_allowed(target.parent, sid) else None
    return jsonify({"current_path": str(target), "parent_path": str(parent) if parent else None, "entries": entries})


@app.route("/api/notes", methods=["GET", "POST"])
def api_notes():
    if request.method == "GET":
        text = NOTES_PATH.read_text(encoding="utf-8") if NOTES_PATH.exists() else ""
        return jsonify({"text": text})
    text = body().get("text")
    if not isinstance(text, str) or len(text.encode("utf-8")) > NOTES_MAX_BYTES:
        return jsonify({"error": "text required (max 200 KB)"}), 400
    NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = NOTES_PATH.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, NOTES_PATH)
    return jsonify({"ok": True, "saved_at": Common.utc_now_iso()})


@app.route("/api/scan_status")
def api_scan_status():
    sid = session_arg()
    scan = Scanner.load_scan_state(sid)
    grp = Grouper.load_group_state(sid)
    return jsonify({"scan": {k: scan.get(k) for k in ("status", "profile", "mode", "target_path", "last_checkpoint_processed", "unreadable_files", "last_error")},
                    "group": {k: grp.get(k) for k in ("status", "method", "groups_formed_last_run", "last_error")}})


# ---------------------------------------------------------------------------
# Entrypoint (Policy 8.6: port constant, URL to clipboard, browser launch, then serve)
# ---------------------------------------------------------------------------

def copy_to_clipboard(text: str) -> bool:
    if shutil.which("termux-clipboard-set") is None:
        return False
    try:
        subprocess.run(["termux-clipboard-set"], input=text, text=True, timeout=5, check=True)
        return True
    except Exception:
        return False


def open_in_browser(url: str) -> bool:
    for cmd in ("termux-open-url", "termux-open"):
        if shutil.which(cmd):
            try:
                subprocess.Popen([cmd, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                return False
    return False


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT, lan: bool = False, launch: bool = True) -> None:
    global ALLOWED_HOSTS
    ALLOWED_HOSTS = set() if lan else {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    url = f"http://{'127.0.0.1' if not lan else '<this device LAN IP>'}:{port}/?token={VIEWER_API_TOKEN}"
    Log.info(f"group_viewer v{VERSION} - open: {url}")
    Log.info(f"Auth: user='{VIEWER_USER}' pass='{VIEWER_PASSWORD}' | Token: {VIEWER_API_TOKEN}")
    if lan:
        Log.warning("--lan: reachable by anything on your network (includes move/trash endpoints). Trusted networks only.")
    if copy_to_clipboard(url):
        Log.info("URL copied to clipboard.")
    if launch and not lan and open_in_browser(url):
        Log.info("Browser launch requested.")
    app.run(host=host, port=port, debug=False, threaded=True)


def main(argv: Optional[List[str]] = None) -> int:
    global VIEWER_USER, VIEWER_PASSWORD
    p = argparse.ArgumentParser(description="group_viewer.py - duplicate review web UI")
    p.add_argument("--lan", action="store_true", help="Bind 0.0.0.0 (default 127.0.0.1).")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--user", type=str, default=None, help=f"Web UI username (default: {VIEWER_USER})")
    p.add_argument("--password", type=str, default=None, help=f"Web UI password (default: {VIEWER_PASSWORD})")
    p.add_argument("--rotate-token", action="store_true", help="Generate a new access token (signs out every browser) and exit.")
    Common.add_standard_flags(p)
    args = p.parse_args(argv)
    if args.user:
        VIEWER_USER = args.user
    if args.password:
        VIEWER_PASSWORD = args.password
    rc = Common.handle_standard_flags(args, SCRIPT_NAME, VERSION, SCRIPT_PATH, Scanner.CONFIG_PATH, Scanner.reset_config_to_defaults)
    if rc is not None:
        return rc
    if args.rotate_token:
        Scanner.set_config_value("viewer_api_token", secrets.token_urlsafe(24), "Shared-secret token for group_viewer.")
        try:
            os.chmod(Scanner.CONFIG_PATH, 0o600)
        except OSError:
            pass
        Log.info("New viewer token written to config. Start the viewer to get the new sign-in link; old sessions are invalid.")
        return Common.EXIT_OK
    serve("0.0.0.0" if args.lan else "127.0.0.1", args.port, args.lan, not args.no_browser)
    return Common.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

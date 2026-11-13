#!/usr/bin/env python3
"""
Name/Library:   lib_prohasher_picker.py
Family:         ProHasher (app-owned)
Description:    Browser-based folder picker for terminal tools (Policy 8.5: never ask the
                 user to type/paste a path in the console). Starts a short-lived 127.0.0.1
                 web page, opens it (termux-open-url when available), returns the chosen
                 directory. On timeout / failure it returns None and the caller aborts with
                 exit code 10 (Policy 8.7) - there is deliberately NO manual-typing fallback.
Version:        1
Date Created:   2026-10-18
Author:         Elton Boehnen
Contact:        boehnenelton2024@gmail.com | boehnenelton2024.pages.dev | github.com/boehnenelton
Format_Creator: Elton Boehnen
RELATIONAL_ID:  0d6f2b94-71e3-4c8a-b5a1-93e47c10d2f6
"""
from __future__ import annotations

import logging
import secrets
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Iterable, List, Optional

from flask import Flask, abort, jsonify, request
from werkzeug.serving import make_server

import lib_prohasher_common as Common

Log = logging.getLogger("prohasher_picker")
PICKER_PORT = 8421

_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Choose a folder</title><style>
body{font-family:sans-serif;margin:0;background:#fff;color:#000}header{padding:12px;background:#000;color:#fff;font-weight:700}
#path{padding:10px;font-family:monospace;font-size:13px;border-bottom:1px solid #ddd;word-break:break-all}
.row{padding:12px;border-bottom:1px solid #eee;cursor:pointer}.row:hover{background:#fdeaea;color:#DE2626}
#bar{position:sticky;bottom:0;padding:10px;background:#fff;border-top:1px solid #ddd;display:flex;gap:8px}
button{flex:1;padding:12px;border:0;border-radius:6px;font-weight:700;cursor:pointer}#ok{background:#DE2626;color:#fff}#no{background:#eee}
</style></head><body><header>Image-ProHasher &middot; choose a folder</header><div id="path"></div><div id="list"></div>
<div id="bar"><button id="ok">Use this folder</button><button id="no">Cancel</button></div>
<script>
const base=location.pathname.replace(/\\/$/,"");let cur=null;
async function ls(p){const r=await fetch(base+"/ls?path="+encodeURIComponent(p||""));const d=await r.json();
if(d.error){alert(d.error);return}cur=d.current;document.getElementById("path").textContent=cur;
const list=document.getElementById("list");list.replaceChildren();
if(d.parent){const u=document.createElement("div");u.className="row";u.textContent=".. (up)";u.onclick=()=>ls(d.parent);list.append(u)}
for(const e of d.entries){const x=document.createElement("div");x.className="row";x.textContent="\\ud83d\\udcc1 "+e.name;x.onclick=()=>ls(e.path);list.append(x)}}
document.getElementById("ok").onclick=async()=>{await fetch(base+"/choose",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:cur})});document.body.textContent="Folder chosen - return to the terminal."};
document.getElementById("no").onclick=async()=>{await fetch(base+"/choose",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:null})});document.body.textContent="Cancelled."};
ls("");
</script></body></html>"""


def build_picker_app(roots: List[Path], start: Path, secret: str, result: dict, done: threading.Event) -> Flask:
    app = Flask(__name__)

    def ok_path(p: Path) -> bool:
        return p.is_dir() and Common.path_within(p, roots)

    @app.route(f"/p/{secret}")
    def page():
        return _PAGE

    @app.route(f"/p/{secret}/ls")
    def ls():
        raw = request.args.get("path") or str(start)
        p = Path(raw)
        if not ok_path(p):
            return jsonify({"error": "folder not accessible or outside allowed roots"}), 403
        try:
            entries = [{"name": c.name, "path": str(c)} for c in sorted(p.iterdir(), key=lambda x: x.name.lower())
                       if c.is_dir() and not c.name.startswith(".")]
        except PermissionError:
            return jsonify({"error": "permission denied"}), 403
        parent = p.parent if p != p.parent and ok_path(p.parent) else None
        return jsonify({"current": str(p), "parent": str(parent) if parent else None, "entries": entries})

    @app.route(f"/p/{secret}/choose", methods=["POST"])
    def choose():
        data = request.get_json(silent=True) or {}
        chosen = data.get("path")
        if chosen is not None and not ok_path(Path(chosen)):
            abort(403)
        result["path"] = chosen
        done.set()
        return jsonify({"ok": True})

    return app


def pick_directory(roots: Iterable[Path], start: Optional[Path] = None, timeout: int = 180,
                   port: int = PICKER_PORT, launch: bool = True) -> Optional[str]:
    roots = [Path(r).resolve() for r in roots if Path(r).exists()]
    if not roots:
        Log.error("Folder picker: no accessible roots (run termux-setup-storage?).")
        return None
    start = Path(start).resolve() if start and Path(start).exists() else roots[0]
    secret, result, done = secrets.token_urlsafe(16), {"path": None}, threading.Event()
    try:
        server = make_server("127.0.0.1", port, build_picker_app(roots, start, secret, result, done), threaded=True)
    except OSError as exc:
        Log.error(f"Folder picker could not bind 127.0.0.1:{port}: {exc}")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/p/{secret}"
    Log.info(f"Folder picker: {url}")
    opened = False
    if launch:
        for cmd in ("termux-open-url", "termux-open"):
            if shutil.which(cmd):
                try:
                    subprocess.Popen([cmd, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    opened = True
                except Exception:
                    pass
                break
    if not opened:
        Log.info("Open the URL above in your browser to choose a folder.")
    try:
        if not done.wait(timeout):
            Log.error("Folder picker timed out.")
            return None
    finally:
        server.shutdown()
    return result["path"]

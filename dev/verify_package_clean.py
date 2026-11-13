#!/usr/bin/env python3
"""
Name/Library:   dev/verify_package_clean.py
Description:    Fails (exit 1) if a delivery ZIP or folder contains user data or secrets:
                 session/index/group data, thumbnails, images, nested archives, bytecode,
                 generated config, API tokens, personal file paths inside data files, or
                 non-empty debug notes. Run before every delivery:
                     python3 dev/verify_package_clean.py Image-ProHasher-V11-PKG6.zip
Version:        1
Date Created:   2026-10-20
Author:         Elton Boehnen
Contact:        boehnenelton2024@gmail.com | boehnenelton2024.pages.dev | github.com/boehnenelton
Format_Creator: Elton Boehnen
RELATIONAL_ID:  d4b7e920-3a61-4f8c-95d2-0c8e1a7f6b33
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Tuple

VERSION = 1
FORBIDDEN_DIRS = {"sessions", "thumb_cache", "__pycache__", "temp_index", "index.old", "image_holder", "trash", "persist", ".pytest_cache"}
FORBIDDEN_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".tif", ".tiff", ".zip", ".tar", ".gz", ".7z", ".pyc", ".lock", ".flag"}
KEEP_ONLY_DIRS = {"backups", "data", "exports", "config"}            # must be empty apart from .keep
ALLOWED_IMAGE_PREFIXES: Tuple[str, ...] = ()                           # the viewer ships no raster assets
TEXT_EXT = {".py", ".js", ".css", ".html", ".md", ".txt", ".json", ".bejson", ".sh", ".keep", ""}
SECRET_PATTERNS = [
    (re.compile(r"viewer_api_token[\"',:\s\]\[]+[A-Za-z0-9_\-]{20,}"), "viewer_api_token with a value"),
    (re.compile(r"\b(ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_\-]{30,}|AKIA[0-9A-Z]{16})"), "API key shape"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
]
PERSONAL_PATH = re.compile(r"/(storage/emulated/0|sdcard)/(DCIM|Pictures|Download|WhatsApp|Movies|Camera)[^\"'\s]*", re.I)
DATA_EXT = {".bejson"}
PATH_EXEMPT = {".bejson_project.json"}


def _entries(target: Path) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    if target.is_file():
        with zipfile.ZipFile(target) as z:
            for info in z.infolist():
                if not info.is_dir():
                    out[info.filename] = z.read(info)
    else:
        for p in target.rglob("*"):
            if p.is_file():
                out[str(PurePosixPath(p.relative_to(target)))] = p.read_bytes()
    return out


def verify(target: Path | str) -> List[str]:
    findings: List[str] = []
    for name, data in _entries(Path(target)).items():
        parts = [p for p in PurePosixPath(name).parts]
        base, ext = parts[-1], PurePosixPath(name).suffix.lower()
        if any(p in FORBIDDEN_DIRS for p in parts[:-1]):
            findings.append(f"forbidden directory in path: {name}")
        if ext in FORBIDDEN_EXT:
            findings.append(f"forbidden file type: {name}")
        for i, p in enumerate(parts[:-1]):
            if p in KEEP_ONLY_DIRS and base != ".keep" and "lib" not in parts[:i]:
                findings.append(f"{p}/ must be empty (generated per install): {name}")
        if base == "persistent-notes.md" and data.strip():
            findings.append(f"debug notes are not empty: {name}")
        if ext in TEXT_EXT:
            text = data.decode("utf-8", errors="ignore")
            for rx, label in SECRET_PATTERNS:
                if rx.search(text):
                    findings.append(f"{label}: {name}")
            if ext in DATA_EXT and base not in PATH_EXEMPT and PERSONAL_PATH.search(text):
                findings.append(f"personal media path inside data file: {name}")
            if base == "config.104a.bejson":
                findings.append(f"generated config must not ship: {name}")
    return findings


def main(argv=None) -> int:
    args = (argv if argv is not None else sys.argv[1:])
    if len(args) != 1:
        sys.stdout.write("usage: verify_package_clean.py <zip-or-folder>\n")
        return 2
    findings = verify(args[0])
    for f in findings:
        sys.stdout.write(f"DIRTY  {f}\n")
    sys.stdout.write("CLEAN: no user data, secrets, caches or images found.\n" if not findings else f"{len(findings)} problem(s) found.\n")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

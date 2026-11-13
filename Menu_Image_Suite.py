#!/usr/bin/env python3
"""
Name/Library:   Menu_Image_Suite.py
Family:         Image-ProHasher (menu wrapper)
Description:    Interactive menu over Cli_Image_Scanner / CLI_Image_Grouper / group_viewer.
                 Calls their cmd_* functions in-process. Long actions (scan, grouping) run on
                 a worker thread; Ctrl+C writes the same stop-flag file the standalone
                 --Stop-* commands use. Folder choices go through a browser-based picker -
                 the menu never asks you to type a path (Policy 8.5); if the picker fails
                 the action aborts (exit code 10 semantics, Policy 8.7), no typing fallback.
Version:        104
Library_Version:        103.31 (MFDB)
Date Created:   2026-08-25
Date Modified:  2026-10-18
Author:         Elton Boehnen
Contact:        boehnenelton2024@gmail.com | boehnenelton2024.pages.dev | github.com/boehnenelton
Format_Creator: Elton Boehnen
RELATIONAL_ID:  a52c8e07-3f1d-4b69-8d40-6e9b17c2f385

CHANGES vs v103: one session selector (v9 had two writing the same file); the Speed/Deep
toggle now actually takes effect; Quick Group, Deep Group, Prune and Status entries; Speed
scans auto-chain Quick-Group; typed path prompts replaced by the picker; worker-thread
exceptions are reported instead of printing "Done" over a traceback.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

SCRIPT_NAME = "Menu_Image_Suite.py"
SCRIPT_PATH = Path(__file__).resolve().parent
VERSION = 104
SCRIPT_VERSION = VERSION
RELATIONAL_ID = "a52c8e07-3f1d-4b69-8d40-6e9b17c2f385"
DATE_MODIFIED = "2026-10-18"

sys.path.insert(0, str(SCRIPT_PATH))

import Cli_Image_Scanner as Scanner      # noqa: E402  (bootstraps lib/Core + lib/ProHasher)
import CLI_Image_Grouper as Grouper      # noqa: E402
import lib_prohasher_common as Common    # noqa: E402

Log = logging.getLogger("Menu_Image_Suite")
if not Log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(Scanner.ColorFormatter("[%(asctime)s] %(levelname)s Menu_Image_Suite: %(message)s", datefmt="%H:%M:%S"))
    Log.addHandler(_h)
Log.setLevel(logging.INFO)
Log.propagate = False

R, RB, DIM, RST, WB = Scanner.ANSI_RED, Scanner.ANSI_RED_BOLD, Scanner.ANSI_DIM, Scanner.ANSI_RESET, "\033[1m\033[37m"
BOX_WIDTH = 72
last_exit_code = Common.EXIT_OK


# ---------------------------------------------------------------------------
# Terminal UI helpers (this is UI output, not logging)
# ---------------------------------------------------------------------------

def out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def box_header(title: str) -> None:
    line = "\u2550" * BOX_WIDTH
    out(f"{RB}\u2554{line}\u2557{RST}")
    out(f"{RB}\u2551{RST} {WB}{title.ljust(BOX_WIDTH - 1)}{RST}{RB}\u2551{RST}")
    out(f"{RB}\u255a{line}\u255d{RST}")


def rule() -> None:
    bar = "\u2500" * (BOX_WIDTH + 2)
    out(f"{R}{bar}{RST}")


def pause() -> None:
    input(f"\n{DIM}Press Enter to return to the menu...{RST}")


def run_action(title: str, func: Callable[[], None]) -> None:
    clear_screen()
    box_header(f"\u25b6 {title}")
    out()
    try:
        func()
        out()
        out(f"{RB}\u2714 Done: {title}{RST}")
    except KeyboardInterrupt:
        out(f"\n{RB}Interrupted.{RST}")
    except Exception as exc:
        Log.error(f"Action failed: {type(exc).__name__}: {exc}")
        out(f"\n{RB}\u2718 Failed: {title}{RST}")
    pause()


def run_stoppable(title: str, worker: Callable[[], None], stop_writer: Callable[[], None]) -> None:
    clear_screen()
    box_header(f"\u25b6 {title}  (Ctrl+C to stop)")
    out()
    errors = []

    def wrapped() -> None:
        try:
            worker()
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=wrapped, daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(timeout=0.2)
    except KeyboardInterrupt:
        out(f"\n{RB}Ctrl+C - requesting stop...{RST}")
        stop_writer()
        t.join()
    out()
    if errors:
        Log.error(f"{title} failed: {type(errors[0]).__name__}: {errors[0]}")
        out(f"{RB}\u2718 Failed: {title}{RST}")
    else:
        out(f"{RB}\u2714 Finished: {title}{RST}")
    pause()


# ---------------------------------------------------------------------------
# Pickers
# ---------------------------------------------------------------------------

def pick_session() -> Optional[str]:
    sessions = Scanner.list_session_ids()
    if not sessions:
        out(f"{RB}No sessions exist yet.{RST}")
        return None
    for i, sid in enumerate(sessions, 1):
        st = Scanner.load_scan_state(sid)
        out(f"  [{i}] {sid}  ({st.get('status')}, {st.get('profile') or 'no profile yet'})")
    raw = input("\nSession number: ").strip()
    try:
        return sessions[int(raw) - 1]
    except (ValueError, IndexError):
        out(f"{RB}Invalid selection.{RST}")
        return None


def pick_group(sid: str) -> Optional[str]:
    names = Grouper.group_entity_names(sid)
    rows = []
    for n in names:
        m = Grouper.load_group_members(sid, n)
        if m:
            rows.append((m[0]["group_id"], len(m), m[0].get("match_basis") or "legacy"))
    if not rows:
        out(f"{RB}No groups found.{RST}")
        return None
    for i, (gid, n, basis) in enumerate(rows[:200], 1):
        out(f"  [{i}] {gid[:8]}  {n} file(s)  - {basis}")
    raw = input("\nGroup number: ").strip()
    try:
        return rows[int(raw) - 1][0]
    except (ValueError, IndexError):
        out(f"{RB}Invalid selection.{RST}")
        return None


def pick_folder(prompt: str) -> Optional[str]:
    global last_exit_code
    import lib_prohasher_picker as Picker
    cfg = Scanner.ensure_config()
    roots = [Path(p.strip()) for p in str(Scanner.get_config_value(cfg, "viewer_allowed_roots", "") or
                                          Scanner.get_config_value(cfg, "drive_scan_root", "")).split(",") if p.strip()]
    out(f"{prompt} - choose it in the browser page that opens.")
    chosen = Picker.pick_directory(roots, launch=True)
    if not chosen:
        last_exit_code = Common.EXIT_PICKER_FAILURE
        Log.error(f"No folder chosen (picker failed or cancelled). Action aborted, exit code {Common.EXIT_PICKER_FAILURE}.")
    return chosen


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def action_new_session() -> None:
    Scanner.cmd_new_session(argparse.Namespace())


def action_sel_session() -> None:
    sid = pick_session()
    if sid:
        Scanner.set_selected_session_id(sid)
        out(f"{RB}Selected session: {sid}{RST}")


def action_del_session() -> None:
    sid = pick_session()
    if sid and input(f"{RB}Type DELETE to permanently remove session {sid}: {RST}").strip() == "DELETE":
        Scanner.cmd_del_session(argparse.Namespace(session_id=sid))
    else:
        out("Cancelled.")


def action_drive_scan() -> None:
    if not Scanner.require_selected_session():
        return
    chosen = pick_folder("Drive-scan root")
    if chosen:
        Scanner.cmd_drive_scan(argparse.Namespace(drive_path=chosen), Scanner.ensure_config())


def action_dir_scan() -> None:
    if not Scanner.require_selected_session():
        return
    chosen = pick_folder("Folder to scan")
    if chosen:
        Scanner.cmd_dir_scan(argparse.Namespace(dir_path=chosen))


def action_start_scan() -> None:
    sid = Scanner.require_selected_session()
    if not sid:
        return
    cfg = Scanner.ensure_config()

    def worker() -> None:
        Scanner.cmd_start_scan(argparse.Namespace(profile=None), cfg)
        st = Scanner.load_scan_state(sid)
        if (st.get("status") == "complete" and st.get("profile") == "speed"
                and Scanner.get_config_value(cfg, "auto_quick_group", True)):
            Grouper.cmd_quick_group(argparse.Namespace(rebuild=False))

    def stop_writer() -> None:
        Scanner.cmd_stop_scan(argparse.Namespace())

    run_stoppable("Start Scan", worker, stop_writer)


def action_toggle_profile() -> None:
    cfg = Scanner.ensure_config()
    now_speed = bool(Scanner.get_config_value(cfg, "speedhash_mode", True))
    Scanner.set_config_value("speedhash_mode", not now_speed)
    out(f"{RB}Scan profile is now {'DEEP (hash everything)' if now_speed else 'SPEED (metadata only, quick groups)'}.{RST}")
    out("Applies to the next Start Scan - no session state is cached any more.")


def action_quick_group() -> None:
    sid = Grouper.require_selected_session()
    if not sid:
        return
    rebuild = input("Dissolve unresolved groups and rebuild first? [y/N]: ").strip().lower() == "y"
    Grouper.cmd_quick_group(argparse.Namespace(rebuild=rebuild))


def action_deep_group() -> None:
    sid = Grouper.require_selected_session()
    if not sid:
        return
    run_stoppable("Deep Grouping (hash backfill + pHash clusters)",
                  lambda: Grouper.cmd_start_grouping(argparse.Namespace()),
                  lambda: Grouper.cmd_stop_grouping(argparse.Namespace()))


def action_prune() -> None:
    Grouper.cmd_prune(argparse.Namespace())


def action_view_group() -> None:
    sid = Grouper.require_selected_session()
    gid = pick_group(sid) if sid else None
    if gid:
        Grouper.cmd_view_group(argparse.Namespace(group_id=gid))


def action_del_group() -> None:
    sid = Grouper.require_selected_session()
    gid = pick_group(sid) if sid else None
    if gid and input(f"{RB}Type DELETE to ungroup {gid[:8]}: {RST}").strip() == "DELETE":
        Grouper.cmd_del_group(argparse.Namespace(group_id=gid))


def action_status() -> None:
    Scanner.cmd_status(argparse.Namespace())
    sid = Scanner.get_selected_session_id()
    if sid:
        gs = Grouper.load_group_state(sid)
        Log.info(f"grouping: status={gs.get('status')} method={gs.get('method')} groups={len(Grouper.group_entity_names(sid)) if Path(Grouper.group_db_manifest(sid)).exists() else 0}")


def action_launch_viewer() -> None:
    import group_viewer as Viewer
    out(f"{R}Ctrl+C to stop the viewer.{RST}")
    try:
        Viewer.serve("127.0.0.1", Viewer.DEFAULT_PORT, lan=False, launch=True)
    except KeyboardInterrupt:
        pass


def action_about() -> None:
    box_header("About")
    out(f"\n  {WB}Image-ProHasher{RST}\n  {SCRIPT_NAME} v{VERSION}  ({DATE_MODIFIED})\n"
        f"  Duplicate-image scanner, grouper and review viewer (BEJSON/MFDB).\n\n  {DIM}{Common.AUTHOR_CREDIT}{RST}\n")


# (key, label, handler, stoppable_handles_own_screen)
MENU_ITEMS = [
    ("1", "New Session", action_new_session, False),
    ("2", "Select Session", action_sel_session, False),
    ("3", "Delete Session", action_del_session, False),
    ("4", "Drive Scan (choose root)", action_drive_scan, False),
    ("5", "Folder Scan (choose folder)", action_dir_scan, False),
    ("6", "Start Scan", action_start_scan, True),
    ("7", "Toggle scan profile (Speed / Deep)", action_toggle_profile, False),
    ("8", "Quick Group (heuristics, instant)", action_quick_group, False),
    ("9", "Deep Group (hashes + pHash)", action_deep_group, True),
    ("10", "Prune vanished files", action_prune, False),
    ("11", "View Group (copy to holder)", action_view_group, False),
    ("12", "Ungroup a Group", action_del_group, False),
    ("13", "Status", action_status, False),
    ("14", "Launch Viewer", action_launch_viewer, False),
    ("a", "About", action_about, False),
    ("0", "Quit", None, False),
]


def status_line() -> str:
    sid = Scanner.get_selected_session_id()
    if not sid:
        return f"{DIM}No session selected{RST}"
    st = Scanner.load_scan_state(sid)
    profile = "SPEED" if Scanner.get_config_value(Scanner.ensure_config(), "speedhash_mode", True) else "DEEP"
    return f"Session {R}{sid[:8]}...{RST}  mode={st.get('mode') or '-'}  scan={st.get('status')}  next-profile={RB}{profile}{RST}"


def main() -> int:
    while True:
        clear_screen()
        box_header("BEJSON IMAGE SUITE")
        out(status_line())
        rule()
        for key, label, *_ in MENU_ITEMS:
            out(f" {RB}[{key}]{RST} {label}")
        rule()
        choice = input("\nChoice: ").strip().lower()
        item = next((i for i in MENU_ITEMS if i[0] == choice), None)
        if not item:
            out(f"{RB}Invalid choice.{RST}")
            pause()
            continue
        key, label, handler, owns_screen = item
        if key == "0":
            clear_screen()
            out(f"{RB}Goodbye.{RST}")
            return last_exit_code
        if owns_screen:
            handler()
        else:
            run_action(label, handler)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Image-ProHasher interactive menu")
    Common.add_standard_flags(ap)
    a = ap.parse_args()
    rc0 = Common.handle_standard_flags(a, SCRIPT_NAME, VERSION, SCRIPT_PATH, Scanner.CONFIG_PATH, Scanner.reset_config_to_defaults)
    sys.exit(rc0 if rc0 is not None else main())

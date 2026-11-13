#!/usr/bin/env python3
"""
Name/Library:   tests/test_prohasher.py
Description:    Regression suite for Image-ProHasher v10 (stdlib unittest, no pytest needed).
                 Runs against an isolated temp sandbox - never touches real config/sessions.
                 Run:  python3 -m unittest discover -s tests -v
Version:        5
Date Created:   2026-10-18
Author:         Elton Boehnen
Contact:        boehnenelton2024@gmail.com | boehnenelton2024.pages.dev | github.com/boehnenelton
Format_Creator: Elton Boehnen
RELATIONAL_ID:  32685006-338a-44de-b564-f054b97c6e4a
"""
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "lib" / "Core"), str(ROOT / "lib" / "ProHasher")]

from PIL import Image, ImageDraw  # noqa: E402

SANDBOX = Path(tempfile.mkdtemp(prefix="prohasher-test-"))
import Cli_Image_Scanner as Scanner  # noqa: E402

Scanner.SESSIONS_ROOT = SANDBOX / "sessions"
Scanner.CONFIG_DIR = SANDBOX / "config"
Scanner.CONFIG_PATH = Scanner.CONFIG_DIR / "config.104a.bejson"
Scanner.CURRENT_SESSION_PATH = Scanner.CONFIG_DIR / "current_session.104a.bejson"
import CLI_Image_Grouper as Grouper  # noqa: E402
import lib_prohasher_common as Common  # noqa: E402


def make_img(path, seed, size=(640, 480), exif_dt=None, cam="TestCam"):
    r = random.Random(seed)
    im = Image.new("RGB", size, (r.randint(0, 255),) * 3)
    d = ImageDraw.Draw(im)
    for _ in range(30):
        x0, x1 = sorted((r.randint(0, size[0]), r.randint(0, size[0])))
        y0, y1 = sorted((r.randint(0, size[1]), r.randint(0, size[1])))
        d.rectangle([x0, y0, x1, y1], fill=tuple(r.randint(0, 255) for _ in range(3)))
    ex = Image.Exif()
    if exif_dt:
        ex[0x0110] = cam
        ex[0x0132] = exif_dt
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "JPEG", quality=92, exif=ex.tobytes() if exif_dt else b"")
    return im


class TestValidators(unittest.TestCase):
    def test_uuid(self):
        self.assertTrue(Common.is_valid_uuid("8847cccd-cd3b-400d-8022-43e82e6cd50c"))
        for bad in ("../..", "", None, "8847cccd", "8847cccd-cd3b-400d-8022-43e82e6cd50c/../x", 5):
            self.assertFalse(Common.is_valid_uuid(bad))
        with self.assertRaises(ValueError):
            Common.require_uuid("../../etc", "session")

    def test_session_dir_rejects_traversal(self):
        with self.assertRaises(ValueError):
            Scanner.session_dir("../../x")

    def test_unique_destination_never_overwrites(self):
        d = Path(tempfile.mkdtemp(dir=SANDBOX))
        (d / "a.jpg").write_text("1")
        (d / "a (1).jpg").write_text("2")
        self.assertEqual(Common.unique_destination(d, "a.jpg").name, "a (2).jpg")
        self.assertEqual(Common.unique_destination(d, "new.jpg").name, "new.jpg")

    def test_path_within_resolves_symlinks(self):
        base = Path(tempfile.mkdtemp(dir=SANDBOX))
        (base / "in").mkdir()
        outside = Path(tempfile.mkdtemp(dir=SANDBOX))
        os.symlink(outside, base / "in" / "link")
        self.assertTrue(Common.path_within(base / "in", [base.resolve()]))
        self.assertFalse(Common.path_within(base / "in" / "link", [(base / "in").resolve()]))


class TestKVState(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=SANDBOX))

    def test_roundtrip_and_atomic(self):
        st = Common.KVState(self.dir / "s.104a.bejson", {"a": 1, "b": None})
        st.set_and_save(a=5, c="x")
        again = Common.KVState(self.dir / "s.104a.bejson", {"a": 1, "b": None})
        self.assertEqual((again.get("a"), again.get("b"), again.get("c")), (5, None, "x"))

    def test_corrupt_file_quarantined(self):
        p = self.dir / "s.104a.bejson"
        p.write_text("{not json")
        st = Common.KVState(p, {"a": 1})
        self.assertEqual(st.get("a"), 1)
        self.assertTrue(list(self.dir.glob("s.104a.bejson.corrupt-*")))

    def test_legacy_104db_migration(self):
        p = self.dir / "legacy.104db.bejson"
        p.write_text(json.dumps({
            "Format": "BEJSON", "Format_Version": "104db", "Records_Type": ["StateNode", "History"],
            "Fields": [{"name": "Record_Type_Parent"}, {"name": "key"}, {"name": "value"}, {"name": "timestamp"}, {"name": "snapshot"}],
            "Values": [["StateNode", "status", json.dumps("complete"), None, None], ["History", None, None, "t", "{}"]]}))
        st = Common.KVState(p, {"status": "idle"})
        self.assertEqual(st.get("status"), "complete")

    def test_crashed_run_detected(self):
        st = Common.KVState(self.dir / "s.104a.bejson", {"status": "idle", "run_pid": None, "heartbeat_at": None})
        st.set_and_save(status="running", run_pid=2_000_000_000, heartbeat_at="2020-01-01T00:00:00+00:00")
        fresh = Common.KVState(self.dir / "s.104a.bejson", {"status": "idle", "run_pid": None, "heartbeat_at": None})
        self.assertTrue(fresh.recover_interrupted())
        self.assertEqual(fresh.get("status"), "interrupted")

    def test_own_process_run_not_marked_dead(self):
        st = Common.KVState(self.dir / "s.104a.bejson", {"status": "idle", "run_pid": None, "heartbeat_at": None})
        st.begin_run("running")
        self.assertFalse(st.recover_interrupted())


class TestBulkOps(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=SANDBOX))
        self.manifest = Scanner.ensure_index_db(self.root, "speed")

    def rec(self, i):
        return {"record_uuid": f"u{i}", "file_path": f"/x/{i}.jpg", "file_size_bytes": i}

    def test_bulk_append_update_remove(self):
        self.assertEqual(Common.bulk_append_records(self.manifest, "ImageRecord", [self.rec(i) for i in range(5)]), 5)
        self.assertEqual(Common.bulk_update_fields(self.manifest, "ImageRecord", {"u2": {"width": 99}, "nope": {"width": 1}}), 1)
        self.assertEqual(Common.bulk_remove_records(self.manifest, "ImageRecord", {"u0", "u4"}), 2)
        rows = Scanner.load_index_records(self.root)
        self.assertEqual(sorted(r["record_uuid"] for r in rows), ["u1", "u2", "u3"])
        self.assertEqual([r["width"] for r in rows if r["record_uuid"] == "u2"], [99])
        from lib_bejson_Core_mfdb_core import mfdb_core_get_stats
        self.assertEqual(mfdb_core_get_stats(self.manifest)["entities"][0]["record_count"], 3)

    def test_entity_removal_order_is_crash_safe(self):
        spec = {"name": "Group_aaaaaaaaaaaa", "fields": Grouper.GROUP_ENTITY_FIELDS, "primary_key": "record_uuid", "records": [], "file_path_rel": "data/group_aaaaaaaaaaaa.bejson"}
        Common.bulk_create_entities(self.manifest, [spec])
        self.assertIn("Group_aaaaaaaaaaaa", Common.manifest_entity_names(self.manifest))
        self.assertTrue(Common.remove_entity_registration(self.manifest, "Group_aaaaaaaaaaaa"))
        self.assertNotIn("Group_aaaaaaaaaaaa", Common.manifest_entity_names(self.manifest))
        self.assertFalse((self.root / "data" / "group_aaaaaaaaaaaa.bejson").exists())

    def test_entity_cache_invalidates_on_change(self):
        Common.bulk_append_records(self.manifest, "ImageRecord", [self.rec(1)])
        path = Common.entity_file_path(self.manifest, "ImageRecord")
        cache = Common.EntityCache()
        self.assertEqual(len(cache.load(path)[0]), 1)
        Common.bulk_append_records(self.manifest, "ImageRecord", [self.rec(2)])
        self.assertEqual(len(cache.load(path)[0]), 2)


class TestHashing(unittest.TestCase):
    def test_dct_matches_v9_formula(self):
        import math
        def d1(v):
            n = len(v)
            return [sum(v[i] * math.cos((math.pi / n) * (i + 0.5) * k) for i in range(n)) * (2.0 if k else 1 / 2 ** 0.5) for k in range(n)]
        r = random.Random(1)
        m = [[float(r.randint(0, 255)) for _ in range(32)] for _ in range(32)]
        rows = [d1(row) for row in m]
        cols = [d1([rows[y][x] for y in range(32)]) for x in range(32)]
        old = [cols[x][y] for y in range(8) for x in range(8)]
        new = Common._dct_low_freq(m)
        self.assertLess(max(abs(a - b) for a, b in zip(old, new)), 1e-6)

    def test_rotation_hashes_match_hashing_rotated_image(self):
        p = SANDBOX / "rot.jpg"
        make_img(p, 11)
        a = Common.compute_phash_set(p, use_draft=False)
        rot = SANDBOX / "rot90.jpg"
        Image.open(p).rotate(-90, expand=True).save(rot, quality=100)
        b = Common.compute_phash_set(rot, use_draft=False)
        self.assertLessEqual(min(Common.hamming_hex(a[1], x) for x in b), 6)
        self.assertLessEqual(Common.phash_min_distance({"phash_original": b[0]}, {f: v for f, v in zip(("phash_original", "phash_rotate_90", "phash_rotate_180", "phash_rotate_270"), a)}), 6)

    def test_different_images_far_apart(self):
        make_img(SANDBOX / "d1.jpg", 21)
        make_img(SANDBOX / "d2.jpg", 22)
        a, b = Common.compute_phash_set(SANDBOX / "d1.jpg"), Common.compute_phash_set(SANDBOX / "d2.jpg")
        self.assertGreater(min(Common.hamming_hex(a[0], x) for x in b), 10)


class TestQuickHeuristics(unittest.TestCase):
    def R(self, u, size=1000, w=100, h=80, dt=None, cam="C", name="x.jpg"):
        return {"record_uuid": u, "file_size_bytes": size, "width": w, "height": h, "exif_datetime": dt, "camera_model": cam, "file_name": name}

    def test_size_dims_and_exif_merge(self):
        recs = [self.R("a", 1000, dt="t1"), self.R("b", 1000, dt="t2"), self.R("c", 2000, dt="t1"), self.R("d", 3000, dt="t9")]
        cl, _ = Grouper.quick_candidates(recs, 25, False)
        self.assertEqual(len(cl), 1)
        self.assertEqual(sorted(cl[0]["members"]), ["a", "b", "c"])
        self.assertEqual(Grouper.basis_text(cl[0]["basis_set"]), f"{Grouper.B_SIZE} + {Grouper.B_EXIF}")

    def test_same_size_different_dims_not_grouped(self):
        cl, _ = Grouper.quick_candidates([self.R("a", 1000, w=100), self.R("b", 1000, w=101)], 25, False)
        self.assertEqual(cl, [])

    def test_oversized_bucket_ignored(self):
        recs = [self.R(f"u{i}", 1000) for i in range(30)]
        cl, st = Grouper.quick_candidates(recs, 25, False)
        self.assertEqual((cl, st["buckets_skipped_too_big"]), ([], 1))

    def test_filename_heuristic_opt_in(self):
        recs = [self.R("a", 1, name="holiday_beach.jpg"), self.R("b", 2, name="holiday_beach (1).jpg")]
        self.assertEqual(Grouper.quick_candidates(recs, 25, False)[0], [])
        self.assertEqual(len(Grouper.quick_candidates(recs, 25, True)[0]), 1)

    def test_normalized_stem(self):
        for n in ("IMG_4821 (2).jpg", "IMG_4821-copy.jpg", "img_4821_1.JPG"):
            self.assertEqual(Grouper.normalized_stem(n), "img_4821")

    def test_phash_cluster_union_and_ratio_gate(self):
        h = lambda x: f"{x:016x}"
        rows = [
            {"record_uuid": "a", "phash_original": h(0), "phash_rotate_90": h(2**63), "phash_rotate_180": h(2**62), "phash_rotate_270": h(2**61), "width": 100, "height": 80},
            {"record_uuid": "b", "phash_original": h(0b111), "phash_rotate_90": h(2**60), "phash_rotate_180": h(2**59), "phash_rotate_270": h(2**58), "width": 200, "height": 160},
            {"record_uuid": "c", "phash_original": h(2**64 - 1), "phash_rotate_90": h(2**64 - 2), "phash_rotate_180": h(2**64 - 3), "phash_rotate_270": h(2**64 - 5), "width": 100, "height": 80},
            {"record_uuid": "d", "phash_original": h(0b1), "phash_rotate_90": h(1), "phash_rotate_180": h(1), "phash_rotate_270": h(1), "width": 100, "height": 20},  # ratio far -> gated
        ]
        out = Grouper.cluster_phash(rows, 10)
        self.assertEqual([sorted(g) for g in out], [["a", "b"]])


class TestPipeline(unittest.TestCase):
    """End to end on real files: speed scan -> quick groups -> deep -> prune/reconcile -> rescan stability."""

    @classmethod
    def setUpClass(cls):
        cls.imgs = SANDBOX / "imgs"
        a = cls.imgs / "a"
        b = cls.imgs / "b"
        make_img(a / "p1.jpg", 1, exif_dt="2024:05:01 10:00:00")
        shutil.copy(a / "p1.jpg", b / "p1_copy.jpg") if b.mkdir(parents=True, exist_ok=True) is None else None
        Image.open(a / "p1.jpg").resize((320, 240)).save(b / "p1_small.jpg", quality=90)
        make_img(a / "p2.jpg", 2, exif_dt="2024:06:02 09:00:00")
        make_img(b / "p2_other.jpg", 22, exif_dt="2024:06:02 09:00:00")
        for i in range(3, 7):
            make_img(a / f"u{i}.jpg", i, size=(600 + i * 9, 400 + i * 4))
        make_img(cls.imgs / ".hidden" / "h.jpg", 99)

    def new_session(self):
        Scanner.main(["--New-Session"])
        sid = Scanner.get_selected_session_id()
        Scanner.main(["--Dir-Scan", str(self.imgs)])
        return sid

    def test_speed_pipeline(self):
        sid = self.new_session()
        Scanner.main(["--Start-Scan", "--Speed"])
        st = Scanner.load_scan_state(sid)
        self.assertEqual((st.get("status"), st.get("profile")), ("complete", "speed"))
        recs = Scanner.load_index_records(Scanner.index_root(sid))
        self.assertEqual(len(recs), 9)                                    # hidden dir skipped
        self.assertTrue(all(r["sha256_hash"] is None and r["phash_original"] is None for r in recs))
        self.assertEqual(len({r["record_uuid"] for r in recs}), 9)
        Grouper.main(["--Quick-Group"])
        names = Grouper.group_entity_names(sid)
        self.assertEqual(len(names), 2)
        bases = sorted(Grouper.load_group_members(sid, n)[0]["match_basis"] for n in names)
        self.assertEqual(bases, sorted([f"{Grouper.B_SIZE} + {Grouper.B_EXIF}", Grouper.B_EXIF]))
        before = len(names)
        Grouper.main(["--Quick-Group"])                                   # idempotent
        self.assertEqual(len(Grouper.group_entity_names(sid)), before)
        gid = Grouper.load_group_members(sid, names[0])[0]["group_id"]
        res = Grouper.verify_group(sid, gid)
        self.assertIn(res["verdict"], ("all_identical", "mixed", "unrelated", "all_similar"))
        self.assertGreater(res["hashed_now"], 0)

    def test_dismissed_is_remembered(self):
        sid = self.new_session()
        Scanner.main(["--Start-Scan", "--Speed"])
        Grouper.main(["--Quick-Group"])
        name = [n for n in Grouper.group_entity_names(sid) if Grouper.load_group_members(sid, n)[0]["match_basis"] == Grouper.B_EXIF][0]
        members = Grouper.load_group_members(sid, name)
        Grouper.remove_members(sid, members[0]["group_id"], [members[0]["record_uuid"]], dismiss=True)
        self.assertNotIn(name, Grouper.group_entity_names(sid))           # fell below 2 -> dissolved
        Grouper.main(["--Quick-Group", "--Rebuild"])
        self.assertNotIn(members[0]["record_uuid"], {m["record_uuid"] for n in Grouper.group_entity_names(sid) for m in Grouper.load_group_members(sid, n)})

    def test_deep_finds_resized_copy_and_rescan_keeps_ids(self):
        sid = self.new_session()
        Scanner.main(["--Start-Scan", "--Deep"])
        ids1 = {r["file_path"]: r["record_uuid"] for r in Scanner.load_index_records(Scanner.index_root(sid))}
        Grouper.main(["--Start-Grouping"])
        found = {m["file_name"] for n in Grouper.group_entity_names(sid) for m in Grouper.load_group_members(sid, n)}
        self.assertTrue({"p1.jpg", "p1_small.jpg", "p1_copy.jpg"} <= found)
        Scanner.main(["--Start-Scan", "--Deep"])                          # rescan
        ids2 = {r["file_path"]: r["record_uuid"] for r in Scanner.load_index_records(Scanner.index_root(sid))}
        self.assertEqual(ids1, ids2)                                      # stable uuids: groups stay valid
        self.assertEqual(Grouper.reconcile(sid), {"index_rows_fixed": 0, "empty_groups_removed": 0})

    def test_crash_between_entity_and_index_is_repaired(self):
        sid = self.new_session()
        Scanner.main(["--Start-Scan", "--Speed"])
        Grouper.main(["--Quick-Group"])
        name = Grouper.group_entity_names(sid)[0]
        members = Grouper.load_group_members(sid, name)
        Common.bulk_update_fields(Grouper.group_db_manifest(sid), Grouper.GROUP_INDEX_ENTITY, {m["record_uuid"]: {"group_id": None} for m in members})
        self.assertGreater(Grouper.reconcile(sid)["index_rows_fixed"], 0)
        idx = {r["record_uuid"]: r for r in Grouper.group_index_rows(sid)}
        self.assertTrue(all(idx[m["record_uuid"]]["group_id"] == members[0]["group_id"] for m in members))

    def test_prune_dissolves_when_files_vanish(self):
        sid = self.new_session()
        Scanner.main(["--Start-Scan", "--Speed"])
        Grouper.main(["--Quick-Group"])
        name = Grouper.group_entity_names(sid)[0]
        victim = Grouper.load_group_members(sid, name)[0]["file_path"]
        os.rename(victim, victim + ".gone")
        try:
            res = Grouper.prune_missing(sid)
            self.assertEqual(res["groups_dissolved"], 1)
            self.assertNotIn(name, Grouper.group_entity_names(sid))
        finally:
            os.rename(victim + ".gone", victim)

    def test_interrupted_state_not_stuck(self):
        sid = self.new_session()
        st = Scanner.load_scan_state(sid)
        st.set_and_save(status="running", run_pid=2_000_000_000, heartbeat_at="2020-01-01T00:00:00+00:00")
        self.assertEqual(Scanner.load_scan_state(sid).get("status"), "interrupted")
        Scanner.main(["--Start-Scan", "--Speed"])                         # resumable, not blocked
        self.assertEqual(Scanner.load_scan_state(sid).get("status"), "complete")


class TestViewer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import group_viewer as V
        cls.V = V
        cls.imgs = SANDBOX / "vimgs"
        make_img(cls.imgs / "a" / "x.jpg", 5, exif_dt="2024:01:01 01:01:01")
        (cls.imgs / "b").mkdir(parents=True, exist_ok=True)
        shutil.copy(cls.imgs / "a" / "x.jpg", cls.imgs / "b" / "x.jpg")
        shutil.copy(cls.imgs / "a" / "x.jpg", cls.imgs / "b" / "x_dup.jpg")
        Scanner.main(["--New-Session"])
        cls.sid = Scanner.get_selected_session_id()
        Scanner.main(["--Dir-Scan", str(cls.imgs)])
        Scanner.main(["--Start-Scan", "--Speed"])
        Grouper.main(["--Quick-Group"])
        cls.c = V.app.test_client()
        cls.c.get("/?token=" + V.VIEWER_API_TOKEN)

    def groups(self):
        return self.c.get(f"/api/groups?session={self.sid}&state=all").get_json()["items"]

    def test_auth_and_no_token_leak(self):
        anon = self.V.app.test_client()
        self.assertEqual(anon.get("/api/sessions").status_code, 401)
        self.assertEqual(anon.post("/login", data={"token": "wrong"}).status_code, 403)
        html = self.c.get("/").get_data(as_text=True)
        self.assertNotIn(self.V.VIEWER_API_TOKEN, html)

    def test_login_form_works_with_null_origin(self):
        """Browsers send 'Origin: null' on the sign-in form POST (Referrer-Policy: no-referrer). Was a 403."""
        anon = self.V.app.test_client()
        ok = anon.post("/login", data={"token": self.V.VIEWER_API_TOKEN}, headers={"Origin": "null"})
        self.assertEqual(ok.status_code, 302)
        self.assertIn("gv_token", ok.headers.get("Set-Cookie", ""))
        self.assertEqual(anon.post("/login", data={"token": "wrong"}, headers={"Origin": "null"}).status_code, 403)   # wrong token still refused
        # ...but the JSON API never accepts a null/foreign Origin
        self.assertEqual(self.c.post("/api/stage/clear", json={"session": self.sid}, headers={"Origin": "null"}).status_code, 403)

    def test_username_password_and_basic_auth(self):
        anon = self.V.app.test_client()
        # 1) Valid username and password login
        ok_up = anon.post("/login", data={"username": self.V.VIEWER_USER, "password": self.V.VIEWER_PASSWORD})
        self.assertEqual(ok_up.status_code, 302)
        self.assertIn("gv_token", ok_up.headers.get("Set-Cookie", ""))

        # 2) Wrong username or password
        bad_up = anon.post("/login", data={"username": self.V.VIEWER_USER, "password": "wrongpassword"})
        self.assertEqual(bad_up.status_code, 403)

        # 3) Basic Auth header
        import base64
        creds = base64.b64encode(f"{self.V.VIEWER_USER}:{self.V.VIEWER_PASSWORD}".encode()).decode()
        res_basic = anon.get("/api/sessions", headers={"Authorization": f"Basic {creds}"})
        self.assertEqual(res_basic.status_code, 200)

    def test_sign_in_page_assets_load_before_login(self):
        anon = self.V.app.test_client()
        page = anon.get("/login").get_data(as_text=True)
        import re as _re
        hrefs = _re.findall(r'href="([^"]+\.css)"', page)
        self.assertTrue(hrefs)
        for h in hrefs:                                  # the sign-in page must be styled for someone who is not signed in yet
            r = anon.get(h)
            self.assertEqual((r.status_code, r.mimetype), (200, "text/css"), h)
        self.assertEqual(anon.get("/api/sessions").status_code, 401)            # ...while the API stays locked

    def test_refusals_explain_themselves(self):
        self.V.ALLOWED_HOSTS = {"127.0.0.1:8420"}
        try:
            r = self.c.get("/", headers={"Host": "0.0.0.0:8420"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("127.0.0.1:8420", r.get_data(as_text=True))
            api = self.c.get("/api/sessions", headers={"Host": "evil.example"})
            self.assertIn("not allowed", api.get_json()["error"].lower())
        finally:
            self.V.ALLOWED_HOSTS = set()

    def test_traversal_and_injection_rejected(self):
        self.assertEqual(self.c.get("/api/groups?session=../..").status_code, 400)
        self.assertEqual(self.c.post("/api/trash/empty", json={"session": "../../../tmp"}).status_code, 400)
        self.assertIn(self.c.get(f"/image/{self.sid}/..%2f..%2fetc").status_code, (400, 404))
        self.assertEqual(self.c.post("/api/stage/clear", json={"session": self.sid}, headers={"Origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(self.c.post("/api/stage/clear", data="x").status_code, 415)

    def test_unhashed_tiles_do_not_collapse(self):
        g = self.groups()[0]
        tiles = self.c.get(f"/api/group/{g['group_id']}/members?session={self.sid}").get_json()["tiles"]
        self.assertEqual(len(tiles), g["member_count"])

    def test_verify_collapses_identical_after_hashing(self):
        g = self.groups()[0]
        res = self.c.post(f"/api/group/{g['group_id']}/verify", json={"session": self.sid}).get_json()
        self.assertEqual(res["verdict"], "all_identical")
        tiles = self.c.get(f"/api/group/{g['group_id']}/members?session={self.sid}").get_json()["tiles"]
        self.assertEqual(len(tiles), 1)
        self.assertEqual(tiles[0]["copy_count"], g["member_count"])

    def test_commit_trash_restore_roundtrip_and_no_overwrite(self):
        imgs = SANDBOX / "vimgs2"
        make_img(imgs / "a" / "k.jpg", 7, exif_dt="2024:02:02 02:02:02")
        shutil.copy(imgs / "a" / "k.jpg", imgs / "b" / "k.jpg") if (imgs / "b").mkdir(parents=True, exist_ok=True) is None else None
        Scanner.main(["--New-Session"])
        sid = Scanner.get_selected_session_id()
        Scanner.main(["--Dir-Scan", str(imgs)]); Scanner.main(["--Start-Scan", "--Speed"]); Grouper.main(["--Quick-Group"])
        g = self.c.get(f"/api/groups?session={sid}&state=all").get_json()["items"][0]
        basis_before = g["match_basis"]
        self.assertNotIn("legacy", basis_before)
        tiles = self.c.get(f"/api/group/{g['group_id']}/members?session={sid}").get_json()["tiles"]
        a, b = (t["representative"] for t in tiles)
        dest = imgs / "dest"
        dest.mkdir()
        (dest / Path(a["file_path"]).name).write_text("precious")
        self.c.post("/api/stage", json={"session": sid, "action": "move", "record_uuids": [a["record_uuid"]], "dest_dir": str(dest)})
        self.c.post("/api/stage", json={"session": sid, "action": "trash", "record_uuids": [b["record_uuid"]]})
        res = self.c.post("/api/stage/commit", json={"session": sid}).get_json()
        self.assertEqual((res["committed"], res["failed"]), (2, []))
        self.assertEqual((dest / Path(a["file_path"]).name).read_text(), "precious")          # never overwritten
        self.assertEqual(len(list(dest.iterdir())), 2)
        self.assertEqual(self.c.get(f"/api/group/{g['group_id']}/members?session={sid}").status_code, 404)  # dissolved
        self.assertEqual(len(self.c.get(f"/api/trash?session={sid}").get_json()["entries"]), 1)
        res = self.c.post("/api/trash/restore", json={"session": sid}).get_json()
        self.assertEqual((res["restored"], res["groups_restored"]), (1, 1))
        self.assertTrue(Path(b["file_path"]).exists())
        back = self.c.get(f"/api/group/{g['group_id']}/members?session={sid}")        # Undo brings the group back, same id
        self.assertEqual(back.status_code, 200)
        self.assertEqual(back.get_json()["member_count"], 2)
        self.assertEqual(back.get_json()["match_basis"], basis_before)               # reason survives Undo

    def test_short_reason_labels(self):
        sb = self.V.short_basis
        self.assertEqual(sb(f"{Grouper.B_SIZE} + {Grouper.B_EXIF}"), "Same size + Same photo time")
        self.assertEqual(sb(Grouper.B_SHA), "Identical")
        self.assertEqual(sb(f"{Grouper.B_PHASH} + {Grouper.B_SHA}"), "Identical + Looks alike")
        self.assertEqual(sb("something v9"), "Legacy group")
        g = self.groups()[0]
        self.assertNotEqual(g["reason"], "Legacy group")

    def test_outside_roots_blocked(self):
        g = self.groups()[0]
        t = self.c.get(f"/api/group/{g['group_id']}/members?session={self.sid}").get_json()["tiles"][0]["representative"]
        r = self.c.post("/api/stage", json={"session": self.sid, "action": "move", "record_uuids": [t["record_uuid"]], "dest_dir": "/etc"})
        self.assertEqual(r.status_code, 403)

    def test_notes_roundtrip(self):
        self.V.NOTES_PATH = SANDBOX / "notes.md"
        self.assertTrue(self.c.post("/api/notes", json={"text": "debug line"}).get_json()["ok"])
        self.assertEqual(self.c.get("/api/notes").get_json()["text"], "debug line")


class TestSafeFileOps(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(dir=SANDBOX))
        (self.d / "a.txt").write_text("payload")

    def test_same_fs_move_and_no_overwrite(self):
        Common.safe_move(self.d / "a.txt", self.d / "b.txt")
        self.assertEqual((self.d / "b.txt").read_text(), "payload")
        self.assertFalse((self.d / "a.txt").exists())
        (self.d / "c.txt").write_text("keep me")
        with self.assertRaises(FileExistsError):
            Common.safe_move(self.d / "b.txt", self.d / "c.txt")
        self.assertEqual((self.d / "c.txt").read_text(), "keep me")

    def test_cross_device_move_verifies_before_deleting_source(self):
        import errno
        from unittest import mock
        real_rename = os.rename
        def fake_rename(a, b):
            raise OSError(errno.EXDEV, "cross-device")
        with mock.patch("os.rename", fake_rename):
            Common.safe_move(self.d / "a.txt", self.d / "moved.txt")
        self.assertEqual((self.d / "moved.txt").read_text(), "payload")
        self.assertFalse((self.d / "a.txt").exists())

    def test_failed_copy_never_loses_source_or_leaves_partial(self):
        import errno
        from unittest import mock
        def short_copy(src, dst, *a, **k):
            Path(dst).write_text("pay")                      # truncated, like a full disk
            return dst
        def fake_rename(a, b):
            raise OSError(errno.EXDEV, "cross-device")
        with mock.patch("os.rename", fake_rename), mock.patch("shutil.copy2", short_copy):
            with self.assertRaises(OSError):
                Common.safe_move(self.d / "a.txt", self.d / "dest.txt")
        self.assertEqual((self.d / "a.txt").read_text(), "payload")                       # source intact
        self.assertEqual(sorted(p.name for p in self.d.iterdir()), ["a.txt"])             # no partial/.part file left


class TestHardenedState(unittest.TestCase):
    def test_pid_ownership_survives_stale_heartbeat_but_not_pid_reuse(self):
        import subprocess
        d = Path(tempfile.mkdtemp(dir=SANDBOX))
        ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)  # Cli_Image_Scanner.py"])
        other = subprocess.Popen(["sleep", "30"])
        try:
            self.assertTrue(Common.pid_is_ours(ours.pid))
            self.assertFalse(Common.pid_is_ours(other.pid))              # alive, but an unrelated program (recycled PID)
            defaults = {"status": "idle", "run_pid": None, "heartbeat_at": None}
            st = Common.KVState(d / "a.bejson", defaults)
            st.set_and_save(status="running", run_pid=ours.pid, heartbeat_at="2020-01-01T00:00:00+00:00")
            self.assertFalse(Common.KVState(d / "a.bejson", defaults).recover_interrupted())    # slow batch != crash
            st2 = Common.KVState(d / "b.bejson", defaults)
            st2.set_and_save(status="running", run_pid=other.pid, heartbeat_at=Common.utc_now_iso())
            self.assertTrue(Common.KVState(d / "b.bejson", defaults).recover_interrupted())     # PID reused -> interrupted
        finally:
            ours.kill(); other.kill()

    def test_corrupt_config_is_quarantined_not_overwritten(self):
        Scanner.ensure_config()
        Scanner.CONFIG_PATH.write_text("{ definitely not bejson")
        doc = Scanner.ensure_config()
        self.assertTrue(Scanner.get_config_value(doc, "speedhash_mode") in (True, False))
        self.assertTrue(list(Scanner.CONFIG_DIR.glob("config.104a.bejson.corrupt-*")))

    def test_exif_subsec_separates_burst_frames(self):
        paths = []
        for i, sub in enumerate(("120", "480")):
            im = Image.new("RGB", (64, 48), (i * 50, 9, 9))
            ex = Image.Exif()
            ifd = ex.get_ifd(0x8769)
            ifd[0x9003] = "2024:01:01 00:00:00"
            ifd[0x9291] = sub
            p = SANDBOX / f"burst{i}.jpg"
            im.save(p, "JPEG", exif=ex.tobytes())
            paths.append(p)
        got = [Common.read_exif_summary(Image.open(p))["exif_datetime"] for p in paths]
        self.assertEqual(got, ["2024:01:01 00:00:00.120", "2024:01:01 00:00:00.480"])
        recs = [{"record_uuid": f"b{i}", "file_size_bytes": 100 + i, "width": 64, "height": 48, "exif_datetime": g, "camera_model": "C"} for i, g in enumerate(got)]
        self.assertEqual(Grouper.quick_candidates(recs, 25, False)[0], [])             # different frames no longer grouped

    def test_entity_path_map_matches_per_entity_lookup(self):
        root = Path(tempfile.mkdtemp(dir=SANDBOX))
        man = Scanner.ensure_index_db(root, "speed")
        specs = [{"name": f"Group_{i:012x}", "fields": Grouper.GROUP_ENTITY_FIELDS, "primary_key": "record_uuid", "records": [], "file_path_rel": f"data/group_{i:012x}.bejson"} for i in range(5)]
        Common.bulk_create_entities(man, specs)
        pmap = Common.entity_path_map(man)
        for n in Common.manifest_entity_names(man):
            self.assertEqual(pmap[n], Common.entity_file_path(man, n))


class TestViewerHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import group_viewer as V
        cls.V = V
        imgs = SANDBOX / "hard"
        make_img(imgs / "a" / "h.jpg", 31, exif_dt="2024:03:03 03:03:03")
        (imgs / "b").mkdir(parents=True, exist_ok=True)
        shutil.copy(imgs / "a" / "h.jpg", imgs / "b" / "h.jpg")
        Scanner.main(["--New-Session"])
        cls.sid = Scanner.get_selected_session_id()
        Scanner.main(["--Dir-Scan", str(imgs)]); Scanner.main(["--Start-Scan", "--Speed"]); Grouper.main(["--Quick-Group"])

    def client(self):
        c = self.V.app.test_client()
        r = c.get("/?token=" + self.V.VIEWER_API_TOKEN)
        return c, r

    def test_cookie_is_a_session_not_the_token_and_logout_revokes_it(self):
        c, r = self.client()
        cookie = r.headers["Set-Cookie"]
        self.assertNotIn(self.V.VIEWER_API_TOKEN, cookie)
        self.assertEqual(c.get("/api/sessions").status_code, 200)
        self.assertEqual(c.post("/api/logout", json={}).status_code, 200)
        self.assertEqual(c.get("/api/sessions").status_code, 401)
        self.assertEqual(c.get("/api/sessions", headers={"X-API-Token": self.V.VIEWER_API_TOKEN}).status_code, 200)   # scripts still work

    def test_forged_and_expired_sessions_rejected(self):
        c = self.V.app.test_client()
        c.set_cookie("gv_token", self.V.VIEWER_API_TOKEN)                         # the raw token is NOT a valid cookie any more
        self.assertEqual(c.get("/api/sessions").status_code, 401)
        c2, _ = self.client()
        for k in list(self.V._SESSIONS):
            self.V._SESSIONS[k] = 0
        self.assertEqual(c2.get("/api/sessions").status_code, 401)

    def test_response_headers(self):
        c, _ = self.client()
        r = c.get("/api/sessions")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        for h in ("Permissions-Policy", "Cross-Origin-Opener-Policy", "Content-Security-Policy", "X-Content-Type-Options"):
            self.assertIn(h, r.headers)

    def test_group_list_cache_sees_changes_immediately(self):
        c, _ = self.client()
        g = c.get(f"/api/groups?session={self.sid}&state=all").get_json()["items"][0]
        self.assertFalse(g["resolved"])
        c.post(f"/api/group/{g['group_id']}/resolve", json={"session": self.sid, "resolved": True})
        again = c.get(f"/api/groups?session={self.sid}&state=all").get_json()["items"]
        self.assertTrue([x for x in again if x["group_id"] == g["group_id"]][0]["resolved"])
        c.post(f"/api/group/{g['group_id']}/resolve", json={"session": self.sid, "resolved": False})

    def test_interrupted_commit_leaves_recoverable_trash(self):
        c, _ = self.client()
        tdir = self.V.trash_dir(self.sid); tdir.mkdir(parents=True, exist_ok=True)
        rec = list(self.V.source_by_uuid(self.sid).values())[0]
        stranded = tdir / f"{rec['record_uuid']}_{rec['file_name']}"
        os.rename(rec["file_path"], stranded)                                     # simulate: moved to trash, crashed before the log was written
        try:
            listing = c.get(f"/api/trash?session={self.sid}").get_json()
            self.assertEqual([e["record_uuid"] for e in listing["entries"]], [rec["record_uuid"]])
            res = c.post("/api/trash/restore", json={"session": self.sid}).get_json()
            self.assertEqual(res["restored"], 1)
            self.assertTrue(Path(rec["file_path"]).exists())
        finally:
            if stranded.exists():
                os.rename(stranded, rec["file_path"])

    def test_rotate_token_flag(self):
        old = self.V.VIEWER_API_TOKEN
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.V.main(["--rotate-token"]), 0)
        doc = Scanner.ensure_config()
        self.assertNotEqual(Scanner.get_config_value(doc, "viewer_api_token"), old)
        Scanner.set_config_value("viewer_api_token", old)                         # restore for the other tests


class TestHeif(unittest.TestCase):
    def test_heic_without_plugin_is_reported_not_silent(self):
        from unittest import mock
        d = SANDBOX / "heicdir"
        d.mkdir(exist_ok=True)
        (d / "x.heic").write_bytes(b"not really heic")
        Scanner.main(["--New-Session"])
        sid = Scanner.get_selected_session_id()
        Scanner.main(["--Dir-Scan", str(d)])
        import io, contextlib
        buf = io.StringIO()
        with mock.patch.object(Common, "HEIF_SUPPORT", False), mock.patch("sys.stderr", buf):
            import logging
            h = logging.StreamHandler(buf); Scanner.Log.addHandler(h)
            try:
                Scanner.main(["--Start-Scan", "--Speed"])
            finally:
                Scanner.Log.removeHandler(h)
        self.assertIn("pillow-heif", buf.getvalue())
        self.assertEqual(Scanner.load_scan_state(sid).get("unreadable_files"), 1)

    @unittest.skipUnless(Common.HEIF_SUPPORT, "pillow-heif not installed")
    def test_heic_scan_group_and_browser_rendition(self):
        import group_viewer as V
        d = SANDBOX / "heic_ok"
        d.mkdir(exist_ok=True)
        Image.new("RGB", (200, 150), (180, 40, 40)).save(d / "a.heic")
        shutil.copy(d / "a.heic", d / "b.heic")
        Scanner.main(["--New-Session"]); sid = Scanner.get_selected_session_id()
        Scanner.main(["--Dir-Scan", str(d)]); Scanner.main(["--Start-Scan", "--Speed"]); Grouper.main(["--Quick-Group"])
        self.assertEqual(len(Grouper.group_entity_names(sid)), 1)
        c = V.app.test_client(); c.get("/?token=" + V.VIEWER_API_TOKEN)
        g = c.get(f"/api/groups?session={sid}&state=all").get_json()["items"][0]
        ru = g["representative_uuid"]
        r = c.get(f"/image/{sid}/{ru}")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/jpeg"))      # browsers can show it
        self.assertEqual(c.get(f"/thumb/{sid}/{ru}").status_code, 200)


class TestPicker(unittest.TestCase):
    def test_picker_contains_to_roots_and_returns_choice(self):
        import lib_prohasher_picker as P
        root = Path(tempfile.mkdtemp(dir=SANDBOX))
        (root / "sub").mkdir()
        result, done = {"path": None}, threading.Event()
        app = P.build_picker_app([root.resolve()], root.resolve(), "sec", result, done)
        c = app.test_client()
        self.assertEqual(c.get("/p/sec/ls").get_json()["entries"][0]["name"], "sub")
        self.assertEqual(c.get("/p/sec/ls?path=/etc").status_code, 403)
        self.assertEqual(c.get("/p/WRONG/ls").status_code, 404)
        c.post("/p/sec/choose", json={"path": str(root / "sub")})
        self.assertTrue(done.is_set())
        self.assertEqual(result["path"], str(root / "sub"))
        self.assertEqual(c.post("/p/sec/choose", json={"path": "/etc"}).status_code, 403)


class TestPackageVerifier(unittest.TestCase):
    def test_flags_user_data_and_secrets(self):
        sys.path.insert(0, str(ROOT / "dev"))
        import verify_package_clean as V
        d = Path(tempfile.mkdtemp(dir=SANDBOX))
        (d / "sessions" / "x").mkdir(parents=True)
        (d / "sessions" / "x" / "i.bejson").write_text('{"p":"/storage/emulated/0/DCIM/Camera/a.jpg"}')
        (d / "gv_static").mkdir()
        (d / "gv_static" / "t.jpg").write_bytes(b"\xff\xd8")
        (d / "config").mkdir()
        (d / "config" / "config.104a.bejson").write_text('{"viewer_api_token": "' + "Zq" * 16 + '"}')
        (d / "dev").mkdir()
        (d / "dev" / "persistent-notes.md").write_text("my private note")
        found = " ".join(V.verify(d))
        for needle in ("sessions", "t.jpg", "config.104a.bejson", "viewer_api_token", "persistent-notes", "personal media path"):
            self.assertIn(needle, found)

    def test_this_tree_is_clean_of_secrets_and_images(self):
        sys.path.insert(0, str(ROOT / "dev"))
        import verify_package_clean as V
        bad = [f for f in V.verify(ROOT) if "pycache" not in f and ".pyc" not in f and "config/" not in f and "backups/" not in f]
        self.assertEqual(bad, [])


class TestStandardFlags(unittest.TestCase):
    def test_version_and_config_masking(self):
        self.assertEqual(Scanner.main(["--version"]), 0)
        Scanner.ensure_config()
        Scanner.set_config_value("viewer_api_token", "SECRETVALUE")
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            Scanner.main(["--config"])
        self.assertNotIn("SECRETVALUE", buf.getvalue())

    def test_reset_backs_up(self):
        Scanner.ensure_config()
        Scanner.main(["--reset"])
        self.assertTrue(list((SCRIPT_BACKUPS := ROOT / "backups").glob("config.104a.bejson.*.bak")))
        for f in SCRIPT_BACKUPS.glob("config.104a.bejson.*.bak"):
            f.unlink()


def tearDownModule():
    shutil.rmtree(SANDBOX, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

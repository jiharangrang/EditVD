"""프로젝트 모델·편집 연산·시간 변환·소스 순서·v1 변환의 계약을 확인해요."""
import copy
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from PIL import Image

import editvdo
from editvdo import project as P

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "test_vlog.mp4"
MISSING = object()
REAL_COVER = P._legacy_cover


def setUpModule():
    """목록을 읽는 테스트가 사용자 v1 프로젝트(test_vlog 등)에 표지를 만들지 않게, 표지는 _t_* 프로젝트에만 만들어요."""
    guard = mock.patch.object(P, "_legacy_cover", lambda d, name, dur: REAL_COVER(d, name, dur)
                              if name.startswith("_t_") else None)
    guard.start()
    unittest.addModuleCleanup(guard.stop)


@contextmanager
def fake_subs(module):
    """editvdo.subs 를 바꿔 끼워요 (None 이면 subs.py 가 없는 것처럼)."""
    saved = sys.modules.get("editvdo.subs", MISSING), vars(editvdo).get("subs", MISSING)
    sys.modules["editvdo.subs"] = module
    if module is None:
        vars(editvdo).pop("subs", None)
    else:
        editvdo.subs = module
    try:
        yield
    finally:
        for store, key, old in ((sys.modules, "editvdo.subs", saved[0]), (vars(editvdo), "subs", saved[1])):
            if old is MISSING:
                store.pop(key, None)
            else:
                store[key] = old


def mem(durs=(10.0, 5.0), fps=30, **colls):
    """디스크 없이 쓰는 프로젝트 (rev 4)."""
    p = {"rev": 4, "canvas": {"w": 1280, "h": 720, "fps": fps},
         "sources": [{"id": f"s{i}", "path": f"/clips/{i}.mp4", "duration": d} for i, d in enumerate(durs, 1)]}
    p.update(copy.deepcopy(colls))
    return P._fill(p, "_t_mem")


def cut(cid, s, e, on=True, typ="silence", **kw):
    return {"id": cid, "start": s, "end": e, "type": typ, "enabled": on, **kw}


def sub(tid, s, e, text="자막", **kw):
    return {"id": tid, "start": s, "end": e, "text": text, **kw}


def temp_name(tag):
    return f"_t_core_{tag}_{uuid.uuid4().hex[:6]}"


class TimeMapping(unittest.TestCase):
    """원본↔편집본 변환은 정확하고, 남는 조각은 소스 경계에서 나뉘고 1프레임 미만은 버려요."""

    def setUp(self):
        self.p = mem(cuts=[cut("a", 2, 3), cut("b", 3, 4), cut("off", 6, 7, on=False), cut("c", 5, 8.99),
                           cut("d", 9.0, 9.5), cut("e", 12, 15.5)])

    def test_intervals_and_segments(self):
        self.assertEqual(P.total(self.p), 15.0)
        self.assertEqual(P.cut_intervals(self.p), [(2, 4), (5, 8.99), (9.0, 9.5), (12, 15.0)])
        self.assertEqual(P.keep_segments(self.p), [(0.0, 2), (4, 5), (9.5, 10.0), (10.0, 12)])
        self.assertEqual(P.edited_duration(self.p), 5.5)

    def test_to_edited_and_back(self):
        for t, e in ((1, 1), (2.5, 2), (4, 2), (4.5, 2.5), (5, 3), (7, 3), (8.995, 3), (9.75, 3.25), (10, 3.5),
                     (11, 4.5), (13, 5.5), (15, 5.5), (-1, 0)):
            self.assertAlmostEqual(P.to_edited(self.p, t), e, places=9, msg=t)
        for e, t in ((0, 0), (1, 1), (2, 4), (2.5, 4.5), (3, 9.5), (3.25, 9.75), (3.5, 10), (5.5, 12), (99, 12),
                     (-1, 0)):
            self.assertAlmostEqual(P.to_original(self.p, e), t, places=9, msg=e)

    def test_clip_cut_and_source_at(self):
        p = mem(cuts=[cut("x", 10, 15, on=False, typ="clip")])
        self.assertEqual(P.keep_segments(p), [(0.0, 10.0)])
        for t, sid, local in ((0, "s1", 0), (9.5, "s1", 9.5), (10, "s2", 0), (12, "s2", 2), (15, "s2", 5)):
            s, x = P.source_at(p, t)
            self.assertEqual(s["id"], sid)
            self.assertAlmostEqual(x, local, places=9)
        empty = mem(durs=())
        self.assertEqual((P.total(empty), P.keep_segments(empty), P.edited_duration(empty)), (0, [], 0))
        self.assertEqual((P.to_edited(empty, 3), P.to_original(empty, 3)), (0.0, 0.0))
        with self.assertRaises(ValueError):
            P.source_at(empty, 0)

    def test_random_roundtrip_and_monotonic(self):
        rnd = random.Random(7)
        cuts = []
        for i in range(200):
            s = round(rnd.uniform(0, 98), 3)
            cuts.append(cut(f"r{i}", s, round(s + rnd.uniform(0.01, 0.6), 3), on=rnd.random() < 0.8))
        p = mem((40.0, 25.5, 33.3), cuts=cuts)
        segs = P.keep_segments(p)
        self.assertAlmostEqual(P.edited_duration(p), sum(b - a for a, b in segs), places=9)
        self.assertTrue(all(b - a >= 1 / 30 - 1e-9 for a, b in segs))
        self.assertFalse(any(a < x < b for a, b in segs for x in (40.0, 65.5)))
        for a, b in segs:
            for t in (a, (a + b) / 2, b - 1e-6):
                self.assertAlmostEqual(P.to_original(p, P.to_edited(p, t)), t, places=9)
        es = [P.to_edited(p, i / 100) for i in range(int(P.total(p) * 100) + 1)]
        self.assertEqual(es, sorted(es))
        for s, e in P.cut_intervals(p):
            self.assertAlmostEqual(P.to_edited(p, (s + e) / 2), P.to_edited(p, s), places=9)

    def test_mapping_is_fast(self):
        """컷 200개·자막 600줄에서 자막마다 두 번씩 변환해도 순식간이에요 (같은 입력은 다시 계산하지 않아요)."""
        rnd = random.Random(3)
        p = mem((1200.0, 1200.0), cuts=[cut(f"k{i}", i * 12 + 1, i * 12 + 1 + rnd.uniform(0.3, 2)) for i in range(200)])
        times = [rnd.uniform(0, 2400) for _ in range(600)]
        t0 = time.perf_counter()
        for t in times:
            P.to_original(p, P.to_edited(p, t))
            P.to_edited(p, t + 1)
        self.assertLess(time.perf_counter() - t0, 0.3)


class Ops(unittest.TestCase):
    """apply_ops: 검사·병합·rev/by 찍기·변경 기록."""

    def setUp(self):
        self.p = mem(cuts=[cut("c1", 1, 2, by="auto")],
                     subtitles=[sub("t1", 0.5, 1.5, speech=[0.5, 1.2]), sub("t2", 3, 4, speaker="A")],
                     speakers=[{"id": "A", "name": "민수"}], comments=[{"id": "m1", "t": 3, "text": "여기"}])
        ctx = fake_subs(None)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def test_new_cut_defaults_follow_writer(self):
        ch = P.apply_ops(self.p, [{"op": "put", "coll": "cuts", "item": {"start": 5, "end": 6.12345}}], "ui")
        c = ch[0]["item"]
        self.assertRegex(c["id"], r"^c[0-9a-f]{6}$")
        self.assertEqual({k: c[k] for k in ("start", "end", "type", "enabled", "reviewed", "by", "rev")},
                         {"start": 5.0, "end": 6.123, "type": "user", "enabled": True, "reviewed": True, "by": "ui", "rev": 5})
        c = P.apply_ops(self.p, [{"op": "put", "coll": "cuts", "item": {"start": 7, "end": 8, "reason": "NG"}}], "cli")[0]["item"]
        self.assertEqual((c["type"], c["reviewed"], c["by"]), ("manual", False, "cli"))

    def test_partial_merge_and_by(self):
        """결정(켜기·확인)만 바꾸면 만든 쪽(by)은 그대로, 시간을 바꾸면 쓴 쪽으로 바뀌어요."""
        ch = P.apply_ops(self.p, [{"op": "put", "coll": "cuts", "item": {"id": "c1", "enabled": False, "reviewed": True}}], "ui")
        self.assertEqual(ch, [{"coll": "cuts", "item": {**cut("c1", 1.0, 2.0, on=False), "reviewed": True, "reason": "",
                                                        "by": "auto", "rev": 5}}])
        self.assertEqual(self.p["cuts"][0]["by"], "auto")
        P.apply_ops(self.p, [{"op": "put", "coll": "cuts", "item": {"id": "c1", "start": 0.9, "rev": 99, "by": "x"}}], "ui")
        self.assertEqual((self.p["cuts"][0]["start"], self.p["cuts"][0]["by"], self.p["cuts"][0]["rev"]), (0.9, "ui", 5))
        P.apply_ops(self.p, [{"op": "put", "coll": "comments", "item": {"id": "m1", "done": True, "reply": "살렸어요"}}], "cli")
        self.assertEqual(self.p["comments"][0]["by"], "ui")

    def test_validation(self):
        before = copy.deepcopy(self.p)
        bad = [
            {"op": "put", "coll": "cuts", "item": {"start": 3, "end": 2}},
            {"op": "put", "coll": "cuts", "item": {"start": 3, "end": 16}},
            {"op": "put", "coll": "cuts", "item": {"start": -1, "end": 2}},
            {"op": "put", "coll": "cuts", "item": {"start": float("nan"), "end": 2}},
            {"op": "put", "coll": "cuts", "item": {"start": True, "end": 2}},
            {"op": "put", "coll": "cuts", "item": {"start": "1", "end": 2}},
            {"op": "put", "coll": "cuts", "item": {"start": 1, "end": 2, "type": "chunk"}},
            {"op": "put", "coll": "cuts", "item": {"id": "c1", "enabled": "yes"}},
            {"op": "put", "coll": "cuts", "item": {"id": "c1", "color": "red"}},
            {"op": "put", "coll": "cuts", "item": {"id": "../x", "start": 1, "end": 2}},
            {"op": "put", "coll": "subtitles", "item": {"id": "t1", "text": 3}},
            {"op": "put", "coll": "subtitles", "item": {"id": "t1", "pos": {"x": 50, "y": 101}}},
            {"op": "put", "coll": "subtitles", "item": {"id": "t1", "pos": {"x": 50}}},
            {"op": "put", "coll": "subtitles", "item": {"id": "t1", "speaker": "a"}},
            {"op": "put", "coll": "subtitles", "item": {"id": "t1", "speech": [1.2, 0.5]}},
            {"op": "put", "coll": "speakers", "item": {"id": "A", "color": "red"}},
            {"op": "put", "coll": "speakers", "item": {"id": "AB"}},
            {"op": "put", "coll": "comments", "item": {"t": 20}},
            {"op": "put", "coll": "words", "item": {}},
            {"op": "move", "coll": "cuts"},
            {"op": "set", "key": "canvas", "value": {}},
            {"op": "set", "key": "title", "value": "  "},
            {"op": "set", "key": "splits", "value": [1, 99]},
            {"op": "set", "key": "settings.subs", "value": {"hold": -1}},
            {"op": "set", "key": "settings.subs", "value": {"max_chars": 18.5}},
            {"op": "set", "key": "settings.subs", "value": {"speed": 1}},
            {"op": "set", "key": "settings.cut", "value": {"fillers": 1}},
        ]
        for op in bad:
            with self.subTest(op=op), self.assertRaises(ValueError):
                P.apply_ops(self.p, [op], "ui")
            self.assertEqual(self.p, before)
        with self.assertRaises(ValueError):
            P.apply_ops(self.p, {"op": "put"}, "ui")

    def test_subtitles(self):
        """손으로 넣은 줄은 지금 타이밍 설정으로 되돌아오는 speech, 시간을 직접 바꾸면 잠금, 새 화자 글자는 화자를 만들어요."""
        ch = P.apply_ops(self.p, [{"op": "put", "coll": "subtitles", "item": {"start": 6, "end": 8, "text": "새 줄"}},
                                  {"op": "put", "coll": "subtitles", "item": {"start": 9, "end": 9.3, "text": "짧"}}], "ui")
        self.assertEqual([c["item"]["speech"] for c in ch], [[6.0, 7.7], [9.0, 9.3]])
        self.assertTrue(all(c["item"]["id"].startswith("t") and c["item"]["by"] == "ui" for c in ch))
        P.apply_ops(self.p, [{"op": "put", "coll": "subtitles", "item": {"id": "t1", "end": 1.8, "pos": {"x": 30.123, "y": 20}}}], "ui")
        t1 = self.p["subtitles"][0]
        self.assertEqual((t1["end"], t1["locked"], t1["pos"], t1["speech"]), (1.8, True, {"x": 30.12, "y": 20.0}, [0.5, 1.2]))
        P.apply_ops(self.p, [{"op": "put", "coll": "subtitles", "item": {"id": "t1", "start": 0.4, "locked": False}}], "cli")
        self.assertFalse(self.p["subtitles"][0]["locked"])
        ch = P.apply_ops(self.p, [{"op": "put", "coll": "subtitles", "item": {"id": "t1", "speaker": "C"}}], "ui")
        self.assertEqual([(c["coll"], c["item"]["id"]) for c in ch], [("subtitles", "t1"), ("speakers", "C")])
        self.assertEqual(ch[1]["item"], {"id": "C", "name": "화자 3", "color": "#A78BFA", "pos": {"x": 50, "y": 88},
                                         "by": "ui", "rev": 5})
        sp = P.apply_ops(self.p, [{"op": "put", "coll": "speakers", "item": {"name": "지수"}}], "ui")[0]["item"]
        self.assertEqual((sp["id"], sp["name"], sp["color"]), ("B", "지수", "#F5A524"))

    def test_delete_set_and_records(self):
        ch = P.apply_ops(self.p, [{"op": "del", "coll": "speakers", "id": "A"},
                                  {"op": "del", "coll": "cuts", "id": "nope"},
                                  {"op": "del", "coll": "comments", "id": "m1"},
                                  {"op": "set", "key": "title", "value": "  성수 브이로그 "},
                                  {"op": "set", "key": "splits", "value": [4, 1.23456, 4.0]},
                                  {"op": "set", "key": "settings.subs", "value": {"hold": 0.5, "max_chars": 20.0}},
                                  {"op": "set", "key": "notes", "value": "메모"}], "cli")
        self.assertEqual(ch[:3], [{"coll": "subtitles", "item": self.p["subtitles"][1]},
                                  {"coll": "speakers", "id": "A", "deleted": True},
                                  {"coll": "comments", "id": "m1", "deleted": True}])
        self.assertIsNone(self.p["subtitles"][1]["speaker"])
        self.assertEqual(ch[3:], [{"key": "title", "value": "성수 브이로그"}, {"key": "splits", "value": [1.235, 4.0]},
                                  {"key": "settings.subs", "value": {**P.DEFAULT_SETTINGS["subs"], "hold": 0.5, "max_chars": 20}},
                                  {"key": "notes", "value": "메모"}])
        self.assertIsInstance(self.p["settings"]["subs"]["max_chars"], int)

    def test_retime_hook(self):
        """컷·자막·화자·타이밍 설정을 바꾸면 subs.retime 을 부르고, 바뀐 자막도 기록에 넣어요."""
        calls = []

        def retime(p):
            calls.append(p["rev"])
            p["subtitles"][1]["end"] = 4.3
            return ["t2"]

        with fake_subs(types.SimpleNamespace(retime=retime)):
            ch = P.apply_ops(self.p, [{"op": "put", "coll": "cuts", "item": {"id": "c1", "enabled": False}}], "ui")
            self.assertEqual([(c["coll"], c["item"]["id"]) for c in ch], [("cuts", "c1"), ("subtitles", "t2")])
            self.assertEqual(ch[1]["item"]["end"], 4.3)
            P.apply_ops(self.p, [{"op": "set", "key": "settings.subs", "value": {"lead": 0.1}}], "ui")
            P.apply_ops(self.p, [{"op": "put", "coll": "comments", "item": {"t": 1}},
                                 {"op": "set", "key": "notes", "value": "x"}], "ui")
            self.assertEqual(len(calls), 2)
        P.apply_ops(self.p, [{"op": "put", "coll": "cuts", "item": {"id": "c1", "enabled": True}}], "ui")  # subs 없음
        self.assertEqual(len(calls), 2)


class SourceOrder(unittest.TestCase):
    """소스 순서 바꾸기·빼기에서 항목이 자기 소스와 함께 움직여요."""

    def setUp(self):
        self.p = mem((10.0, 5.0, 8.0),
                     cuts=[cut("A", 9, 11, typ="ng"), cut("B", 16, 17), cut("C", 2, 3)],
                     subtitles=[sub("T1", 10.5, 12, speech=[10.6, 11.8]), sub("T2", 9.8, 10.9, speech=[9.85, 10.8])],
                     comments=[{"id": "M1", "t": 12}, {"id": "M2", "t": 20}], splits=[5, 13])

    def test_reorder(self):
        with fake_subs(None):
            P.reorder_sources(self.p, ["s3", "s1", "s2"])
        self.assertEqual([(s["id"], s["offset"]) for s in self.p["sources"]], [("s3", 0.0), ("s1", 8.0), ("s2", 18.0)])
        cuts = [(c["id"], c["start"], c["end"]) for c in self.p["cuts"]]
        self.assertEqual(cuts[:3], [("B", 1.0, 2.0), ("C", 10.0, 11.0), ("A", 17.0, 18.0)])
        self.assertEqual(cuts[3][1:], (18.0, 19.0))
        self.assertNotEqual(cuts[3][0], "A")
        self.assertEqual(self.p["cuts"][3]["type"], "ng")
        self.assertEqual([(t["id"], t["start"], t["end"], t["speech"]) for t in self.p["subtitles"]],
                         [("T2", 18.0, 18.9, [18.0, 18.8]), ("T1", 18.5, 20.0, [18.6, 19.8])])
        self.assertEqual([(m["id"], m["t"]) for m in self.p["comments"]], [("M2", 5.0), ("M1", 20.0)])
        self.assertEqual(self.p["splits"], [13.0, 21.0])
        with self.assertRaises(ValueError):
            P.reorder_sources(self.p, ["s1", "s2"])
        with self.assertRaises(ValueError):
            P.reorder_sources(self.p, ["s1", "s2", "s2"])

    def test_remove(self):
        with fake_subs(None):
            P.remove_source(self.p, "s2")
        self.assertEqual([(s["id"], s["offset"]) for s in self.p["sources"]], [("s1", 0.0), ("s3", 10.0)])
        self.assertEqual([(c["id"], c["start"], c["end"]) for c in self.p["cuts"]],
                         [("C", 2.0, 3.0), ("A", 9.0, 10.0), ("B", 11.0, 12.0)])
        self.assertEqual(self.p["subtitles"], [])
        self.assertEqual([(m["id"], m["t"]) for m in self.p["comments"]], [("M2", 15.0)])
        self.assertEqual(self.p["splits"], [5.0])
        self.assertEqual(P.total(self.p), 18.0)
        with self.assertRaises(ValueError):
            P.remove_source(self.p, "s9")


class OnDisk(unittest.TestCase):
    """create·load·transact·list_projects·add_sources (projects/_t_core_* 를 만들고 지워요)."""

    @classmethod
    def setUpClass(cls):
        cls.name = P.create("코어 테스트", [SAMPLE], name=temp_name("disk"))
        cls.raw = json.loads((P.PROJECTS / cls.name / "project.json").read_text())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(P.PROJECTS / cls.name, ignore_errors=True)

    def test_create_layout(self):
        d, raw = P.PROJECTS / self.name, self.raw
        self.assertEqual((raw["format"], raw["rev"], raw["title"], raw["name"]), (2, 1, "코어 테스트", self.name))
        self.assertEqual(raw["canvas"], {"w": 1280, "h": 720, "fps": 30})
        s = raw["sources"][0]
        self.assertEqual((s["id"], s["path"], s["name"], s["duration"], s["offset"], s["status"]),
                         ("s1", str(SAMPLE), "test_vlog.mp4", 34.033, 0.0, "queued"))
        self.assertEqual(json.loads((d / "sources" / "s1" / "probe.json").read_text())["duration"], 34.033)
        self.assertEqual(raw["settings"], P.DEFAULT_SETTINGS)
        with self.assertRaisesRegex(ValueError, "이미 있는"):
            P.create("x", [SAMPLE], name=self.name)
        for bad in (["/nope/missing.mov"], [], [str(ROOT / "README.md")]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                P.create("x", bad, name=temp_name("bad"))
        self.assertFalse(list(P.PROJECTS.glob("_t_core_bad_*")))

    def test_names(self):
        for bad in ("", "../x", "a b", "a/b", "x" * 61, None, ".."):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                P.check_name(bad)
        self.assertEqual(P.check_name("성수_브이로그-2"), "성수_브이로그-2")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(P, "PROJECTS", Path(tmp)):
            a, b = P.create("성수 브이로그!", [SAMPLE]), P.create("성수 브이로그!", [SAMPLE])
            c = P.create("", [SAMPLE])
            self.assertEqual((a, b), ("성수_브이로그", "성수_브이로그-2"))
            self.assertRegex(c, r"^\d{8}$")
            self.assertRegex(P.load(c)["title"], r"^\d+월 \d+일$")
        with self.assertRaises(FileNotFoundError):
            P.load(temp_name("none"))

    def test_transact(self):
        p0 = P.load(self.name)
        d = P.PROJECTS / self.name
        p, ch = P.transact(self.name, lambda p: P.apply_ops(p, [{"op": "put", "coll": "comments", "item": {"t": 1, "text": "a"}}], "ui"), by="ui")
        self.assertEqual((p["rev"], p["by"]), (p0["rev"] + 1, "ui"))
        self.assertEqual(ch[0]["item"]["rev"], p["rev"])
        self.assertEqual(P.load(self.name)["comments"][-1]["text"], "a")
        before = (d / "project.json").read_bytes()

        def boom(p):
            p["title"] = "바뀌면 안 돼요"
            raise ValueError("중간 실패")

        with self.assertRaises(ValueError):
            P.transact(self.name, boom)
        self.assertEqual((d / "project.json").read_bytes(), before)
        p, _ = P.transact(self.name, lambda p: None, snapshot="detect cuts")
        snaps = sorted((d / "history").glob("*-detect_cuts*.json"))
        self.assertEqual(json.loads(snaps[-1].read_text())["rev"], p["rev"] - 1)
        self.assertTrue(P.snapshot(self.name, "수동").exists())
        self.assertFalse(list(d.glob("*.tmp")))

    def test_concurrent_transacts_lose_nothing(self):
        start = P.load(self.name)["rev"]
        n0 = len(P.load(self.name)["comments"])

        def work(k):
            for i in range(5):
                P.transact(self.name, lambda p: P.apply_ops(
                    p, [{"op": "put", "coll": "comments", "item": {"t": 2, "text": f"{k}-{i}"}}], "ui"), by="ui")

        threads = [threading.Thread(target=work, args=(k,)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        p = P.load(self.name)
        self.assertEqual(p["rev"], start + 40)
        self.assertEqual(len(p["comments"]), n0 + 40)

    def test_list_projects(self):
        rows = {r["name"]: r for r in P.list_projects()}
        me = rows[self.name]
        self.assertEqual({k: me[k] for k in ("title", "duration", "edited", "sources", "cover")},
                         {"title": "코어 테스트", "duration": 34.033, "edited": 34.033, "sources": 1, "cover": None})
        self.assertNotIn("legacy", me)
        for legacy in ("test_vlog", "gym"):
            if (P.PROJECTS / legacy / "meta.json").exists() and not (P.PROJECTS / legacy / "project.json").exists():
                self.assertTrue(rows[legacy]["legacy"])
        if "test_vlog" in rows and rows["test_vlog"].get("legacy"):
            self.assertEqual((rows["test_vlog"]["duration"], rows["test_vlog"]["edited"]), (34.033, 22.264))
        (P.PROJECTS / self.name / "sources" / "s1" / "thumbs.jpg").write_bytes(b"x")
        cover = {r["name"]: r for r in P.list_projects()}[self.name]["cover"]
        self.assertTrue(cover.startswith("/media/_t_core_disk_") and cover.endswith("/sources/s1/thumbs.jpg"))

    def test_add_and_remove_never_reuse_ids(self):
        with fake_subs(None):
            p, _ = P.transact(self.name, lambda p: P.add_sources(p, [SAMPLE]))
            self.assertEqual([s["id"] for s in p["sources"]], ["s1", "s2"])
            self.assertEqual((p["sources"][1]["offset"], p["sources"][1]["status"]), (34.033, "queued"))
            P.transact(self.name, lambda p: P.remove_source(p, "s2"))
            p, sids = P.transact(self.name, lambda p: P.add_sources(p, [str(SAMPLE)]))
            self.assertEqual(sids, ["s3"])
            P.transact(self.name, lambda p: P.remove_source(p, "s3"))
        with self.assertRaises(ValueError):
            P.transact(self.name, lambda p: P.add_sources(p, ["/nope.mov"]))


def tree(d):
    """폴더 안 모든 파일의 (경로, 크기, 수정 시각)."""
    out = {}
    for root, dirs, files in os.walk(d):
        for f in files + dirs:
            st = os.lstat(os.path.join(root, f))
            out[os.path.join(root, f)] = (st.st_size, st.st_mtime_ns, st.st_ino)
    return out


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class LegacyCover(unittest.TestCase):
    """v1 표지: 목록에 처음 보일 때 preview.mp4 10% 지점 한 장(cover.jpg, 가로 320)을 한 번만 만들고 다른 v1 파일은 그대로."""

    def test_cover_made_once_without_touching_v1_files(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(P, "PROJECTS", Path(tmp)):
            d, bad = Path(tmp) / "_t_cover_v1", Path(tmp) / "_t_cover_bad"
            for x in (d, bad):
                x.mkdir()
                (x / "meta.json").write_text(json.dumps({"duration": 34.033, "fps": 30}), encoding="utf-8")
            (d / "edit.json").write_text(json.dumps({"cuts": [], "subtitles": []}), encoding="utf-8")
            if subprocess.run(["cp", "-c", str(SAMPLE), str(d / "preview.mp4")]).returncode:
                shutil.copy(SAMPLE, d / "preview.mp4")
            (bad / "preview.mp4").write_bytes(b"not a video")
            before = tree(d)
            rows = {r["name"]: r for r in P.list_projects()}
            self.assertEqual((rows["_t_cover_v1"]["cover"], rows["_t_cover_bad"]["cover"]),
                             ("/media/_t_cover_v1/cover.jpg", None))
            self.assertTrue(rows["_t_cover_v1"]["legacy"] and rows["_t_cover_bad"]["legacy"])
            with Image.open(d / "cover.jpg") as im:
                self.assertEqual(im.size, (320, 180))
            self.assertEqual(json.loads((d / "cover.json").read_text()), {"w": 320, "h": 180, "cols": 1, "count": 1})
            after = tree(d)
            self.assertEqual({k: v for k, v in after.items() if not k.endswith(("/cover.jpg", "/cover.json"))}, before)
            self.assertEqual(sorted(x.name for x in bad.iterdir()), ["meta.json", "preview.mp4"])
            self.assertEqual({r["name"]: r for r in P.list_projects()}["_t_cover_v1"]["cover"], "/media/_t_cover_v1/cover.jpg")
            self.assertEqual(tree(d), after)  # 이미 있으면 다시 만들지 않아요


class Migration(unittest.TestCase):
    """v1 → v2 변환 (test_vlog 를 복사한 임시 프로젝트, gym 은 읽기만)."""

    def setUp(self):
        self.name = temp_name("v1")
        self.d = P.PROJECTS / self.name
        src = P.PROJECTS / "test_vlog"
        if not (src / "meta.json").exists():
            self.skipTest("projects/test_vlog 가 없어요")
        self.d.mkdir()
        self.addCleanup(shutil.rmtree, self.d, True)
        for f in ("meta.json", "edit.json", "transcript.json", "silences.json", "peaks.json"):
            shutil.copy(src / f, self.d / f)
        media = self.d / "_media"  # v1 미리보기·소리는 심볼릭 링크로 (대상은 APFS 복제본이라 test_vlog 는 그대로)
        media.mkdir()
        for f in ("preview.mp4", "audio.wav"):
            if subprocess.run(["cp", "-c", str(src / f), str(media / f)]).returncode:
                shutil.copy(src / f, media / f)
            (self.d / f).symlink_to(media / f)
        (self.d / "source.mp4").symlink_to(SAMPLE)
        review = {"toggles": {"c2": True, "c3": False}, "cut_times": {"c4": [19.5, 22.0]},
                  "cut_meta": {"c5": {"decision": "accepted", "reason": "웃음 정리"}},
                  "new_cuts": [{"id": "u1", "start": 30, "end": 31, "type": "user", "reason": "", "enabled": True},
                               {"id": "k1", "start": 0.5, "end": 0.9, "type": "chunk", "reason": "흐름상 제외", "enabled": True}],
                  "sub_edits": {"s2": "투어 브이로그예요"}, "sub_times": {"s5": [11.8, 14.5]},
                  "sub_styles": {"s7": {"x": 30, "y": 20, "size": 6}},
                  "new_subs": [{"id": "n1", "start": 33.6, "end": 34.0, "text": "끝", "style": {"x": 60, "y": 10}}],
                  "deleted_subs": ["s3"], "comments": [{"t": 12.3, "te": 5.1, "text": "여기 살려줘"}], "split_points": [3]}
        (self.d / "review.json").write_text(json.dumps(review, ensure_ascii=False))

    def test_migrate_temp_copy(self):
        v1 = {f: digest(self.d / f) for f in ("meta.json", "edit.json", "review.json", "transcript.json",
                                              "silences.json", "peaks.json", "preview.mp4", "audio.wav")}
        with fake_subs(None):
            self.assertTrue(P.migrate_v1(self.name))
            self.assertFalse(P.migrate_v1(self.name))
        p = P.load(self.name)
        self.assertEqual((p["format"], p["rev"], p["title"], p["canvas"]), (2, 1, self.name, {"w": 1280, "h": 720, "fps": 30}))
        s = p["sources"][0]
        self.assertEqual((s["id"], s["path"], s["duration"], s["status"], s["has_audio"]),
                         ("s1", str(SAMPLE), 34.033, "ready", True))
        cuts = {c["id"]: c for c in p["cuts"]}
        self.assertEqual(len(cuts), 8)
        self.assertEqual({k: (c["enabled"], c["reviewed"]) for k, c in cuts.items() if c["reviewed"]},
                         {"c2": (True, True), "c3": (False, True), "c5": (True, True), "u1": (True, True)})
        self.assertEqual((cuts["c4"]["start"], cuts["c4"]["end"]), (19.5, 22.0))
        self.assertEqual((cuts["k1"]["type"], cuts["k1"]["by"], cuts["k1"]["reason"]), ("manual", "ui", "흐름상 제외"))
        self.assertEqual((cuts["c1"]["by"], cuts["c6"]["by"], cuts["u1"]["by"], cuts["c5"]["reason"]),
                         ("auto", "cli", "ui", "웃음 정리"))
        subs = {t["id"]: t for t in p["subtitles"]}
        self.assertEqual(len(subs), 14)
        self.assertNotIn("s3", subs)
        self.assertEqual((subs["s2"]["text"], subs["s2"]["by"], subs["s1"]["by"]), ("투어 브이로그예요", "ui", "auto"))
        self.assertEqual((subs["s1"]["speech"], subs["s5"]["speech"], subs["n1"]["speech"]),
                         ([0.0, 2.66], [11.94, 14.5], [33.6, 33.8]))
        self.assertEqual((subs["s5"]["start"], subs["s5"]["end"], subs["s5"]["locked"]), (11.8, 14.5, False))
        self.assertEqual((subs["s7"]["pos"], subs["n1"]["pos"], subs["s1"]["pos"]), ({"x": 30.0, "y": 20.0}, {"x": 60.0, "y": 10.0}, None))
        self.assertEqual([(m["t"], m["text"]) for m in p["comments"]], [(12.3, "여기 살려줘")])
        self.assertEqual(p["splits"], [3.0])
        self.assertTrue(p["notes"].startswith("무음·필러 자동 컷"))
        sd = self.d / "sources" / "s1"
        for name, v1name in (("proxy.mp4", "preview.mp4"), ("audio.wav", "audio.wav"), ("transcript.json", "transcript.json"),
                             ("silences.json", "silences.json"), ("peaks.json", "peaks.json")):
            self.assertEqual(os.stat(sd / name).st_ino, os.stat(self.d / v1name).st_ino, name)
        self.assertEqual(json.loads((sd / "probe.json").read_text())["duration"], 34.033)
        self.assertEqual(v1, {f: digest(self.d / f) for f in v1})
        self.assertTrue((self.d / "preview.mp4").is_symlink())
        self.assertNotIn("legacy", {r["name"]: r for r in P.list_projects()}[self.name])

    def test_gym_dry_run_reads_only(self):
        """gym 은 메모리에서만 변환해요: 컷 116개, 자막 596줄, 폴더는 그대로."""
        gym = P.PROJECTS / "gym"
        if not (gym / "meta.json").exists() or (gym / "project.json").exists():
            self.skipTest("v1 gym 프로젝트가 없어요")
        before = tree(gym)
        p, info = P._convert_v1(gym)
        self.assertEqual(tree(gym), before)
        self.assertEqual((len(p["cuts"]), len(p["subtitles"])), (116, 596))
        self.assertEqual(sum(c["type"] == "manual" for c in p["cuts"]), 19)
        self.assertFalse(any(c["type"] == "chunk" for c in p["cuts"]))
        self.assertEqual(sum(t["check"] for t in p["subtitles"]), 96)
        self.assertEqual((p["canvas"], info["duration"]), ({"w": 2688, "h": 1512, "fps": 60}, 2260.523))
        self.assertEqual(p["settings"]["asr"]["model"], "mlx-community/whisper-large-v3-mlx")
        tot = P.total(p)
        for t in p["subtitles"]:
            self.assertTrue(0 <= t["start"] < t["end"] <= tot and 0 <= t["speech"][0] <= t["speech"][1] <= tot, t)
        self.assertAlmostEqual(P.edited_duration(p), 406.75, delta=0.5)
        c1 = next(c for c in p["cuts"] if c["id"] == "c1")
        self.assertEqual((c1["start"], c1["end"], c1["type"]), (0.0, 81.52, "manual"))


if __name__ == "__main__":
    unittest.main()

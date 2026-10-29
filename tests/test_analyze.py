"""소스 분석: 무음·음량, 자동 컷 규칙, 결정 이어받기, 단어 합치기, 실제 영상 끝까지 분석을 확인해요."""
import json
import math
import shutil
import subprocess
import tempfile
import unittest
import uuid
import wave
from collections import Counter
from pathlib import Path
from unittest import mock

import numpy as np

from editvdo import analyze as A, project as P, subs as S

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "test_vlog.mp4"
V1 = ROOT / "projects" / "test_vlog"
GYM = ROOT / "projects" / "gym"
CS = P.DEFAULT_SETTINGS["cut"]
TURBO = Path.home() / ".cache" / "huggingface" / "hub" / "models--mlx-community--whisper-large-v3-turbo"


def temp_name(tag):
    return f"_t_engines_{tag}_{uuid.uuid4().hex[:6]}"


def drop(name):
    if name.startswith("_t_engines_"):
        shutil.rmtree(P.PROJECTS / name, ignore_errors=True)


def islands_to_fine(isl, dur):
    out, t = [], 0.0
    for a, b in isl:
        if a > t:
            out.append((t, a))
        t = b
    if t < dur:
        out.append((t, dur))
    return out


def db_for(dur, levels, base=-90.0):
    """[(a, b, dB)] → 20 ms 음량 배열."""
    db = np.full(int(math.ceil(dur / A.FRAME)), base, np.float32)
    for a, b, v in levels:
        db[int(a / A.FRAME):int(b / A.FRAME)] = v
    return db


def word(a, b, text):
    return {"start": a, "end": b, "word": text}


def spans(cuts):
    return [(c["start"], c["end"], c["type"], c["enabled"]) for c in cuts]


class SourceCuts(unittest.TestCase):
    """한 소스의 자동 컷 규칙 (소스 안 시간)."""

    def test_audio_only_pauses(self):
        fine = [(0, 1.0), (3.0, 3.3), (5.0, 6.0), (8.0, 11.0), (12.0, 12.52), (19.5, 20)]
        got = spans(A.source_cuts(20.0, fine, None, CS))
        self.assertEqual(got, [(0.0, 0.85, "silence", True),     # 앞 무음: 말 앞 여유 0.15 만 남겨요
                               (5.25, 5.85, "silence", True),    # 말 뒤 0.25 · 말 앞 0.15 여유 (0.3초 쉼은 안 잘라요)
                               (8.25, 10.85, "long", True),      # 2초 넘으면 long (0.52초 쉼은 여유 빼면 0.12초라 빼요)
                               (19.75, 20.0, "silence", True)])  # 뒤 무음: 말 뒤 여유 0.25
        self.assertEqual(A.source_cuts(20.0, [(0, 20)], None, CS), [])  # 통째로 조용한 클립
        self.assertEqual(A.source_cuts(20.0, [], None, CS), [])
        wide = {**CS, "pad_before": 0.0, "pad_after": 0.0, "min_silence": 0.25, "long_silence": 5}
        self.assertIn((3.0, 3.3, "silence", True), spans(A.source_cuts(20.0, fine, None, wide)))
        self.assertIn((8.0, 11.0, "silence", True), spans(A.source_cuts(20.0, fine, None, wide)))

    def test_fillers(self):
        isl = [(1.0, 3.0), (3.6, 3.9), (4.6, 6.0)]
        ws = [word(1.0, 2.0, "안녕하세요"), word(2.0, 3.0, "여러분"), word(3.55, 3.95, "음..."), word(4.6, 5.5, "오늘은")]
        got = A.source_cuts(20.0, islands_to_fine(isl, 20.0), ws, CS, db_for(20, [(1, 6, -20)]))
        self.assertEqual(spans(got), [(0.0, 0.85, "silence", True), (3.25, 3.45, "silence", True),
                                      (3.45, 4.15, "filler", True), (4.15, 4.45, "silence", True),
                                      (6.25, 20.0, "silence", True)])
        self.assertEqual(got[2]["reason"], '필러 "음"')
        off = A.source_cuts(20.0, islands_to_fine(isl, 20.0), ws, {**CS, "fillers": False}, None)
        self.assertEqual([c["enabled"] for c in off if c["type"] == "filler"], [False])
        # 내용 단어가 든 덩어리의 '음' 은 필러로 자르지 않아요
        mixed = [word(1.0, 2.0, "음"), word(2.0, 3.0, "여러분")]
        self.assertFalse(any(c["type"] == "filler" for c in A.source_cuts(20.0, islands_to_fine(isl, 20.0), mixed, CS)))

    def test_quiet_words_are_not_cut(self):
        """조용해서 무음으로 잡혀도 인식된 말(멀리서 작게 한 말)은 자르지 않아요."""
        fine = [(0.0, 1.0), (3.0, 9.0)]
        ws = [word(1.0, 3.0, "안녕하세요"), word(5.0, 6.0, "오케이"), word(9.0, 10.0, "반가워요")]
        got = A.source_cuts(10.0, fine, ws, CS, None)
        self.assertTrue(got)
        self.assertFalse([c for c in got if c["enabled"] and c["start"] < 5.75 and c["end"] > 5.25])

    def stretch_case(self, other_db, other):
        """말 [1,4] … 말 없는 소리 … 말 [20,23], 끝에 말 없는 소리 [24,30]."""
        isl = [(1, 4)] + other + [(20, 23), (24, 30)]
        ws = [word(1.2, 3.8, "안녕하세요"), word(20.2, 22.8, "반가워요")]
        db = db_for(30, [(1, 4, -20), (20, 23, -20)] + [(a, b, other_db) for a, b in other + [(24, 30)]])
        return A.source_cuts(30.0, islands_to_fine(isl, 30.0), ws, CS, db)

    def test_ambient_gap(self):
        got = spans(self.stretch_case(-25, [(5, 10), (10.5, 15)]))
        self.assertEqual(got, [(0.0, 0.85, "silence", True),
                               (4.25, 4.85, "silence", True),     # 말 → 현장음 사이 쉼은 잘라요
                               (4.85, 15.25, "gap", False),       # 현장음 = B-roll 후보 (안 자름, 안쪽 쉼도 그대로)
                               (15.25, 19.85, "long", True),
                               (23.25, 23.85, "silence", True),
                               (23.85, 30.0, "gap", False)])
        quiet_but_busy = self.stretch_case(-45, [(5, 10), (10.5, 15)])
        self.assertIn((4.85, 15.25, "gap", False), spans(quiet_but_busy))  # 조용해도 소리가 많으면 제안만
        self.assertNotIn("현장음", next(c for c in quiet_but_busy if c["type"] == "gap")["reason"])

    def test_quiet_stretch_is_cut_whole(self):
        got = spans(self.stretch_case(-45, [(9, 10)]))  # 대부분 조용하고 작은 소리 하나 (카메라 만지는 소리 등)
        self.assertEqual([c for c in got if 4.0 < c[0] < 20.0], [(4.25, 19.85, "long", True)])

    def test_short_stretch_keeps_reaction(self):
        isl = [(1, 4), (4.8, 5.4), (6.2, 9)]  # 말 사이 0.6초 웃음
        ws = [word(1.2, 3.8, "재밌어요"), word(6.3, 8.8, "그렇죠")]
        got = spans(A.source_cuts(10.0, islands_to_fine(isl, 10.0), ws, CS, db_for(10, [(1, 9, -20)])))
        self.assertEqual(got, [(0.0, 0.85, "silence", True), (4.25, 4.65, "silence", True),
                               (5.65, 6.05, "silence", True), (9.25, 10.0, "silence", True)])

    def test_broll_source(self):
        """말이 하나도 없는 소스(B-roll)는 앞뒤 무음만 잘라요."""
        got = spans(A.source_cuts(13.0, [(0, 1), (5, 6), (12, 13)], [], CS, db_for(13, [(1, 12, -30)])))
        self.assertEqual(got, [(0.0, 0.85, "silence", True), (12.25, 13.0, "silence", True)])

    @unittest.skipUnless((V1 / "silences.json").exists(), "test_vlog v1 자료 없음")
    def test_real_test_vlog(self):
        sil = json.loads((V1 / "silences.json").read_text())
        ws = json.loads((V1 / "transcript.json").read_text())["words"]
        db, thr = A._levels(V1 / "audio.wav")
        self.assertEqual(thr, sil["threshold_db"])
        got = A.source_cuts(34.033, sil["fine"], ws, CS, db)
        self.assertEqual([c["type"] for c in got], ["long", "filler", "silence", "silence", "long", "silence", "filler"])
        self.assertEqual(spans(got)[:3], [(4.19, 6.319, "long", True), (6.319, 6.969, "filler", True),
                                          (6.969, 7.41, "silence", True)])
        self.assertEqual([c["reason"] for c in got if c["type"] == "filler"], ['필러 "음"', '필러 "어"'])

    @unittest.skipUnless((GYM / "transcript.whisper.json").exists(), "gym 자료 없음")
    def test_real_gym(self):
        sil = json.loads((GYM / "silences.json").read_text())
        ws = json.loads((GYM / "transcript.whisper.json").read_text())["words"]
        dur = 2260.523
        got = A.source_cuts(dur, sil["fine"], ws, CS, A._levels(GYM / "audio.wav")[0])
        n = Counter(c["type"] for c in got)
        self.assertTrue(all(0 <= c["start"] < c["end"] <= dur for c in got))
        self.assertEqual(got, sorted(got, key=lambda c: (c["start"], c["end"])))
        self.assertTrue(all(not c["enabled"] for c in got if c["type"] == "gap"))
        self.assertGreater(n["gap"], 5)
        self.assertGreater(n["silence"] + n["long"], 50)
        print(f"\n  gym 자동 컷: {dict(n)}, 켜진 컷 {sum(c['enabled'] for c in got)}, "
              f"잘리는 길이 {sum(c['end'] - c['start'] for c in got if c['enabled']):.0f}초 / {dur:.0f}초")


def write_wav(path, parts, rate=16000):
    """[(초, 진폭)] → 16 kHz 모노 wav (진폭 0 은 아주 작은 잡음)."""
    rnd = np.random.default_rng(0)
    chunks = []
    for sec, amp in parts:
        n = int(sec * rate)
        t = np.arange(n) / rate
        x = amp * np.sin(2 * np.pi * 220 * t) if amp else rnd.normal(0, 0.0003, n)
        chunks.append(x)
    a = (np.clip(np.concatenate(chunks), -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(a.tobytes())


class SilencesAndFilters(unittest.TestCase):

    def test_detect_silences(self):
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "a.wav"
            write_wav(wav, [(1.0, 0.3), (1.0, 0), (0.3, 0.3), (0.2, 0), (1.0, 0.3), (0.6, 0)])
            sil = A.detect_silences(wav, 0.5)
            self.assertTrue(-50 <= sil["threshold_db"] <= -28)
            fine = [(round(a, 1), round(b, 1)) for a, b in sil["fine"]]
            self.assertEqual(fine, [(1.0, 2.0), (2.3, 2.5), (3.5, 4.1)])  # 파일 끝까지 이어지는 무음도 닫아요
            self.assertEqual(len(sil["silences"]), 2)
            db, thr = A._levels(wav)
            self.assertEqual(len(db), 205)
            self.assertGreater(db[10], -15)
            self.assertLess(db[60], -60)

    def test_garbage_and_hallucinations(self):
        self.assertFalse(A.is_garbage("오늘은 LA 카페에서 iced latte 마셨어요"))  # 영어는 그대로
        self.assertTrue(A.is_garbage("ご視聴ありがとうございました"))
        self.assertTrue(A.is_garbage("네 네 네 네 네 네 네 네"))
        self.assertFalse(A.is_garbage("ご視聴ありがとうございました", "ja"))
        ws = [word(0.5, 1.0, "말"), word(3.0, 3.5, "환각"), word(4.8, 5.4, "걸침")]
        self.assertEqual([w["word"] for w in A.drop_words_in_silence(ws, [(2.0, 5.0), (0.0, 0.6)])], ["말", "걸침"])


class OnDisk(unittest.TestCase):
    """임시 프로젝트(projects/_t_engines_*)에서: 단어 합치기, 컷 다시 찾기와 결정 이어받기."""

    @classmethod
    def setUpClass(cls):
        cls.name = P.create("엔진 테스트", [SAMPLE, SAMPLE], name=temp_name("disk"))

    @classmethod
    def tearDownClass(cls):
        drop(cls.name)

    def sd(self, sid):
        return P.project_dir(self.name) / "sources" / sid

    def test_words_merge(self):
        A._write_json(self.sd("s1") / "transcript.json", {"words": [word(1.0, 1.5, "첫"), word(0.2, 0.4, "앞")]})
        A._write_json(self.sd("s2") / "transcript.json", {"words": [word(0.5, 0.9, "둘"), word(40, 41, "밖"),
                                                                   {"start": None, "end": 1, "word": "x"}]})
        try:
            p = P.load(self.name)
            got = A.words(p)
            off = p["sources"][1]["offset"]
            self.assertEqual([(w["word"], w["sid"]) for w in got], [("앞", "s1"), ("첫", "s1"), ("둘", "s2"), ("밖", "s2")])
            self.assertEqual((got[2]["start"], got[3]["start"], got[3]["end"]),
                             (round(off + 0.5, 3), round(off + 34.033, 3), round(off + 34.033, 3)))  # 소스 끝으로 잘라요
        finally:
            for sid in ("s1", "s2"):
                (self.sd(sid) / "transcript.json").unlink(missing_ok=True)

    @unittest.skipUnless((V1 / "silences.json").exists(), "test_vlog v1 자료 없음")
    def test_snap(self):
        shutil.copyfile(V1 / "silences.json", self.sd("s1") / "silences.json")
        try:
            p = P.load(self.name)
            self.assertEqual(A.snap(p, 4.0, "start"), 4.19)    # 쉼 안, 앞말 뒤 0.25 여유
            self.assertEqual(A.snap(p, 6.4, "end"), 6.319)     # 뒷말 앞 0.15 여유
            self.assertEqual(A.snap(p, 3.7, "start"), 4.19)    # 0.35초 안의 쉼으로
            self.assertEqual(A.snap(p, 13.0, "start"), 13.0)   # 가까운 쉼 없음
            self.assertEqual(A.snap(p, 40.0, "end"), 40.0)     # s2 는 분석 전
        finally:
            (self.sd("s1") / "silences.json").unlink(missing_ok=True)

    @unittest.skipUnless((V1 / "silences.json").exists(), "test_vlog v1 자료 없음")
    def test_redetect_keeps_decisions(self):
        for f in ("silences.json", "transcript.json", "audio.wav"):
            shutil.copyfile(V1 / f, self.sd("s1") / f)
        try:
            p, ch = P.transact(self.name, lambda p: A.redetect(p), by="cli")
            auto = [c for c in p["cuts"] if c["by"] == "auto"]
            self.assertEqual(len(auto), 7)
            self.assertTrue(all(A._sid_of(p, c) == "s1" for c in auto))  # s2 는 아직 분석 전이라 그대로
            self.assertEqual(len(ch), 7)
            first = {c["id"]: c for c in auto}
            long_id = next(c["id"] for c in auto if c["start"] == 4.19)
            user = {"id": "cuser1", "start": 30.0, "end": 31.0, "type": "user"}
            P.transact(self.name, lambda p: P.apply_ops(p, [
                {"op": "put", "coll": "cuts", "item": {"id": long_id, "enabled": False, "reviewed": True}},
                {"op": "put", "coll": "cuts", "item": user}], "ui"), by="ui")
            p, ch = P.transact(self.name, lambda p: A.redetect(p, ["s1"]), by="cli")
            self.assertEqual(ch, [])  # 그대로면 바뀌는 게 없어요 (id·rev 유지)
            P.transact(self.name, lambda p: P.apply_ops(p, [{"op": "set", "key": "settings.cut",
                                                               "value": {"pad_after": 0.35}}], "ui"), by="ui")
            p, ch = P.transact(self.name, lambda p: A.redetect(p, ["s1"]), by="cli")
            cuts = {c["id"]: c for c in p["cuts"]}
            self.assertIn("cuser1", cuts)
            lc = cuts[long_id]  # 시작이 0.1초 바뀌어도 같은 컷: id 와 '살림' 결정을 이어받아요
            self.assertEqual((lc["start"], lc["enabled"], lc["reviewed"]), (4.29, False, True))
            self.assertEqual(set(cuts) - {"cuser1"}, set(first))
            self.assertTrue(all(c["coll"] in ("cuts", "subtitles") for c in ch))
        finally:
            for f in ("silences.json", "transcript.json", "audio.wav"):
                (self.sd("s1") / f).unlink(missing_ok=True)
            P.transact(self.name, lambda p: p.update(cuts=[], settings={**p["settings"], "cut": dict(CS)}))


@unittest.skipUnless(SAMPLE.exists(), "샘플 영상 없음")
class EndToEnd(unittest.TestCase):
    """샘플 영상으로 끝까지: 분석(+음성 인식) → 컷·자막 → 대본 가져오기 → 나누기·합치기 → 두 번째 영상 → 미리보기."""

    def setUp(self):
        self.name = P.create("엔진 끝까지", [SAMPLE], name=temp_name("e2e"))

    def tearDown(self):
        drop(self.name)

    def test_full_flow(self):
        whisper = TURBO.exists()
        seen = []
        info = A.analyze_source(self.name, "s1", lambda step, f: seen.append((step, f)), transcribe=whisper)
        p = P.load(self.name)
        sd = P.project_dir(self.name) / "sources" / "s1"
        for f in ("audio.wav", "peaks.json", "thumbs.jpg", "thumbs.json", "silences.json"):
            self.assertTrue((sd / f).exists(), f)
        self.assertEqual(p["sources"][0]["play"], "original")  # 크롬이 여는 mp4 → 변환·이어 붙이기 없이 원본 재생
        self.assertFalse((sd / "proxy.mp4").exists())
        self.assertFalse((P.project_dir(self.name) / "preview.mp4").exists())
        self.assertEqual((p["sources"][0]["status"], p["sources"][0]["progress"], p["sources"][0]["step"]),
                         ("ready", 1.0, ""))
        fr = [f for _, f in seen]
        self.assertEqual(fr, sorted(fr))
        self.assertGreater(fr[-1], 0.9)
        self.assertTrue({"분석"} <= {s for s, _ in seen} and "변환" not in {s for s, _ in seen})
        cuts = [c for c in p["cuts"] if c["by"] == "auto"]
        self.assertGreater(len(cuts), 3)
        print(f"\n  분석: {info}")
        for c in cuts:
            print(f"   ✂ {c['start']:7.2f}~{c['end']:7.2f} {c['type']:8s} {'on ' if c['enabled'] else 'off'} {c['reason']}")
        if not whisper:
            return
        self.assertIn("인식", {s for s, _ in seen})
        self.assertTrue((sd / "transcript.json").exists())
        self.assertGreater(info["subtitles"], 5)
        for t in p["subtitles"]:
            print(f"   T {t['start']:6.2f}~{t['end']:6.2f} speech {t['speech']} {t['text']}")
        self.assertTrue(any("성수" in t["text"] for t in p["subtitles"]))

        srt = ("1\n00:00:00,000 --> 00:00:03,800\n안녕하세요 여러분, 오늘은 성수동 카페 투어 브이로그입니다.\n\n"
               "2\n00:00:11,500 --> 00:00:15,900\n첫 번째로 간 곳은 골목 안쪽에 있는 작은 로스터리 카페예요.\n")
        p, got = P.transact(self.name, lambda p: S.import_script(p, srt), by="ui", snapshot="import")
        self.assertEqual((got["count"], got["unmatched"]), (2, 0))
        clova = ("참석자 1 00:00\n안녕하세요 여러분, 오늘은 성수동 카페 투어 브이로그입니다.\n\n"
                 "참석자 1 00:06\n음, 첫 번째로 간 곳은 어, 그러니까 그 뭐더라.\n\n"
                 "참석자 2 00:16\n여기 라떼가 진짜 고소하고 맛있었어요.\n\n"
                 "참석자 1 00:28\n오늘 영상 재밌게 보셨다면 구독과 좋아요 부탁드려요. 안녕!\n")
        p, got = P.transact(self.name, lambda p: S.import_script(p, clova), by="ui")
        print(f"  클로바 가져오기: {got}")
        self.assertEqual(got["base"], "orig")
        self.assertLessEqual(got["unmatched"], 1)
        self.assertEqual([s["name"] for s in p["speakers"]], ["참석자 1", "참석자 2"])
        for t in p["subtitles"]:
            print(f"   T {t['start']:6.2f}~{t['end']:6.2f} {t['speaker']} {'?' if t['check'] else ' '} {t['text']}")
        late = next(t for t in p["subtitles"] if "라떼" in t["text"])
        self.assertAlmostEqual(late["speech"][0], 16.3, delta=0.6)

        first = min(p["subtitles"], key=lambda t: t["start"])
        mid = round((first["speech"][0] + first["speech"][1]) / 2, 2)
        p, (a, b) = P.transact(self.name, lambda p: S.split_subtitle(p, first["id"], mid), by="ui")
        self.assertTrue(a["text"] and b["text"])
        self.assertEqual((a["text"] + " " + b["text"]).replace(" ", ""), first["text"].replace(" ", ""))
        p, m = P.transact(self.name, lambda p: S.merge_subtitles(p, [a["id"], b["id"]]), by="ui")
        self.assertEqual(m["text"].replace(" ", ""), first["text"].replace(" ", ""))

        n = A.transcribe_source(self.name, "s1")
        self.assertGreater(n, 20)
        self.assertEqual(P.load(self.name)["sources"][0]["status"], "ready")

    def test_failures(self):
        """음성 인식만 실패하면 소스는 준비(소리로만 찾은 컷) + 오류 글, 다른 실패는 status=error 와 예외."""
        class Stop(Exception):
            pass

        def stop(step, f):
            if step == "인식":
                raise Stop()

        def fake(wav, asr, cb):
            cb(0.5)
            return {"segments": []}
        with mock.patch.object(A, "_whisper", side_effect=fake), self.assertRaises(Stop):
            A.analyze_source(self.name, "s1", stop, transcribe=True)  # 진행 알림이 멈추면(취소) 그대로 멈춰요
        self.assertEqual(P.load(self.name)["sources"][0]["status"], "error")
        with mock.patch.object(A, "_whisper", side_effect=RuntimeError("모델을 받을 수 없어요")):
            info = A.analyze_source(self.name, "s1", None, transcribe=True)
        s = P.load(self.name)["sources"][0]
        self.assertEqual((s["status"], info["words"]), ("ready", None))
        self.assertIn("음성 인식에 실패했어요", s["error"])
        self.assertEqual(info["warnings"], [s["error"]])
        self.assertGreater(info["cuts"], 3)
        P.transact(self.name, lambda p: p["sources"][0].update(path="/nope/없는파일.mov"))
        with self.assertRaises(ValueError):
            A.analyze_source(self.name, "s1", None, transcribe=False)
        s = P.load(self.name)["sources"][0]
        self.assertEqual(s["status"], "error")
        self.assertIn("없는파일", s["error"])
        with self.assertRaises(ValueError):
            A.analyze_source(self.name, "s9")

    def test_second_clip_and_preview(self):  # 크롬이 못 여는 형식처럼 변환본으로 재생하는 길
        P.transact(self.name, lambda p: p["sources"][0].update(play="proxy"))
        A.analyze_source(self.name, "s1", None, transcribe=False)
        p, sids = P.transact(self.name, lambda p: P.add_sources(p, [SAMPLE]))
        self.assertEqual(sids, ["s2"])
        P.transact(self.name, lambda p: p["sources"][1].update(play="proxy"))
        A.analyze_source(self.name, "s2", None, transcribe=False)
        p = P.load(self.name)
        self.assertEqual([s["status"] for s in p["sources"]], ["ready", "ready"])
        self.assertAlmostEqual(probe_duration(P.project_dir(self.name) / "preview.mp4"), P.total(p), delta=0.1)
        self.assertTrue(any(A._sid_of(p, c) == "s2" for c in p["cuts"]))
        p, _ = P.transact(self.name, lambda p: P.reorder_sources(p, ["s2", "s1"]))
        self.assertTrue(A.rebuild_preview(self.name))


@unittest.skipUnless((V1 / "meta.json").exists() and SAMPLE.exists(), "test_vlog v1 자료 없음")
class MigratedProject(unittest.TestCase):
    """v1 에서 옮긴 프로젝트에 영상을 더하면 형식이 다른 s1 미리보기를 다시 만들어서 이어 붙여요."""

    def setUp(self):
        self.name = temp_name("v1")
        d = P.PROJECTS / self.name
        d.mkdir()
        for f in ("meta.json", "edit.json", "preview.mp4", "audio.wav", "peaks.json", "silences.json", "transcript.json"):
            if (V1 / f).exists():
                shutil.copyfile(V1 / f, d / f)
        self.assertTrue(P.migrate_v1(self.name))

    def tearDown(self):
        drop(self.name)

    def test_add_clip_to_migrated(self):
        sd = P.project_dir(self.name) / "sources" / "s1"
        self.assertFalse(A._proxy_ok(sd / "proxy.mp4", P.load(self.name)["canvas"]))
        P.transact(self.name, lambda p: P.add_sources(p, [SAMPLE]))
        A.analyze_source(self.name, "s2", None, transcribe=False)
        p = P.load(self.name)
        self.assertTrue(A._proxy_ok(sd / "proxy.mp4", p["canvas"]))
        self.assertAlmostEqual(probe_duration(P.project_dir(self.name) / "preview.mp4"), P.total(p), delta=0.1)
        self.assertTrue((V1 / "preview.mp4").exists())


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout
    return float(out.strip())


if __name__ == "__main__":
    unittest.main()

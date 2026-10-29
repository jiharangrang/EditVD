"""자막 엔진: 줄 나누기, 자동 타이밍, 대본 읽기(여러 형식), 대본↔음성 맞추기, 나누기·합치기를 확인해요."""
import copy
import json
import random
import time
import unittest
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from editvdo import analyze, project as P, subs as S

ROOT = Path(__file__).resolve().parent.parent
GYM = ROOT / "projects" / "gym"


def mem(durs=(60.0,), fps=30, **colls):
    """디스크 없이 쓰는 프로젝트 (rev 4)."""
    p = {"rev": 4, "canvas": {"w": 1920, "h": 1080, "fps": fps},
         "sources": [{"id": f"s{i}", "path": f"/clips/{i}.mp4", "duration": d} for i, d in enumerate(durs, 1)]}
    p.update(copy.deepcopy(colls))
    return P._fill(p, "_t_mem")


def cut(cid, s, e, on=True, typ="silence", **kw):
    return {"id": cid, "start": s, "end": e, "type": typ, "enabled": on, "reviewed": False, "reason": "",
            "by": "auto", "rev": 1, **kw}


def sub(tid, sp0, sp1, text="자막", **kw):
    return {"id": tid, "start": sp0, "end": sp1, "text": text, "speech": [sp0, sp1], "speaker": None, "pos": None,
            "locked": False, "check": False, "by": "auto", "rev": 1, **kw}


def w(a, b, word, sid="s1", **kw):
    return {"start": a, "end": b, "word": word, "sid": sid, **kw}


@contextmanager
def fake_words(ws):
    with mock.patch.object(analyze, "words", lambda p: [dict(x) for x in ws]):
        yield


def speech_words(text, t0=1.0, gap=0.08, char=0.17, pause_after=()):
    """글 → 그럴듯한 인식 단어 (글자당 0.17초, pause_after 단어 뒤에 1초 쉼)."""
    out, t = [], t0
    for i, tok in enumerate(text.split()):
        d = len(S._keys(tok)) * char
        out.append(w(round(t, 3), round(t + d, 3), tok))
        t += d + gap + (1.0 if i in pause_after else 0.0)
    return out


def by_id(p):
    return {t["id"]: t for t in p["subtitles"]}


def assert_valid(tc, p):
    """자막이 모두 올바르고 같은 화자 안에서는 (컷에 숨은 줄 말고) 겹치지 않아요."""
    tot, ax = P.total(p), S._Axis(p)
    groups = defaultdict(list)
    for t in p["subtitles"]:
        tc.assertTrue(0 <= t["start"] < t["end"] <= tot + 1e-9, t)
        tc.assertTrue(0 <= t["speech"][0] <= t["speech"][1] <= tot + 1e-9, t)
        if not ax.hidden(*t["speech"]):
            groups[t["speaker"]].append(t)
    for g in groups.values():
        g.sort(key=lambda t: t["start"])
        for a, b in zip(g, g[1:]):
            tc.assertLessEqual(a["end"], b["start"] + 1e-6, (a, b))


SRT = """﻿1
00:00:01,000 --> 00:00:03,200
<i>안녕하세요</i> 여러분
오늘은 성수동이에요

2
00:00:04.500 --> 00:00:06,000
화자1: 첫 번째 카페

00:00:07,000 --> 00:00:08,000
[화자2] 두 번째 &amp; 마지막
3
00:00:09,000 --> 00:00:10,500
[웃음] 진짜요?
"""

VTT = """WEBVTT
Kind: captions

NOTE 메모 블록은
무시해요

intro
00:01.000 --> 00:03.200 align:start position:10%
<v 민수>안녕하세요</v>

00:00:04.000 --> 00:00:05.000
두 번째 줄
"""

TIMED = """오늘 촬영 메모 (시간 없는 머리말은 버려요)
- 00:12 화자1: 첫 줄이에요
  이어지는 말
[00:15] [화자2]: 둘째 줄
1:02:03.5 참석자 1: 한 시간 넘은 줄
00:20 ~ 00:24 범위가 있는 줄
00:25 - 00:27 대시 범위
00:28-00:30 붙은 범위
0:31 짧은 시간
00:33.25 소수 초
"""

CLOVA = """성수 브이로그 회의
2026.10.16 목 오후 12:36 ・ 30분 0초
참석자 1 참석자 2

참석자 1 00:00
안녕하세요 오늘은 성수동에 왔어요
카페 투어를 할 거예요

참석자 2 00:12
네 반가워요

민수 01:02:03
마지막 인사
"""

SECTIONS = """[00:06 ~ 03:00] 헬스장 도착

  - 00:06 [남]: 전 뭐... 언더그립...
  - 00:08 [여]: 언더그립이요? (웃음)
  - 00:10 [남]: 네.

[03:16 ~ 05:45] 1세트
  - 03:17 [남]: 이게 호흡 때문인데...
"""


class ParseScript(unittest.TestCase):
    """대본 형식을 너그럽게 읽어요."""

    def test_srt(self):
        es = S.parse_script(SRT)
        self.assertEqual([(e["start"], e.get("end")) for e in es], [(1.0, 3.2), (4.5, 6.0), (7.0, 8.0), (9.0, 10.5)])
        self.assertEqual(es[0]["text"], "안녕하세요 여러분\n오늘은 성수동이에요")
        self.assertEqual((es[1]["text"], es[1]["speaker"]), ("첫 번째 카페", "화자1"))
        self.assertEqual((es[2]["text"], es[2]["speaker"]), ("두 번째 & 마지막", "화자2"))
        self.assertEqual((es[3]["text"], es[3].get("speaker")), ("[웃음] 진짜요?", None))  # 효과음 표시는 화자가 아니에요
        self.assertTrue(all(e["cue"] for e in es))

    def test_vtt(self):
        es = S.parse_script(VTT)
        self.assertEqual([(e["start"], e["end"], e["text"]) for e in es],
                         [(1.0, 3.2, "안녕하세요"), (4.0, 5.0, "두 번째 줄")])
        self.assertEqual(es[0]["speaker"], "민수")

    def test_timed_lines(self):
        es = S.parse_script(TIMED)
        got = [(e["start"], e.get("end"), e.get("speaker"), e["text"]) for e in es]
        self.assertEqual(got, [
            (12.0, None, "화자1", "첫 줄이에요\n이어지는 말"),
            (15.0, None, "화자2", "둘째 줄"),
            (3723.5, None, "참석자 1", "한 시간 넘은 줄"),
            (20.0, 24.0, None, "범위가 있는 줄"),
            (25.0, 27.0, None, "대시 범위"),
            (28.0, 30.0, None, "붙은 범위"),
            (31.0, None, None, "짧은 시간"),
            (33.25, None, None, "소수 초")])

    def test_clova_note(self):
        es = S.parse_script(CLOVA)
        self.assertEqual([(e["start"], e["speaker"], e["text"]) for e in es], [
            (0.0, "참석자 1", "안녕하세요 오늘은 성수동에 왔어요\n카페 투어를 할 거예요"),
            (12.0, "참석자 2", "네 반가워요"),
            (3723.0, "민수", "마지막 인사")])

    def test_section_headers_are_dropped(self):
        es = S.parse_script(SECTIONS)
        self.assertEqual([(e["start"], e["speaker"], e["text"]) for e in es], [
            (6.0, "남", "전 뭐... 언더그립..."), (8.0, "여", "언더그립이요? (웃음)"), (10.0, "남", "네."),
            (197.0, "남", "이게 호흡 때문인데...")])
        # 하위 줄이 시간 없는 글이면 머리줄이 아니라 범위 자막이에요
        es = S.parse_script("[00:06 ~ 00:09] 헬스장 도착\n오늘은 등 운동")
        self.assertEqual(es, [{"text": "헬스장 도착\n오늘은 등 운동", "start": 6.0, "end": 9.0}])

    def test_plain_and_speaker_names(self):
        es = S.parse_script("안녕하세요\n\n민수: 반가워요\n지수: 네\n민수: 가요\n주의: 뜨거워요\n")
        self.assertEqual([(e.get("speaker"), e["text"]) for e in es],  # 한 번만 나온 '이름:' 은 그대로 글이에요
                         [(None, "안녕하세요"), ("민수", "반가워요"), (None, "지수: 네"), ("민수", "가요"),
                          (None, "주의: 뜨거워요")])
        self.assertTrue(all("start" not in e for e in es))

    def test_errors_only_when_nothing_usable(self):
        for bad in ("", "   \n\n", "1\n00:00:01,000 --> 00:00:02,000\n\n", "00:12\n00:15\n"):
            with self.assertRaises(ValueError):
                S.parse_script(bad)
        with self.assertRaises(ValueError):
            S.parse_script(None)
        self.assertEqual(S.parse_script("00:12\n이어지는 글")[0], {"text": "이어지는 글", "start": 12.0})

    def test_bad_times_are_text(self):
        es = S.parse_script("00:75 이상한 시간\n12:30에 만나요")
        self.assertEqual([e["text"] for e in es], ["00:75 이상한 시간", "12:30에 만나요"])

    @unittest.skipUnless((GYM / "transcript_ext.txt").exists(), "gym 대본 없음")
    def test_gym_transcript_ext(self):
        es = S.parse_script((GYM / "transcript_ext.txt").read_text(encoding="utf-8"))
        self.assertEqual(len(es), 207)  # 구간 머리줄은 빠져요
        self.assertEqual({e.get("speaker") for e in es}, {"남", "여", "카메라"})
        self.assertEqual((es[0]["start"], es[0]["text"]), (6.0, "전 뭐... 언더그립..."))
        self.assertTrue(all(e["start"] is not None and e["text"] for e in es))
        self.assertEqual([e["start"] for e in es], sorted(e["start"] for e in es))

    @unittest.skipUnless((GYM / "script.txt").exists(), "gym 대본 없음")
    def test_gym_script(self):
        es = S.parse_script((GYM / "script.txt").read_text(encoding="utf-8"))
        self.assertGreater(len(es), 500)
        self.assertEqual((es[0]["start"], es[0]["text"]), (6.06, "정답. 언더그립. 언더그립?"))
        self.assertTrue(all(e["text"] for e in es))


class BuildLines(unittest.TestCase):
    """단어 → 자막 줄 규칙."""

    def lines(self, ws, p=None, **kw):
        p = p or mem()
        return [t["text"] for t in S.build_lines(ws, p, **kw)]

    def test_breaks(self):
        ws = [w(0.0, 0.4, "안녕하세요"), w(0.5, 0.9, "여러분."),           # 문장 끝
              w(1.0, 1.3, "오늘은"), w(2.0, 2.4, "성수동"),               # 0.7초 쉼
              w(2.5, 2.8, "카페"), w(2.9, 3.2, "투어", sid="s2")]          # 소스 경계
        self.assertEqual(self.lines(ws), ["안녕하세요 여러분", "오늘은", "성수동 카페", "투어"])

    def test_max_chars_and_comma(self):
        p = mem()
        p["settings"]["subs"]["max_chars"] = 12
        ws = speech_words("하나둘셋넷 다섯여섯, 일곱여덟아홉 열열열열열열 끝")
        self.assertEqual(self.lines(ws, p), ["하나둘셋넷 다섯여섯", "일곱여덟아홉 열열열열열열", "끝"])
        p["settings"]["subs"]["max_chars"] = 18
        long = speech_words(" ".join(["가나다"] * 12))
        out = self.lines(long, p)
        self.assertTrue(all(S._len(x) <= 18 for x in out), out)

    def test_long_cut_and_duration(self):
        ws = [w(0.5, 0.9, "앞말"), w(1.0, 1.1, "그"), w(1.5, 1.9, "뒷말")]  # 쉼은 0.4초뿐
        self.assertEqual(self.lines(ws, mem(cuts=[cut("c", 1.12, 2.3)])), ["앞말 그", "뒷말"])
        self.assertEqual(self.lines(ws, mem(cuts=[cut("c", 1.12, 1.45)])), ["앞말 그 뒷말"])  # 1초 이하 컷은 그대로
        slow = [w(i * 0.45, i * 0.45 + 0.4, "아") for i in range(14)]
        self.assertGreater(len(self.lines(slow)), 1)  # 한 줄 4.5초까지

    def test_items(self):
        p = mem(subtitles=[sub("t000001", 0, 1)])
        out = S.build_lines([w(1.0, 1.5, "확인", guess=True), w(1.6, 2.0, "할게요...", guess=False)], p)
        self.assertEqual(len(out), 1)
        t = out[0]
        self.assertEqual((t["text"], t["speech"], t["check"], t["by"], t["rev"], t["locked"]),
                         ("확인 할게요", [1.0, 2.0], True, "auto", 5, False))
        self.assertNotEqual(t["id"], "t000001")
        self.assertEqual(S.build_lines([w(1.0, 1.5, "...")], p), [])
        self.assertEqual(self.lines([w(0, 1, "가", sid="s1"), w(1, 2, "나", sid="s2")], mem((5.0, 5.0)), sids=["s2"]), ["나"])

    @unittest.skipUnless((GYM / "transcript.whisper.json").exists(), "gym 인식 결과 없음")
    def test_gym_words(self):
        ws = [{**x, "sid": "s1"} for x in json.loads((GYM / "transcript.whisper.json").read_text())["words"]]
        p = mem((2260.523,))
        out = S.build_lines(ws, p)
        self.assertGreater(len(out), 400)
        self.assertTrue(all(S._len(t["text"]) <= 18 or " " not in t["text"] for t in out))
        self.assertEqual([t["speech"][0] for t in out], sorted(t["speech"][0] for t in out))
        p["subtitles"] = out
        S.retime(p)
        assert_valid(self, p)


class Retime(unittest.TestCase):
    """자동 타이밍: 편집본 시간축, 화자별, lead/hold/min/bridge, 결정적."""

    def setUp(self):
        self.p = mem()
        self.p["settings"]["subs"].update(lead=0.1, hold=0.3, min=0.8, bridge=0.3)

    def run_retime(self, *subs_):
        self.p["subtitles"] = [copy.deepcopy(t) for t in subs_]
        changed = S.retime(self.p)
        return changed, {t["id"]: (t["start"], t["end"]) for t in self.p["subtitles"]}

    def test_basic_rules(self):
        _, got = self.run_retime(sub("a", 1.0, 2.0), sub("b", 2.4, 2.5), sub("c", 3.6, 5.0), sub("d", 10.0, 11.0))
        self.assertEqual(got["a"], (0.9, 2.3))     # lead 0.1·hold 0.3, 다음 줄 시작(2.3)과 딱 붙어요
        self.assertEqual(got["b"], (2.3, 3.1))     # 최소 0.8초, 다음 줄(3.5)과 0.4초라 bridge(0.3) 밖
        self.assertEqual(got["c"], (3.5, 5.3))
        self.assertEqual(got["d"], (9.9, 11.3))

    def test_bridge_and_cap(self):
        _, got = self.run_retime(sub("a", 1.0, 2.0), sub("b", 2.6, 3.5))
        self.assertEqual(got["a"], (0.9, 2.5))     # 2.3 → 다음 줄(2.5)과 0.2초 → 이어 붙여요
        _, got = self.run_retime(sub("a", 1.0, 2.0), sub("b", 2.1, 3.0))
        self.assertEqual((got["a"], got["b"]), ((0.9, 2.0), (2.0, 3.3)))  # 다음 줄 시작에서 잘라요

    def test_speakers_are_independent(self):
        a = sub("a", 1.0, 3.0, speaker="A")
        b = sub("b", 1.5, 2.0, speaker="B")
        c = sub("c", 2.0, 4.0, speaker="A")
        _, got = self.run_retime(a, b, c)
        self.assertEqual(got["b"], (1.4, 2.3))     # 다른 화자와는 겹쳐도 돼요
        self.assertEqual(got["a"], (0.9, 1.9))     # 같은 화자 다음 줄 시작에서 끊겨요
        self.assertEqual(got["c"], (1.9, 4.3))

    def test_cuts_edited_axis(self):
        self.p["cuts"] = [cut("x", 2.0, 4.0), cut("h", 6.0, 9.0), cut("off", 20, 25, on=False)]
        a = sub("a", 4.05, 5.0)      # 말 바로 앞이 컷 → lead 는 남는 조각 시작(4.0)까지만
        b = sub("b", 10.0, 11.4)     # hold 가 컷(12~13) 너머 편집본에서 이어져요
        h = sub("h", 6.5, 8.0)       # 말소리가 통째로 컷 안 → 그대로
        h["start"], h["end"] = 6.4, 8.3
        self.p["cuts"].append(cut("y", 11.5, 13.0))
        _, got = self.run_retime(a, b, h)
        self.assertEqual(got["a"], (4.0, 5.3))
        self.assertEqual(got["b"], (9.9, 13.2))    # 11.4+0.3 → 편집본으로 0.2 넘어가 원본 13.2
        self.assertEqual(got["h"], (6.4, 8.3))
        self.assertAlmostEqual(P.to_edited(self.p, 13.2) - P.to_edited(self.p, 11.4), 0.3, places=6)

    def test_locked_lines_block_neighbours(self):
        lk = sub("lk", 3.0, 3.5, locked=True)
        lk["start"], lk["end"] = 2.0, 4.0
        _, got = self.run_retime(sub("a", 1.0, 1.95), lk, sub("b", 3.8, 4.5))
        self.assertEqual(got["lk"], (2.0, 4.0))
        self.assertEqual(got["a"], (0.9, 2.0))     # 잠긴 줄 시작에서 멈춰요
        self.assertEqual(got["b"], (4.0, 4.8))     # 잠긴 줄 끝 뒤에서 시작해요

    def test_deterministic_and_changed_ids(self):
        rnd = random.Random(5)
        items = []
        t = 0.5
        for i in range(200):
            d = rnd.uniform(0.2, 3)
            items.append(sub(f"r{i}", round(t, 3), round(t + d, 3), speaker=rnd.choice([None, "A", "B"])))
            t += d + rnd.uniform(0, 1.2)
        self.p["sources"][0]["duration"] = round(t + 5, 3)
        self.p["cuts"] = [cut(f"k{i}", round(x, 3), round(x + rnd.uniform(0.2, 2), 3)) for i, x in
                          enumerate(sorted(rnd.uniform(0, t) for _ in range(60)))]
        changed, first = self.run_retime(*items)
        self.assertEqual(set(changed), {x["id"] for x in items if first[x["id"]] != (x["start"], x["end"])})
        self.assertEqual(S.retime(self.p), [])     # 두 번째는 바뀌는 게 없어요
        ax = S._Axis(self.p)
        shuffled = items[:]
        rnd.shuffle(shuffled)
        for x in shuffled:  # 다른 시간에서 시작해도 (숨은 줄 말고) 같은 결과
            if not ax.hidden(*x["speech"]):
                x["start"], x["end"] = first[x["id"]][0], round(first[x["id"]][1] + 1, 3)
        _, again = self.run_retime(*shuffled)
        self.assertEqual(again, first)
        assert_valid(self, self.p)

    def test_axis_matches_project(self):
        rnd = random.Random(9)
        p = mem((40.0, 25.5, 33.3), cuts=[cut(f"c{i}", s, round(s + rnd.uniform(0.01, 2), 3), on=rnd.random() < 0.8)
                                          for i, s in enumerate(round(rnd.uniform(0, 98), 3) for _ in range(150))])
        ax = S._Axis(p)
        for _ in range(500):
            t, e = rnd.uniform(-1, 100), rnd.uniform(-1, 80)
            self.assertEqual(ax.E(t), P.to_edited(p, t))
            self.assertEqual(ax.O(e), P.to_original(p, e))

    def test_speed(self):
        rnd = random.Random(1)
        p = mem((1200.0, 1200.0), cuts=[cut(f"k{i}", i * 12 + 1, i * 12 + 1 + rnd.uniform(0.3, 2)) for i in range(200)])
        p["subtitles"] = [sub(f"t{i}", i * 4 + 0.5, i * 4 + 2.5, speaker=rnd.choice([None, "A"])) for i in range(600)]
        t0 = time.perf_counter()
        S.retime(p)
        self.assertLess(time.perf_counter() - t0, 0.1)

    def test_apply_ops_hook(self):
        """타이밍 설정을 바꾸면 apply_ops 가 retime 을 불러 바뀐 자막도 기록에 넣어요."""
        p = mem(subtitles=[sub("a", 1.0, 2.0), sub("b", 5.0, 6.0)])
        S.retime(p)
        changes = P.apply_ops(p, [{"op": "set", "key": "settings.subs", "value": {"hold": 1.0}}], "cli")
        items = {c["item"]["id"]: c["item"] for c in changes if c.get("coll") == "subtitles"}
        self.assertEqual((items["a"]["end"], items["b"]["end"]), (3.0, 7.0))
        changes = P.apply_ops(p, [{"op": "put", "coll": "cuts", "item": {"id": "c1", "start": 2.2, "end": 4.5,
                                                                             "type": "user"}}], "ui")
        self.assertEqual(by_id(p)["a"]["end"], 5.0)  # 컷 너머 편집본에서 1초 → 원본 5.0 (b 시작에서 끊김)
        self.assertIn("a", {c["item"]["id"] for c in changes if c.get("coll") == "subtitles"})


class ImportScript(unittest.TestCase):
    """대본 ↔ 인식 단어 맞추기."""

    TEXT = "안녕하세요 여러분 오늘은 성수동 카페 투어를 해볼게요 첫 번째 카페는 골목 안쪽에 있어요"

    def setUp(self):
        self.ws = speech_words(self.TEXT, t0=2.0, pause_after=(1, 6))
        self.p = mem()

    def test_timed_script(self):
        script = ("00:02 화자1: 안녕하세요 여러분\n"
                  "00:04 화자1: 오늘은 성수동 카페 투어를 해볼게요\n"
                  "00:09 화자2: 전혀 다른 말이 들어간 줄\n"
                  "00:10 화자2: 첫 번째 카페는 골목 안쪽에 있어요\n")
        with fake_words(self.ws):
            info = S.import_script(self.p, script)
        self.assertEqual((info["count"], info["base"], info["unmatched"], info["entries"]), (4, "orig", 1, 4))
        subs_ = sorted(self.p["subtitles"], key=lambda t: t["speech"][0])
        self.assertEqual([t["text"] for t in subs_], ["안녕하세요 여러분", "오늘은 성수동 카페 투어를 해볼게요",
                                                      "전혀 다른 말이 들어간 줄", "첫 번째 카페는 골목 안쪽에 있어요"])
        self.assertEqual([t["check"] for t in subs_], [False, False, True, False])
        self.assertAlmostEqual(subs_[0]["speech"][0], self.ws[0]["start"], places=2)
        self.assertAlmostEqual(subs_[1]["speech"][0], self.ws[2]["start"], places=2)
        self.assertAlmostEqual(subs_[3]["speech"][1], self.ws[-1]["end"], places=2)
        self.assertEqual([(s["id"], s["name"]) for s in self.p["speakers"]], [("A", "화자1"), ("B", "화자2")])
        self.assertEqual([t["speaker"] for t in subs_], ["A", "A", "B", "B"])
        self.assertTrue(all(t["by"] == "ui" and t["rev"] == 5 for t in subs_))
        assert_valid(self, self.p)

    def test_plain_script_and_long_lines(self):
        with fake_words(self.ws):
            info = S.import_script(self.p, self.TEXT.replace(" 첫 번째", "\n첫 번째"))
        self.assertEqual(info["unmatched"], 0)
        texts = [t["text"] for t in sorted(self.p["subtitles"], key=lambda t: t["start"])]
        self.assertEqual(texts, ["안녕하세요 여러분", "오늘은 성수동 카페 투어를 해볼게요", "첫 번째 카페는 골목 안쪽에 있어요"])

    def test_srt_cues_stay_whole(self):
        srt = "1\n00:00:02,000 --> 00:00:03,500\n안녕하세요\n여러분\n\n2\n00:00:04,000 --> 00:00:09,000\n" \
              "오늘은 성수동 카페 투어를 해볼게요 첫 번째 카페는 골목 안쪽에 있어요\n"
        with fake_words(self.ws):
            S.import_script(self.p, srt)
        texts = [t["text"] for t in sorted(self.p["subtitles"], key=lambda t: t["start"])]
        self.assertEqual(texts, ["안녕하세요\n여러분", "오늘은 성수동 카페 투어를 해볼게요 첫 번째 카페는 골목 안쪽에 있어요"])

    def test_edited_time_base(self):
        self.p["cuts"] = [cut("c1", 0.0, 1.5), cut("c2", 3.5, 4.4)]  # 말 앞과 1초 쉼 안
        ws = self.ws
        lines = ["안녕하세요 여러분", "오늘은 성수동 카페 투어를 해볼게요", "첫 번째 카페는 골목 안쪽에 있어요"]
        starts = [next(x["start"] for x in ws if x["word"] == ln.split()[0]) for ln in lines]
        fmt = "{:02d}:{:05.2f}".format
        edited = "\n".join(f"{fmt(*divmod_(P.to_edited(self.p, t)))} {ln}" for t, ln in zip(starts, lines))
        with fake_words(ws):
            info = S.import_script(copy.deepcopy(self.p), edited)
            self.assertEqual(info["base"], "edit")
            orig = "\n".join(f"{fmt(*divmod_(t))} {ln}" for t, ln in zip(starts, lines))
            self.assertEqual(S.import_script(copy.deepcopy(self.p), orig)["base"], "orig")
            q = copy.deepcopy(self.p)
            self.assertEqual(S.import_script(q, edited, base="orig")["base"], "orig")
        with self.assertRaises(ValueError):
            S.import_script(self.p, "x", base="nope")

    def test_no_words_uses_script_times(self):
        script = "00:01 첫 줄\n00:03 둘째 줄\n00:20 셋째 줄\n[00:30 ~ 00:31] 끝 있는 줄"
        with fake_words([]):
            info = S.import_script(self.p, script)
            self.assertEqual((info["unmatched"], info["base"]), (0, "orig"))
            got = [(t["start"], t["end"]) for t in sorted(self.p["subtitles"], key=lambda t: t["start"])]
            self.assertEqual(got, [(1.0, 3.0), (3.0, 9.0), (20.0, 26.0), (30.0, 31.0)])  # 다음 줄 시작까지, 최대 6초
            self.assertFalse(any(t["check"] for t in self.p["subtitles"]))
            with self.assertRaises(ValueError):
                S.import_script(self.p, "시간 없는 대본")

    def test_replace_false_keeps_existing(self):
        self.p["subtitles"] = [sub("keep01", 40.0, 41.0)]
        with fake_words(self.ws):
            S.import_script(self.p, "00:02 안녕하세요 여러분", replace=False)
        self.assertIn("keep01", by_id(self.p))
        with fake_words(self.ws):
            S.import_script(self.p, "00:02 안녕하세요 여러분")
        self.assertNotIn("keep01", by_id(self.p))

    @unittest.skipUnless((GYM / "transcript.whisper.json").exists() and (GYM / "transcript_ext.txt").exists(),
                         "gym 자료 없음")
    def test_gym_alignment(self):
        """실제 자료: 외부 전사·whisper 덤프를 gym whisper 단어에 맞춰요 (메모리에서만, gym 은 안 건드려요)."""
        ws = [{**x, "sid": "s1"} for x in json.loads((GYM / "transcript.whisper.json").read_text())["words"]]
        report = {}
        for f, floor in (("transcript_ext.txt", 0.3), ("script.txt", 0.9)):
            if not (GYM / f).exists():
                continue
            p = mem((2260.523,))
            t0 = time.perf_counter()
            with fake_words(ws):
                info = S.import_script(p, (GYM / f).read_text(encoding="utf-8"))
            report[f] = {**info, "sec": round(time.perf_counter() - t0, 2)}
            self.assertGreater(info["matched"], floor, info)
            self.assertLess(info["unmatched"], info["count"] * 0.5)
            assert_valid(self, p)
        print("\n  gym 대본 맞추기:", json.dumps(report, ensure_ascii=False))


def divmod_(t):
    m, s = divmod(t, 60)
    return int(m), s


class SplitMerge(unittest.TestCase):

    def setUp(self):
        self.p = mem(subtitles=[sub("a", 1.0, 3.0, "안녕하세요 여러분 반가워요"), sub("b", 5.0, 6.0, "다음 줄")])
        S.retime(self.p)

    def test_split_by_word_timing(self):
        ws = [w(1.0, 1.8, "안녕하세요"), w(1.9, 2.3, "여러분"), w(2.4, 3.0, "반가워요")]
        with fake_words(ws):
            a, b = S.split_subtitle(self.p, "a", 2.35)
        self.assertEqual((a["text"], b["text"]), ("안녕하세요 여러분", "반가워요"))
        self.assertEqual((a["speech"], b["speech"]), ([1.0, 2.35], [2.35, 3.0]))
        self.assertEqual(a["id"], "a")
        self.assertNotEqual(b["id"], "a")
        self.assertEqual((a["start"], a["end"], b["start"], b["end"]), (1.0, 2.35, 2.35, 3.3))
        self.assertEqual((a["by"], b["rev"]), ("ui", 5))
        assert_valid(self, self.p)

    def test_split_ratio_fallback_and_locked(self):
        self.p["subtitles"][0]["locked"] = True
        with fake_words([]):
            a, b = S.split_subtitle(self.p, "a", 1.4)
        self.assertEqual((a["text"], b["text"]), ("안녕하세요", "여러분 반가워요"))
        self.assertTrue(a["locked"] and b["locked"])
        self.assertEqual((a["start"], a["end"], b["start"], b["end"]), (1.0, 1.4, 1.4, 3.3))
        with fake_words([]):
            x, y = S.split_subtitle(self.p, "b", 5.5)
        self.assertEqual((x["text"], y["text"]), ("다음", "줄"))
        for bad in (1.0, 1.02, 99, float("nan"), "1"):
            with self.assertRaises(ValueError):
                with fake_words([]):
                    S.split_subtitle(self.p, y["id"], bad)
        with self.assertRaises(ValueError):
            S.split_subtitle(self.p, "nope", 2.0)

    def test_merge(self):
        m = S.merge_subtitles(self.p, ["b", "a"])
        self.assertEqual((m["id"], m["text"], m["speech"]), ("a", "안녕하세요 여러분 반가워요 다음 줄", [1.0, 6.0]))
        self.assertEqual(list(by_id(self.p)), ["a"])
        self.assertEqual((m["start"], m["end"]), (1.0, 6.3))
        for bad in (["a"], ["a", "a"], ["a", "zz"], "a"):
            with self.assertRaises(ValueError):
                S.merge_subtitles(self.p, bad)


class DiffGenerate(unittest.TestCase):

    def test_diff(self):
        p = mem(cuts=[cut("c1", 1, 2)], subtitles=[sub("a", 1, 2), sub("b", 3, 4)])
        old = copy.deepcopy(p)
        p["subtitles"][0]["text"] = "바뀜"
        del p["subtitles"][1]
        p["cuts"].append(cut("c2", 5, 6))
        p["settings"]["subs"]["hold"] = 0.9
        got = S.diff(old, p)
        self.assertIn({"coll": "subtitles", "item": p["subtitles"][0]}, got)
        self.assertIn({"coll": "subtitles", "id": "b", "deleted": True}, got)
        self.assertIn({"coll": "cuts", "item": p["cuts"][1]}, got)
        self.assertIn({"key": "settings.subs", "value": p["settings"]["subs"]}, got)
        self.assertEqual(len(got), 4)

    def test_generate_per_source(self):
        p = mem((10.0, 10.0), subtitles=[sub("old1", 1, 2), sub("old2", 12, 13)])
        ws = [w(1.0, 1.5, "첫째", "s1"), w(11.0, 11.5, "둘째", "s2")]
        with fake_words(ws):
            new = S.generate(p, ["s2"])
        self.assertEqual([t["text"] for t in new], ["둘째"])
        self.assertEqual(sorted(t["text"] for t in p["subtitles"]), ["둘째", "자막"])
        with fake_words(ws):
            S.generate(p)
        self.assertEqual([t["text"] for t in p["subtitles"]], ["첫째", "둘째"])


if __name__ == "__main__":
    unittest.main()

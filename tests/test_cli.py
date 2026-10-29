"""ev.py(Claude 용 명령줄)를 진짜 프로세스로 실행해 확인해요. 바꾸는 명령은 project.json 에 by="cli" 로 남고,
출력은 한 줄에 하나씩 원본·편집본 시간을 같이 보여요. 임시 프로젝트는 projects/_t_cli_* 에 만들고 끝나면 지워요."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from editvdo import analyze, project

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "test_vlog.mp4"
SRT = ("1\n00:00:00,000 --> 00:00:02,600\n안녕하세요 여러분, 오늘은 성수동 카페\n\n"
       "2\n00:00:02,700 --> 00:00:04,100\n투어 브이로그입니다\n")


def ev(*args, code=0, env=None):
    r = subprocess.run([sys.executable, str(ROOT / "ev.py"), *map(str, args)], cwd=ROOT, capture_output=True,
                       text=True, timeout=900, env={**os.environ, **(env or {})})
    if code is not None and r.returncode != code:
        raise AssertionError(f"ev.py {' '.join(map(str, args))} → {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


@unittest.skipUnless(hasattr(analyze, "analyze_source"), "analyze v2 가 아직 없어요")
class Cli(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="editvdo-cli-"))
        cls.clips = [cls.tmp / "a.mp4", cls.tmp / "b.mp4"]
        for x in cls.clips:
            shutil.copyfile(SAMPLE, x)
        cls.name = f"_t_cli_{uuid.uuid4().hex[:6]}"
        cls.made = [cls.name]
        cls.new = ev("new", "명령줄 테스트", cls.clips[0], "--name", cls.name, "--no-transcribe")

    @classmethod
    def tearDownClass(cls):
        for n in cls.made:
            shutil.rmtree(project.PROJECTS / n, ignore_errors=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def p(self):
        return project.load(self.name)

    def test_01_new_and_text(self):
        out = self.new.stdout
        self.assertIn(f"프로젝트 {self.name}", out)
        self.assertRegex(out, r"\d+% s1 (변환|분석)")
        p = self.p()
        self.assertEqual([(s["id"], s["status"]) for s in p["sources"]], [("s1", "ready")])
        self.assertFalse((project.project_dir(self.name) / "sources/s1/transcript.json").exists())
        self.assertGreater(len(p["cuts"]), 0)
        out = ev("text", self.name).stdout.splitlines()
        self.assertTrue(out[0].startswith(f"명령줄 테스트 ({self.name}) · rev "))
        self.assertTrue(out[1].startswith("소스: s1 a.mp4 00:00.00–00:34.03"))
        self.assertIn("(음성 인식 단어가 없어요)", out[-1])
        self.assertTrue(any("✂ [" in x for x in out))

    def test_02_cut_toggle_drop(self):
        r = ev("cut", self.name, "0:20", "0:21.5", "테스트 컷", "--type", "retake")
        self.assertTrue(r.stdout.startswith("추가 "), r.stdout)
        c = next(c for c in self.p()["cuts"] if c["reason"] == "테스트 컷")
        self.assertEqual((c["type"], c["by"], c["enabled"], c["reviewed"]), ("retake", "cli", True, False))
        self.assertTrue(19.5 <= c["start"] <= 20.5 and 21.0 <= c["end"] <= 22.0, c)
        self.assertIn(f"[{c['id']} retake on]", r.stdout)
        ev("off", self.name, c["id"])
        self.assertFalse(next(x for x in self.p()["cuts"] if x["id"] == c["id"])["enabled"])
        auto = next(x for x in self.p()["cuts"] if x["by"] == "auto")
        project.transact(self.name, lambda p: project.apply_ops(
            p, [{"op": "put", "coll": "cuts", "item": {"id": auto["id"], "reviewed": True}}], "ui"), by="ui")
        r = ev("on", self.name, c["id"], auto["id"])
        cuts = {x["id"]: x for x in self.p()["cuts"]}
        self.assertTrue(cuts[c["id"]]["enabled"] and cuts[auto["id"]]["enabled"])
        self.assertFalse(cuts[auto["id"]]["reviewed"])  # 사용자에게 다시 보여요
        self.assertIn("미확인", r.stdout)
        r = ev("off", self.name, "c_nope", code=1)
        self.assertIn("없는 컷이에요: c_nope", r.stderr)
        ev("drop", self.name, c["id"])
        self.assertNotIn(c["id"], {x["id"] for x in self.p()["cuts"]})

    def test_03_cuts_and_detect(self):
        out = ev("cuts", self.name).stdout.splitlines()
        self.assertTrue(out[0].startswith(f"컷 {len(self.p()['cuts'])}개"))
        self.assertEqual(len(out) - 1, len(self.p()["cuts"]))
        todo = ev("cuts", self.name, "--todo").stdout.splitlines()
        self.assertEqual(len(todo) - 1, sum(not c["reviewed"] for c in self.p()["cuts"]))
        snaps = len(list((project.project_dir(self.name) / "history").glob("*.json")))
        r = ev("detect", self.name, "--min-silence", "0.6", "--no-fillers")
        self.assertIn("자동 컷을 다시 찾았어요", r.stdout)
        cs = self.p()["settings"]["cut"]
        self.assertEqual((cs["min_silence"], cs["fillers"]), (0.6, False))
        self.assertEqual(len(list((project.project_dir(self.name) / "history").glob("*.json"))), snaps + 1)

    def test_04_import_and_edit_subs(self):
        script = self.tmp / "대본.srt"
        script.write_text(SRT, encoding="utf-8")
        r = ev("import", self.name, script)
        self.assertIn("불러온 줄 2", r.stdout)
        subs = self.p()["subtitles"]
        self.assertEqual([t["by"] for t in subs], ["cli", "cli"])
        tid = subs[0]["id"]
        self.assertIn(tid, ev("subs", self.name).stdout)
        r = ev("sub", self.name, tid, "--text", "고친 글", "--pos", "30,70", "--speaker", "b")
        t = next(x for x in self.p()["subtitles"] if x["id"] == tid)
        self.assertEqual((t["text"], t["pos"], t["speaker"]), ("고친 글", {"x": 30, "y": 70}, "B"))
        self.assertIn("B", {s["id"] for s in self.p()["speakers"]})
        self.assertIn("「고친 글」", r.stdout)
        ev("sub", self.name, tid, "--start", "0.2")
        t = next(x for x in self.p()["subtitles"] if x["id"] == tid)
        self.assertEqual((t["start"], t["locked"]), (0.2, True))
        ev("sub", self.name, tid, "--unlock", "--check", "--pos", "none")
        t = next(x for x in self.p()["subtitles"] if x["id"] == tid)
        self.assertEqual((t["locked"], t["check"], t["pos"]), (False, True, None))
        checked = ev("subs", self.name, "--check").stdout.splitlines()
        self.assertEqual([x for x in checked if "「" in x and tid not in x], [])
        self.assertEqual(ev("sub", self.name, "t_nope", "--text", "x", code=1).stderr.strip(), "오류: 없는 자막이에요: t_nope")

    def test_05_timing_and_speaker(self):
        r = ev("timing", self.name, "--hold", "0.6", "--bridge", "0.2")
        self.assertIn("hold=0.6", r.stdout)
        st = self.p()["settings"]["subs"]
        self.assertEqual((st["hold"], st["bridge"]), (0.6, 0.2))
        self.assertIn("lead=0.0", ev("timing", self.name).stdout)
        r = ev("speaker", self.name, "b", "--name", "민수", "--pos", "70,80", "--color", "#A78BFA")
        s = next(x for x in self.p()["speakers"] if x["id"] == "B")
        self.assertEqual((s["name"], s["pos"], s["color"]), ("민수", {"x": 70, "y": 80}, "#A78BFA"))
        self.assertIn("화자 B 민수", r.stdout)
        self.assertIn("화자", ev("speaker", self.name, "B", "--color", "red", code=1).stderr)

    def test_06_comments_reply_note_at(self):
        p, ch = project.transact(self.name, lambda p: project.apply_ops(
            p, [{"op": "put", "coll": "comments", "item": {"t": 3.0, "text": "여기 살려줘"}}], "ui"), by="ui")
        mid = ch[0]["item"]["id"]
        self.assertIn(f"{mid} 할 일 「여기 살려줘」", ev("comments", self.name).stdout)
        r = ev("reply", self.name, mid, "살렸어요", "--done")
        m = next(x for x in self.p()["comments"] if x["id"] == mid)
        self.assertEqual((m["reply"], m["done"]), ("살렸어요", True))
        self.assertIn("→ 답: 살렸어요", r.stdout)
        ev("note", self.name, "컷 3개를 살렸어요")
        self.assertEqual(self.p()["notes"], "컷 3개를 살렸어요")
        self.assertEqual(self.p()["by"], "cli")
        r = ev("at", self.name, "0:03")
        orig = project.to_original(self.p(), 3.0)
        self.assertTrue(r.stdout.startswith(f"편집본 00:03.00 = 원본 {ev_fmt(orig)} (s1 a.mp4"), r.stdout)
        self.assertIn("편집본은", ev("at", self.name, "59:00", code=1).stderr)

    def test_07_export_capcut_folder_or_here(self):
        """기본은 exports/ 에 다른 맥으로 옮길 폴더, --here 는 이 맥 캡컷 (여기선 없는 폴더라 아무것도 안 써요)."""
        r = ev("export", self.name, "capcut", "--name", "명령줄")
        folder = Path(r.stdout.strip().splitlines()[-1].split(": ", 1)[1])
        self.assertEqual((folder.name, folder.parent.parent), ("명령줄", project.project_dir(self.name) / "exports"))
        self.assertTrue((folder / "draft_info.json").is_file() and any((folder / "media").iterdir()))
        nowhere = self.tmp / "캡컷 없음"
        r = ev("export", self.name, "capcut", "--here", code=1, env={"EDITVDO_CAPCUT_ROOT": str(nowhere)})
        self.assertRegex(r.stderr, "캡컷을 먼저 닫아 주세요|캡컷 프로젝트 폴더가 없어요")
        self.assertFalse(nowhere.exists())

    def test_07_export_srt(self):
        r = ev("export", self.name, "srt")
        path = Path(r.stdout.strip().split(": ", 1)[1])
        self.assertTrue(path.is_file() and path.parent.parent.name == "exports")
        self.assertIn("-->", path.read_text(encoding="utf-8"))

    def test_08_add_order_remove(self):
        r = ev("add", self.name, self.clips[1])
        self.assertIn("영상 1개를 더했어요 (s2)", r.stdout)
        p = self.p()
        self.assertEqual([(s["id"], s["status"]) for s in p["sources"]], [("s1", "ready"), ("s2", "ready")])
        self.assertTrue((project.project_dir(self.name) / "sources/s2/transcript.json").exists())
        ev("order", self.name, "s2", "s1")
        self.assertEqual([s["id"] for s in self.p()["sources"]], ["s2", "s1"])
        self.assertIn("영상 순서", ev("order", self.name, "s1", code=1).stderr)
        ev("remove", self.name, "s2")
        self.assertEqual([(s["id"], s["offset"]) for s in self.p()["sources"]], [("s1", 0.0)])

    def test_09_subgen_and_words(self):
        r = ev("subgen", self.name)
        self.assertIn("음성 인식부터 해요: s1", r.stdout)
        subs = self.p()["subtitles"]
        self.assertGreater(len(subs), 3)
        self.assertTrue(all(t["by"] == "auto" for t in subs))
        out = ev("text", self.name, "--words", "--from", "0", "--to", "0:05").stdout
        self.assertIn("└ ", out)
        self.assertNotIn("(음성 인식 단어가 없어요)", out)

    def test_10_analyze_one_source(self):
        r = ev("analyze", self.name, "--sid", "s1")
        self.assertIn("s1", r.stdout)
        self.assertEqual(self.p()["sources"][0]["status"], "ready")
        self.assertIn("없는 영상이에요: s7", ev("analyze", self.name, "--sid", "s7", code=1).stderr)

    def test_11_errors(self):
        self.assertIn("프로젝트가 없어요", ev("text", "_t_cli_nope", code=1).stderr)
        self.assertIn("시간 형식을 확인해 주세요", ev("cut", self.name, "1:75", "2", "x", code=2).stderr)
        self.assertIn("영상 범위", ev("cut", self.name, "90:00", "91:00", "x", code=1).stderr)

    def test_12_migrate_v1(self):
        name = f"_t_cli_v1_{uuid.uuid4().hex[:6]}"
        self.made.append(name)
        d = project.PROJECTS / name
        d.mkdir()
        shutil.copyfile(SAMPLE, d / "preview.mp4")
        (d / "meta.json").write_text(json.dumps({"source_path": str(self.clips[0]), "duration": 34.033, "width": 1920,
                                                 "height": 1080, "fps": 30}), encoding="utf-8")
        (d / "edit.json").write_text(json.dumps({"cuts": [], "subtitles": [], "notes": ""}), encoding="utf-8")
        self.assertIn(f"ev.py migrate {name}", ev("text", name, code=1).stderr)
        r = ev("migrate", name)
        self.assertIn("v1 프로젝트를 옮겼어요", r.stdout)
        self.assertTrue((d / "sources/s1/thumbs.jpg").exists())
        self.assertIn("이미 v2 프로젝트예요", ev("migrate", name).stdout)


def ev_fmt(t):
    cs = int(round(t * 100))
    return f"{cs // 6000:02d}:{cs % 6000 // 100:02d}.{cs % 100:02d}"


if __name__ == "__main__":
    unittest.main()

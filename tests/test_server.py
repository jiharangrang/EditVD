"""HTTP API v2(server.py)와 작업(jobs.py)을 진짜 서버(빈 포트)로 확인해요.
프로젝트 만들기 → 분석 대기 → 편집 연산 → 컷·자막 묶음 편집 → 소스 순서·빼기·다시 분석 → 내보내기(srt·mp4·캡컷은 임시 폴더),
보안(Host·Origin·JSON·경로), Range 요청, v1 옮기기, 서버가 다시 켜졌을 때 멈춘 분석 정리.
임시 프로젝트는 projects/_t_server_* 에 만들고 끝나면 지워요."""
import http.client
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import unittest
import uuid
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from editvdo import analyze, capcut, jobs, project, server

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "test_vlog.mp4"
HAS_ENGINES = hasattr(analyze, "analyze_source")
SRT = ("1\n00:00:00,000 --> 00:00:02,600\n안녕하세요 여러분, 오늘은 성수동 카페\n\n"
       "2\n00:00:02,700 --> 00:00:04,100\n투어 브이로그입니다\n")
CLOVA = ("참석자 1 00:00\n안녕하세요 여러분, 오늘은 성수동 카페 투어 브이로그입니다\n\n"
         "참석자 1 00:07\n첫 번째로 간 곳은 골목 안쪽에 있는 작은 로스터리 카페예요\n")
REAL_COVER = project._legacy_cover


def setUpModule():
    """/api/projects 를 읽는 테스트가 사용자 v1 프로젝트(test_vlog 등)에 표지를 만들지 않게, 표지는 _t_* 프로젝트에만 만들어요."""
    guard = mock.patch.object(project, "_legacy_cover", lambda d, name, dur: REAL_COVER(d, name, dur)
                              if name.startswith("_t_") else None)
    guard.start()
    unittest.addModuleCleanup(guard.stop)


class Api:
    """Host·Origin·Content-Type 을 직접 정해서 보내는 작은 클라이언트."""

    def __init__(self, port):
        self.port = port

    def call(self, method, path, body=None, headers=None, raw=False, data=None):
        h = {"Host": f"localhost:{self.port}"}
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=300)
        try:
            conn.request(method, quote(path, safe="/?=&%:+,;@~"), data, h)
            r = conn.getresponse()
            payload = r.read()
            hdrs = {k.lower(): v for k, v in r.getheaders()}
        finally:
            conn.close()
        if raw:
            return r.status, hdrs, payload
        return r.status, (json.loads(payload) if payload else None)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, {} if body is None else body, **kw)

    def ok(self, method, path, body=None):
        st, data = self.call(method, path, body)
        if st != 200:
            raise AssertionError(f"{method} {path} → {st} {data}")
        return data

    def wait(self, job_id, timeout=900):
        end = time.time() + timeout
        while time.time() < end:
            st, j = self.get(f"/api/jobs/{job_id}")
            if st == 200 and j["state"] in ("done", "error"):
                return j
            time.sleep(0.3)
        raise AssertionError(f"작업이 끝나지 않았어요: {job_id}")


def start_server():
    srv = server.make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, Api(srv.server_port)


def idle(name, timeout=300):
    end = time.time() + timeout
    while jobs.busy(name) and time.time() < end:
        time.sleep(0.2)


def temp_name(tag):
    return f"_t_server_{tag}{uuid.uuid4().hex[:6]}"


class Security(unittest.TestCase):
    """Host·Origin·JSON 검사, 경로 밖 파일 막기, osascript·mdfind·open 에는 사용자 값을 인자로만 넘겨요."""

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.api = start_server()
        cls.tmp = Path(tempfile.mkdtemp(prefix="editvdo-server-sec-"))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_host_check_on_every_request(self):
        for host in ("evil.example:%d" % self.api.port, "localhost", "localhost:1", "[::1]:%d" % self.api.port):
            st, data = self.api.get("/api/projects", headers={"Host": host})
            self.assertEqual(st, 403, host)
            self.assertIn("error", data)
        st, _, _ = self.api.get("/", headers={"Host": "evil.example"}, raw=True)
        self.assertEqual(st, 403)
        st, data = self.api.get("/api/projects", headers={"Host": f"127.0.0.1:{self.api.port}"})
        self.assertEqual(st, 200)
        self.assertIsInstance(data, list)
        self.assertTrue(all("path" in x and Path(x["path"]).is_absolute() for x in data))

    def test_post_needs_same_origin_and_json(self):
        st, data = self.api.post("/api/pick", {"kind": "video"}, headers={"Origin": "http://evil.example"})
        self.assertEqual((st, set(data)), (403, {"error"}))
        st, _ = self.api.post("/api/pick", {"kind": "video"}, headers={"Origin": "null"})
        self.assertEqual(st, 403)
        st, data = self.api.call("POST", "/api/pick", data=b'{"kind":"video"}', headers={"Content-Type": "text/plain"})
        self.assertEqual(st, 415)
        st, data = self.api.call("POST", "/api/pick", data=b"{oops", headers={"Content-Type": "application/json"})
        self.assertEqual(st, 400)
        st, data = self.api.call("POST", "/api/pick", data=b"[1]", headers={"Content-Type": "application/json"})
        self.assertEqual(st, 400)
        with mock.patch.object(server, "_osascript", return_value=subprocess.CompletedProcess([], 1, "", "(-128)")):
            for origin in (None, f"http://localhost:{self.api.port}", f"http://127.0.0.1:{self.api.port}"):
                st, data = self.api.post("/api/pick", {"kind": "video"}, headers={"Origin": origin} if origin else None)
                self.assertEqual((st, data), (200, {"paths": []}), origin)

    def test_static_files_and_traversal(self):
        st, h, body = self.api.get("/", raw=True)
        self.assertEqual(st, 200)
        self.assertIn("text/html", h["content-type"])
        self.assertEqual(h["cache-control"], "no-cache")
        st, h, body = self.api.get("/ui/js/api.js", raw=True)
        self.assertEqual((st, h["content-type"].split(";")[0]), (200, "text/javascript"))
        st, h, _ = self.api.get("/ui/js/api.js", headers={"If-None-Match": h["etag"]}, raw=True)
        self.assertEqual(st, 304)
        for bad in ("/ui/../ev.py", "/ui/%2e%2e/ev.py", "/ui/js/../../ev.py", "/ui/.hidden", "/ui/", "/ui//etc/passwd",
                    "/media/x/sources/s1/../../project.json", "/media/x/project.json", "/media/../ev.py",
                    "/media/x/exports/../../ev.py", "/media/x/sources/s1/audio.wav", "/api/p/..%2Fprojects"):
            st, _, _ = self.api.get(bad, raw=True)
            self.assertEqual(st, 404, bad)

    def test_pick_passes_kind_as_argument(self):
        clip = self.tmp / "고른 영상.mov"
        clip.write_bytes(b"x")
        done = subprocess.CompletedProcess([], 0, f"{clip}\n/없는/파일.mov\n", "")
        with mock.patch.object(server, "_osascript", return_value=done) as osa:
            self.assertEqual(self.api.ok("POST", "/api/pick", {"kind": "script"}), {"paths": [str(clip)]})
        osa.assert_called_once_with(server.PICK_SCRIPT, "script")
        self.assertNotIn("script\"", server.PICK_SCRIPT)
        with mock.patch.object(server, "_osascript") as osa:
            st, _ = self.api.post("/api/pick", {"kind": "video\" & do shell script \"x"})
        self.assertEqual(st, 400)
        osa.assert_not_called()
        with mock.patch.object(server, "_osascript", return_value=subprocess.CompletedProcess([], 1, "", "boom")):
            st, data = self.api.post("/api/pick", {"kind": "video"})
        self.assertEqual(st, 400)

    def test_resolve_by_name_and_size(self):
        nfc = unicodedata.normalize("NFC", "여행 클립.mov")
        good, clone = self.tmp / nfc, self.tmp / "Movies/CapCut/초안/media" / nfc  # 캡컷으로 내보낸 복사본
        good.write_bytes(b"1234567")
        clone.parent.mkdir(parents=True)
        clone.write_bytes(b"1234567")
        found = [str(clone), str(self.tmp / "다른.mov"), "/없는/곳/" + nfc, str(good)]
        with mock.patch.object(server, "_mdfind", return_value=found) as md, \
                mock.patch.object(server, "_scan", return_value=[]) as sc:
            out = self.api.ok("POST", "/api/resolve", {"files": [
                {"name": unicodedata.normalize("NFD", nfc), "size": 7, "mtime": good.stat().st_mtime},
                {"name": nfc, "size": 8},
                {"name": "../etc/passwd", "size": 1}, {"name": 3}, "x"]})
        self.assertEqual(out, {"paths": [str(good)]})
        md.assert_any_call(nfc)
        self.assertTrue(all(c.args == (nfc,) for c in md.call_args_list))
        sc.assert_called_once_with(nfc)  # 크기가 다른 두 번째 파일만 폴더 뒤지기로 넘어가요
        st, _ = self.api.post("/api/resolve", {"files": "x"})
        self.assertEqual(st, 400)

    def test_open_reveals_or_launches_capcut(self):
        with mock.patch.object(server, "_open", return_value=0) as op:
            self.assertEqual(self.api.ok("POST", "/api/open", {"path": "projects"}), {"ok": True})
            op.assert_called_with(["-R", str(ROOT / "projects")])
            self.api.ok("POST", "/api/open", {"path": str(self.tmp)})
            op.assert_called_with(["-R", str(self.tmp)])
            self.api.ok("POST", "/api/open", {"app": "CapCut"})
            op.assert_called_with(["-a", "CapCut"])
            calls = op.call_count
            self.assertEqual(self.api.post("/api/open", {"app": "Terminal"})[0], 400)
            self.assertEqual(self.api.post("/api/open", {"path": "/없는/경로"})[0], 404)
            self.assertEqual(self.api.post("/api/open", {"path": ""})[0], 400)
            self.assertEqual(op.call_count, calls)
        with mock.patch.object(server, "_open", return_value=1):
            st, data = self.api.post("/api/open", {"app": "CapCut"})
        self.assertEqual((st, data["error"]), (404, "캡컷을 찾을 수 없어요"))

    def test_unknown_routes_and_projects(self):
        self.assertEqual(self.api.get("/api/nope")[0], 404)
        self.assertEqual(self.api.get("/api/p/없는프로젝트_t")[0], 404)
        self.assertEqual(self.api.get("/api/p/없는프로젝트_t/rev")[0], 404)
        self.assertEqual(self.api.get("/api/jobs/nope")[0], 404)
        self.assertEqual(self.api.post("/api/p/없는프로젝트_t/ops", {"ops": [{"op": "set", "key": "notes", "value": ""}]})[0], 404)
        self.assertEqual(self.api.post("/api/p/bad name!/ops", {"ops": []})[0], 404)
        st, data = self.api.post("/api/projects", {"paths": ["/없는/영상.mov"]})
        self.assertEqual(st, 400)
        self.assertIn("찾을 수 없어요", data["error"])


class JobQueue(unittest.TestCase):
    """분석 작업은 전체에서 하나씩, 나머지는 둘까지 같이 돌고, 오류(SystemExit 포함)는 작업 상태로 남아요."""
    NAME = "_t_server_unit_jobs"

    def wait(self, ids, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            if all(jobs.get(i)["state"] in ("done", "error") for i in ids):
                return [jobs.get(i) for i in ids]
            time.sleep(0.05)
        raise AssertionError("작업이 끝나지 않았어요")

    def test_heavy_jobs_run_one_at_a_time(self):
        spans = []

        def work(progress):
            t0 = time.monotonic()
            progress(0.5, "분석")
            time.sleep(0.25)
            spans.append((t0, time.monotonic()))
            return "ok"
        out = self.wait([jobs.submit(self.NAME, "analyze", work, heavy=True)["id"] for _ in range(3)])
        self.assertEqual([j["state"] for j in out], ["done"] * 3)
        self.assertEqual([j["result"] for j in out], ["ok"] * 3)
        spans.sort()
        for (_, e0), (s1, _) in zip(spans, spans[1:]):
            self.assertGreaterEqual(s1, e0 - 0.01)

    def test_light_jobs_run_two_at_a_time(self):
        lock, state = threading.Lock(), {"now": 0, "peak": 0}

        def work(progress):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.25)
            with lock:
                state["now"] -= 1
        self.wait([jobs.submit(self.NAME, "export", work)["id"] for _ in range(5)])
        self.assertEqual(state["peak"], 2)

    def test_errors_become_job_state(self):
        def exits(progress):
            raise SystemExit(3)

        def fails(progress):
            progress(0.4, "변환")
            raise ValueError("한국어 오류예요")
        with mock.patch.object(jobs, "_trace") as trace:  # 뜻밖의 오류는 터미널에 자세히 남겨요 (여기선 조용히)
            a, b = self.wait([jobs.submit(self.NAME, "analyze", exits, heavy=True)["id"],
                              jobs.submit(self.NAME, "export", fails, target="mp4")["id"]])
        self.assertEqual(trace.call_count, 2)
        self.assertEqual(a["state"], "error")
        self.assertIn("SystemExit", a["message"])
        self.assertEqual((b["state"], b["message"], b["progress"], b["target"]), ("error", "한국어 오류예요", 0.4, "mp4"))
        listed = {j["id"]: j for j in jobs.active(self.NAME)}
        self.assertIn(a["id"], listed)
        self.assertEqual(set(listed[b["id"]]) & {"project", "result"}, set())
        self.assertFalse(jobs.busy(self.NAME))


class Recovery(unittest.TestCase):
    """서버가 다시 켜진 뒤 처음 열 때: 도는 작업도 분석 잠금도 없으면 queued·analyzing → error '중단됨'.
    ev.py 가 분석 중(잠금)이면 그대로 둬요. 아직 없는 미리보기·파형·썸네일은 null 이에요."""

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.api = start_server()
        cls.name = project.create("멈춘 분석", [str(SAMPLE), str(SAMPLE)], name=temp_name("rec"))

    @classmethod
    def tearDownClass(cls):
        idle(cls.name)
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(project.project_dir(cls.name), ignore_errors=True)

    def test_stuck_sources_become_errors_unless_someone_analyzes(self):
        project.transact(self.name, lambda p: p["sources"][1].update(status="analyzing", progress=0.4, step="인식"), by="job")
        with jobs.analysis_lock(self.name):  # ev.py 가 분석하는 중
            d = self.api.ok("GET", f"/api/p/{self.name}")
        self.assertEqual([s["status"] for s in d["project"]["sources"]], ["queued", "analyzing"])
        self.assertEqual(d["words_count"], 0)
        self.assertIsNone(d["preview"])
        play = d["assets"]["s1"].pop("play")  # 원본은 분석 전에도 바로 재생돼요
        self.assertTrue(play.startswith(f"/media/{quote(self.name)}/sources/s1/original"), play)
        self.assertEqual(d["assets"]["s1"], {"proxy": None, "peaks": None, "thumbs": None, "thumbs_meta": None,
                                             "silences": None})
        rev = d["project"]["rev"]
        d = self.api.ok("GET", f"/api/p/{self.name}")
        self.assertEqual([(s["status"], s["error"]) for s in d["project"]["sources"]], [("error", "중단됨")] * 2)
        self.assertEqual((d["project"]["rev"], d["project"]["by"]), (rev + 1, "job"))
        self.assertEqual(self.api.ok("GET", f"/api/p/{self.name}")["project"]["rev"], rev + 1)  # 한 번만 바꿔요


@unittest.skipUnless(HAS_ENGINES, "analyze v2 가 아직 없어요")
class Migration(unittest.TestCase):
    """v1 프로젝트를 처음 열면 project.json 으로 옮기고 (v1 파일은 그대로), 분석 없이 썸네일만 만드는 작업을 넣어요."""

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.api = start_server()
        cls.name = temp_name("v1_")
        d = cls.d = project.PROJECTS / cls.name
        d.mkdir(parents=True)
        shutil.copyfile(SAMPLE, d / "preview.mp4")
        (d / "meta.json").write_text(json.dumps({"source_path": str(d / "preview.mp4"), "duration": 34.033, "width": 1920,
                                                 "height": 1080, "fps": 30, "has_audio": True}), encoding="utf-8")
        cls.edit = json.dumps({"version": 2, "cuts": [{"id": "c1", "start": 4.0, "end": 6.0, "type": "silence",
                                                      "reason": "", "enabled": True}],
                               "subtitles": [{"id": "s1", "start": 0.0, "end": 2.6, "text": "안녕하세요 여러분",
                                              "auto": "안녕하세요 여러분"}], "notes": "v1 메모"}, ensure_ascii=False)
        (d / "edit.json").write_text(cls.edit, encoding="utf-8")
        (d / "review.json").write_text(json.dumps({"toggles": {"c1": False}}), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        idle(cls.name)
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.d, ignore_errors=True)

    def test_first_open_migrates_and_builds_thumbs(self):
        listed = {x["name"]: x for x in self.api.ok("GET", "/api/projects")}
        self.assertTrue(listed[self.name]["legacy"])
        self.assertEqual(listed[self.name]["cover"], f"/media/{self.name}/cover.jpg")  # v1 표지 한 장
        st, h, _ = self.api.get(listed[self.name]["cover"], raw=True)
        self.assertEqual((st, h["content-type"]), (200, "image/jpeg"))
        self.assertEqual(self.api.ok("GET", f"/media/{self.name}/cover.json"), {"w": 320, "h": 180, "cols": 1, "count": 1})
        self.assertEqual(self.api.get(f"/api/p/{self.name}/rev")[0], 404)  # 옮기기 전엔 v2 프로젝트가 아니에요
        d = self.api.ok("GET", f"/api/p/{self.name}")
        p = d["project"]
        self.assertTrue((self.d / "project.json").exists())
        self.assertEqual((self.d / "edit.json").read_text(encoding="utf-8"), self.edit)
        self.assertEqual([(s["id"], s["status"]) for s in p["sources"]], [("s1", "ready")])
        self.assertEqual([(c["id"], c["enabled"], c["reviewed"]) for c in p["cuts"]], [("c1", False, True)])
        self.assertEqual([t["text"] for t in p["subtitles"]], ["안녕하세요 여러분"])
        self.assertEqual(p["notes"], "v1 메모")
        self.assertIsNotNone(d["assets"]["s1"]["proxy"])
        self.assertIsNone(d["assets"]["s1"]["thumbs"])
        job = next(j for j in self.api.ok("GET", f"/api/p/{self.name}/rev")["jobs"] if j["kind"] == "thumbs")
        self.assertEqual(job["sids"], ["s1"])
        self.assertEqual(self.api.wait(job["id"], 300)["state"], "done")
        d2 = self.api.ok("GET", f"/api/p/{self.name}")
        self.assertGreater(d2["project"]["rev"], p["rev"])
        self.assertTrue(d2["assets"]["s1"]["thumbs"].startswith(f"/media/{quote(self.name)}/sources/s1/thumbs.jpg?v="))
        self.assertEqual(d2["assets"]["s1"]["thumbs_meta"]["cols"], 10)
        self.assertFalse(any(j["kind"] == "thumbs" and j["state"] in ("queued", "running")
                             for j in self.api.ok("GET", f"/api/p/{self.name}/rev")["jobs"]))
        st, h, body = self.api.get(d2["assets"]["s1"]["thumbs"], raw=True)
        self.assertEqual((st, h["content-type"]), (200, "image/jpeg"))


@unittest.skipUnless(HAS_ENGINES, "analyze v2 가 아직 없어요")
class Flow(unittest.TestCase):
    """두 클립(같은 샘플 두 벌)으로 프로젝트를 만들고 분석이 끝나길 기다린 뒤 API 를 차례로 써 봐요."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="editvdo-server-"))
        cls.clips = [cls.tmp / "a.mp4", cls.tmp / "b.mp4"]
        for x in cls.clips:
            shutil.copyfile(SAMPLE, x)
        cls.root = cls.tmp / "capcut-drafts"
        cls.root.mkdir()
        cls.patches = [mock.patch.object(capcut, "drafts_root", return_value=cls.root),
                       mock.patch.object(capcut, "capcut_running", return_value=False)]
        for x in cls.patches:
            x.start()
        cls.srv, cls.api = start_server()
        cls.name = temp_name("")
        st, res = cls.api.post("/api/projects", {"title": "서버 테스트", "paths": [str(x) for x in cls.clips],
                                                 "name": cls.name})
        assert st == 200, res
        cls.analysis = cls.api.wait(res["job"])

    @classmethod
    def tearDownClass(cls):
        idle(cls.name)
        cls.srv.shutdown()
        cls.srv.server_close()
        for x in cls.patches:
            x.stop()
        shutil.rmtree(project.project_dir(cls.name), ignore_errors=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def p(self):
        return project.load(self.name)

    def ops(self, ops, rev=None):
        rev = self.p()["rev"] if rev is None else rev
        return self.api.post(f"/api/p/{self.name}/ops", {"rev": rev, "ops": ops})

    def history(self):
        return sorted((project.project_dir(self.name) / "history").glob("*.json"))

    def test_01_open_assets_and_media(self):
        self.assertEqual(self.analysis["state"], "done", self.analysis)
        self.assertEqual((self.analysis["kind"], self.analysis["sids"]), ("analyze", ["s1", "s2"]))
        d = self.api.ok("GET", f"/api/p/{self.name}")
        p = d["project"]
        self.assertEqual([(s["id"], s["status"]) for s in p["sources"]], [("s1", "ready"), ("s2", "ready")])
        self.assertAlmostEqual(p["sources"][1]["offset"], p["sources"][0]["duration"], places=3)
        self.assertGreater(d["words_count"], 10)
        self.assertGreater(len(p["cuts"]), 0)
        self.assertGreater(len(p["subtitles"]), 0)
        types = {"play": "video/mp4", "peaks": "application/json", "thumbs": "image/jpeg", "silences": "application/json"}
        for sid in ("s1", "s2"):
            a = d["assets"][sid]
            self.assertEqual(a["thumbs_meta"]["cols"], 10)
            for key, ctype in types.items():
                self.assertTrue(a[key].startswith(f"/media/{quote(self.name)}/sources/{sid}/"), a[key])
                st, h, _ = self.api.get(a[key], raw=True)
                self.assertEqual((st, h["content-type"].split(";")[0]), (200, ctype), a[key])
        words = self.api.ok("GET", f"/api/p/{self.name}/words?from=0&to=3")
        self.assertTrue(words and all(w["end"] >= 0 and w["start"] <= 3 and w["sid"] == "s1" for w in words))
        later = self.api.ok("GET", f"/api/p/{self.name}/words?from=40")
        self.assertTrue(later and all(w["end"] >= 40 and w["sid"] == "s2" for w in later))
        self.assertEqual(self.api.get(f"/api/p/{self.name}/words?from=abc")[0], 400)

    def test_02_range_requests(self):  # 원본 그대로 재생 (project.json 에 적힌 파일)
        path = Path(self.p()["sources"][0]["path"])
        blob, size = path.read_bytes(), path.stat().st_size
        url = f"/media/{quote(self.name)}/sources/s1/original"
        st, h, body = self.api.get(url, raw=True)
        self.assertEqual((st, h["accept-ranges"], len(body)), (200, "bytes", size))
        st, h, body = self.api.get(url, headers={"Range": "bytes=0-99"}, raw=True)
        self.assertEqual((st, h["content-range"], body), (206, f"bytes 0-99/{size}", blob[:100]))
        st, h, body = self.api.get(url, headers={"Range": "bytes=1000-"}, raw=True)
        self.assertEqual((st, int(h["content-length"]), body), (206, size - 1000, blob[1000:]))
        st, h, body = self.api.get(url, headers={"Range": "bytes=-50"}, raw=True)
        self.assertEqual((st, h["content-range"], body), (206, f"bytes {size - 50}-{size - 1}/{size}", blob[-50:]))
        st, h, body = self.api.get(url, headers={"Range": f"bytes={size}-"}, raw=True)
        self.assertEqual((st, h["content-range"], body), (416, f"bytes */{size}", b""))
        st, h, body = self.api.get(url, headers={"Range": f"bytes=10-{size + 999}"}, raw=True)
        self.assertEqual((st, body), (206, blob[10:]))

    def test_03_rev_and_jobs(self):
        r = self.api.ok("GET", f"/api/p/{self.name}/rev")
        self.assertEqual(r["rev"], self.p()["rev"])
        self.assertIn(r["by"], ("job", "ui", "cli"))
        for j in r["jobs"]:
            self.assertLessEqual({"id", "kind", "state", "progress", "message"}, set(j))
        j = self.api.ok("GET", f"/api/jobs/{self.analysis['id']}")
        self.assertEqual((j["project"], j["state"], j["progress"]), (self.name, "done", 1.0))

    def test_04_ops(self):
        rev = self.p()["rev"]
        st, r = self.ops([{"op": "put", "coll": "cuts", "item": {"id": "cu1", "start": 5.0, "end": 6.0, "type": "user"}}], rev)
        self.assertEqual(st, 200, r)
        self.assertEqual(r["rev"], rev + 1)
        cut = next(c["item"] for c in r["changes"] if c.get("coll") == "cuts")
        self.assertEqual((cut["id"], cut["by"], cut["rev"], cut["enabled"]), ("cu1", "ui", rev + 1, True))
        st, r = self.ops([{"op": "put", "coll": "cuts", "item": {"id": "cu1", "enabled": False, "reviewed": True}}], rev + 1)
        self.assertEqual(st, 200, r)
        self.assertFalse(next(c for c in self.p()["cuts"] if c["id"] == "cu1")["enabled"])
        st, r = self.ops([{"op": "set", "key": "settings.subs", "value": {"hold": 0.1, "bridge": 0.0}}])
        self.assertEqual(st, 200, r)
        self.assertIn({"key": "settings.subs", "value": self.p()["settings"]["subs"]}, r["changes"])
        self.assertTrue(any(c.get("coll") == "subtitles" for c in r["changes"]))  # 다시 맞춘 자막
        before = self.p()["rev"]
        st, r = self.ops([{"op": "put", "coll": "cuts", "item": {"id": "cu2", "start": 7.0, "end": 6.0, "type": "user"}}])
        self.assertEqual(st, 400)
        self.assertIn("시작보다 뒤", r["error"])
        self.assertEqual(self.p()["rev"], before)
        # 그 사이 Claude 가 지운 컷을 UI 가 모른 채 고치면: 버리고 '지워짐'으로 알려요
        project.transact(self.name, lambda p: project.apply_ops(p, [{"op": "del", "coll": "cuts", "id": "cu1"}], "cli"))
        st, r = self.ops([{"op": "put", "coll": "cuts", "item": {"id": "cu1", "reviewed": False}},
                          {"op": "set", "key": "notes", "value": "UI 메모"}], before)
        self.assertEqual(st, 200, r)
        self.assertEqual(r["rev"], before + 2)
        self.assertIn({"coll": "cuts", "id": "cu1", "deleted": True}, r["changes"])
        self.assertFalse(any(c["id"] == "cu1" for c in self.p()["cuts"]))
        self.assertEqual(self.p()["notes"], "UI 메모")
        st, r = self.ops([{"op": "set", "key": "notes", "value": "x"}], self.p()["rev"] + 5)
        self.assertEqual(st, 409)
        self.assertEqual(self.ops([], before)[1], {"rev": self.p()["rev"], "changes": []})
        self.assertEqual(self.api.post(f"/api/p/{self.name}/ops", {"ops": "x"})[0], 400)
        self.ops([{"op": "set", "key": "title", "value": "새 제목"}])
        listed = {x["name"]: x for x in self.api.ok("GET", "/api/projects")}[self.name]
        self.assertEqual((listed["title"], listed["path"]), ("새 제목", str(project.project_dir(self.name))))

    def test_05_detect_keeps_decisions(self):
        auto = next(c for c in self.p()["cuts"] if c["by"] == "auto" and c["enabled"])
        self.assertEqual(self.ops([{"op": "put", "coll": "cuts", "item": {"id": auto["id"], "enabled": False,
                                                                           "reviewed": True}}])[0], 200)
        snaps = len(self.history())
        r = self.api.ok("POST", f"/api/p/{self.name}/cuts/detect", {"settings": {"min_silence": 0.45}})
        self.assertEqual(len(self.history()), snaps + 1)
        p = self.p()
        self.assertEqual(r["rev"], p["rev"])
        self.assertIn({"key": "settings.cut", "value": p["settings"]["cut"]}, r["changes"])
        self.assertEqual(p["settings"]["cut"]["min_silence"], 0.45)
        same = [c for c in p["cuts"] if c["by"] == "auto" and min(c["end"], auto["end"]) - max(c["start"], auto["start"])
                >= 0.5 * (c["end"] - c["start"])]
        self.assertTrue(same)
        self.assertTrue(all(not c["enabled"] and c["reviewed"] for c in same))
        r = self.api.ok("POST", f"/api/p/{self.name}/cuts/detect", {"settings": {"cut": {"pad_before": 0.2}}})
        self.assertEqual(self.p()["settings"]["cut"]["pad_before"], 0.2)
        self.assertEqual(self.api.post(f"/api/p/{self.name}/cuts/detect", {"settings": {"min_silence": "x"}})[0], 400)
        self.assertEqual(self.api.post(f"/api/p/{self.name}/cuts/detect", {"settings": 3})[0], 400)

    def test_06_import_scripts(self):
        old = {t["id"] for t in self.p()["subtitles"]}
        r = self.api.ok("POST", f"/api/p/{self.name}/subs/import", {"text": SRT, "filename": "대본.srt"})
        self.assertEqual(r["info"]["count"], 2)
        subs = self.p()["subtitles"]
        self.assertEqual([t["text"] for t in subs], ["안녕하세요 여러분, 오늘은 성수동 카페", "투어 브이로그입니다"])
        deleted = {c["id"] for c in r["changes"] if c.get("deleted")}
        self.assertEqual(deleted, old - {t["id"] for t in subs})
        self.assertTrue(all(t["by"] == "ui" and t["rev"] == r["rev"] for t in subs))
        r = self.api.ok("POST", f"/api/p/{self.name}/subs/import", {"text": CLOVA, "base": "orig"})
        self.assertGreaterEqual(r["info"]["count"], 2)
        p = self.p()
        self.assertIn("참석자 1", [s["name"] for s in p["speakers"]])
        self.assertTrue(any(c.get("coll") == "speakers" for c in r["changes"]))
        path = Path(tempfile.mkdtemp(prefix="editvdo-script-")) / "대본.srt"
        path.write_text(SRT, encoding="utf-8")
        try:
            r = self.api.ok("POST", f"/api/p/{self.name}/subs/import", {"path": str(path)})
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)
        self.assertEqual(r["info"]["count"], 2)
        self.assertEqual(self.api.post(f"/api/p/{self.name}/subs/import", {"text": "  "})[0], 400)
        self.assertEqual(self.api.post(f"/api/p/{self.name}/subs/import", {"text": SRT, "base": "x"})[0], 400)

    def test_07_split_and_merge(self):
        t = max(self.p()["subtitles"], key=lambda t: t["end"] - t["start"])
        before = {x["id"] for x in self.p()["subtitles"]}
        r = self.api.ok("POST", f"/api/p/{self.name}/subs/split", {"id": t["id"], "t": (t["start"] + t["end"]) / 2})
        (other,) = {x["id"] for x in self.p()["subtitles"]} - before
        ids = {c["item"]["id"] for c in r["changes"] if c.get("coll") == "subtitles" and "item" in c}
        self.assertLessEqual({t["id"], other}, ids)
        r = self.api.ok("POST", f"/api/p/{self.name}/subs/merge", {"ids": [t["id"], other]})
        self.assertIn({"coll": "subtitles", "id": other, "deleted": True}, r["changes"])
        merged = next(x for x in self.p()["subtitles"] if x["id"] == t["id"])
        self.assertEqual(merged["text"].replace(" ", ""), t["text"].replace(" ", ""))
        self.assertEqual(self.api.post(f"/api/p/{self.name}/subs/split", {"id": t["id"], "t": 9999})[0], 400)
        self.assertEqual(self.api.post(f"/api/p/{self.name}/subs/split", {"id": "없음", "t": 1})[0], 400)
        self.assertEqual(self.api.post(f"/api/p/{self.name}/subs/merge", {"ids": [t["id"]]})[0], 400)

    def test_08_generate_from_words(self):
        r = self.api.ok("POST", f"/api/p/{self.name}/subs/generate", {})
        self.assertNotIn("job", r)
        subs = self.p()["subtitles"]
        self.assertGreater(len(subs), 2)
        self.assertTrue(all(t["by"] == "auto" for t in subs))
        self.assertEqual(r["rev"], self.p()["rev"])

    def test_09_reorder_sources(self):
        snaps = len(self.history())
        r = self.api.ok("POST", f"/api/p/{self.name}/sources", {"order": ["s2", "s1"]})
        self.assertEqual(jobs.get(r["job"])["state"], "done")  # 새 preview.mp4 를 만든 뒤 답해요 (UI 가 바로 다시 불러요)
        p = self.p()
        self.assertEqual([(s["id"], s["offset"]) for s in p["sources"]], [("s2", 0.0), ("s1", p["sources"][0]["duration"])])
        self.assertEqual(len(self.history()), snaps + 1)
        self.assertEqual(self.api.post(f"/api/p/{self.name}/sources", {"order": ["s1"]})[0], 400)
        self.assertEqual(self.api.post(f"/api/p/{self.name}/sources", {"nothing": 1})[0], 400)

    def test_10_exports(self):
        p = self.p()
        d1, d2 = (s["duration"] for s in p["sources"])
        self.assertEqual(self.ops([{"op": "put", "coll": "cuts", "item": {"id": "cbig1", "start": 3.0, "end": d1 - 3.0, "type": "user"}},
                                   {"op": "put", "coll": "cuts", "item": {"id": "cbig2", "start": d1 + 3.0, "end": d1 + d2 - 3.0,
                                                                           "type": "user"}}])[0], 200)
        edited = project.edited_duration(self.p())
        out = {}
        for key, body in (("srt", {"kind": "srt"}), ("mp4", {"kind": "mp4", "burn": True}),
                          ("folder", {"kind": "capcut", "name": "서버 테스트"}),
                          ("capcut", {"kind": "capcut", "name": "서버 테스트", "target": "capcut"})):
            r = self.api.ok("POST", f"/api/p/{self.name}/export", body)
            j = self.api.wait(r["job"], 600)
            self.assertEqual((j["state"], j["kind"], j["target"]), ("done", "export", body["kind"]), j)
            out[key] = j["result"]
            self.assertTrue(Path(j["result"]["path"]).exists(), j["result"])
        srt = out["srt"]
        self.assertTrue(srt["dir"].startswith("srt-") and srt["url"].endswith("/final.srt"))
        st, h, body = self.api.get(srt["url"], raw=True)
        self.assertEqual(st, 200)
        self.assertIn("-->", body.decode("utf-8"))
        mp4 = out["mp4"]
        st, h, body = self.api.get(mp4["url"], headers={"Range": "bytes=0-1023"}, raw=True)
        self.assertEqual((st, len(body), h["content-type"]), (206, 1024, "video/mp4"))
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    mp4["path"]], capture_output=True, text=True).stdout)
        self.assertAlmostEqual(dur, edited, delta=0.15)
        moved = Path(out["folder"]["path"])  # 기본: 다른 맥으로 옮길 폴더 (영상까지 안에)
        self.assertEqual({k: out["folder"][k] for k in ("kind", "target", "name")},
                         {"kind": "capcut", "target": "folder", "name": "서버 테스트"})
        self.assertEqual(moved.parent.parent, project.project_dir(self.name) / "exports")
        self.assertTrue(moved.parent.name.startswith("capcut-") and len(list((moved / "media").iterdir())) == 2)
        folder = Path(out["capcut"]["path"])
        self.assertEqual((out["capcut"]["target"], out["capcut"]["name"], folder.parent), ("capcut", "서버 테스트", self.root))
        listed = json.loads((self.root / "root_meta_info.json").read_text(encoding="utf-8"))["all_draft_store"]
        self.assertEqual([e["draft_fold_path"] for e in listed], [str(folder)])  # folder 는 캡컷 목록에 안 올려요
        with mock.patch.object(capcut, "capcut_running", return_value=True):
            r = self.api.ok("POST", f"/api/p/{self.name}/export", {"kind": "capcut", "target": "capcut"})
            j = self.api.wait(r["job"], 60)
            self.assertEqual((j["state"], j["message"]), ("error", capcut.CLOSE_FIRST))
            r = self.api.ok("POST", f"/api/p/{self.name}/export", {"kind": "capcut"})  # 다른 맥으로는 켜져 있어도 돼요
            j = self.api.wait(r["job"], 600)
            self.assertEqual((j["state"], j["result"]["target"]), ("done", "folder"), j)
        self.assertEqual(self.api.post(f"/api/p/{self.name}/export", {"kind": "capcut", "target": "cloud"})[0], 400)
        self.assertEqual(self.api.post(f"/api/p/{self.name}/export", {"kind": "gif"})[0], 400)
        self.assertEqual(self.api.post(f"/api/p/{self.name}/export", {"kind": "mp4", "burn": "yes"})[0], 400)

    def test_11_reanalyze_and_remove(self):
        r = self.api.ok("POST", f"/api/p/{self.name}/sources", {"analyze": "s2", "transcribe": False})
        j = self.api.wait(r["job"], 600)
        self.assertEqual((j["state"], j["sids"], j["transcribe"]), ("done", ["s2"], False), j)
        self.assertEqual(next(s for s in self.p()["sources"] if s["id"] == "s2")["status"], "ready")
        self.assertEqual(self.api.post(f"/api/p/{self.name}/sources", {"analyze": "s9"})[0], 400)
        r = self.api.ok("POST", f"/api/p/{self.name}/sources", {"remove": "s1"})
        self.assertEqual(self.api.wait(r["job"], 300)["state"], "done")
        p = self.p()
        self.assertEqual([(s["id"], s["offset"]) for s in p["sources"]], [("s2", 0.0)])
        self.assertTrue(all(c["end"] <= p["sources"][0]["duration"] + 1e-6 for c in p["cuts"]))

    def test_12_add_source(self):
        r = self.api.ok("POST", f"/api/p/{self.name}/sources", {"add": [str(self.clips[0])]})
        self.assertEqual(r["sids"], ["s3"])  # 지운 소스 id 는 다시 쓰지 않아요
        j = self.api.wait(r["job"])
        self.assertEqual((j["state"], j["sids"]), ("done", ["s3"]), j)
        self.assertEqual([(s["id"], s["status"]) for s in self.p()["sources"]], [("s2", "ready"), ("s3", "ready")])
        self.assertEqual(self.api.post(f"/api/p/{self.name}/sources", {"add": ["/없는/영상.mov"]})[0], 400)


if __name__ == "__main__":
    unittest.main()

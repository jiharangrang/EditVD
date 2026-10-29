"""편집 화면(ui/)과 API v2 를 이 컴퓨터 안에서만(127.0.0.1) 제공해요.
모든 요청은 Host 가 localhost·127.0.0.1:<포트> 여야 하고, POST 는 JSON 이면서 같은 출처(Origin)여야 해요.
오류는 {"error": "한국어 메시지"} 와 400·403·404·409·413·415·500 으로 돌려줘요."""
import json
import os
import re
import stat
import subprocess
import traceback
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import jobs, project

UI = project.ROOT / "ui"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon", ".woff2": "font/woff2",
         ".json": "application/json; charset=utf-8", ".mp4": "video/mp4", ".srt": "application/x-subrip; charset=utf-8"}
SOURCE_FILES = ("proxy.mp4", "peaks.json", "thumbs.jpg", "thumbs.json", "silences.json")
MEDIA = re.compile(r"/media/([^/]+)/(preview\.mp4|cover\.(?:jpg|json)|sources/s\d+/(?:{})|exports/[A-Za-z0-9_-]+/[^/]+\.(?:mp4|srt))".format(
    "|".join(map(re.escape, SOURCE_FILES))))
ORIGINAL = re.compile(r"/media/([^/]+)/sources/(s\d+)/original")
ACTION = re.compile(r"/api/p/([^/]+)/(ops|sources|cuts/detect|subs/generate|subs/import|subs/split|subs/merge|export)")
MAX_BODY = 32 << 20
PREVIEW_WAIT = 30  # 순서 바꾸기·빼기 뒤 preview.mp4 다시 잇기를 이만큼(초)까지 기다렸다가 답해요
SCAN_DIRS = ("Desktop", "Downloads", "Movies", "Documents", "Pictures")
PICK_SCRIPT = """on run argv
activate
if item 1 of argv is "video" then
set picked to choose file with prompt "영상 고르기" of type {"public.movie"} with multiple selections allowed
else
set picked to choose file with prompt "대본 고르기" with multiple selections allowed
end if
set out to ""
repeat with f in picked
set out to out & POSIX path of f & linefeed
end repeat
return out
end run"""


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    server_version = "EditVDO/2"

    def log_message(self, *args):
        pass  # 1초마다 오는 /rev 같은 요청은 남기지 않아요

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def _serve(self, method):
        try:
            self._check(method)
            path = unquote(urlsplit(self.path).path)
            if method == "GET":
                self._get(path)
            else:
                self._post(path, self._body())
        except ApiError as e:
            self._json({"error": str(e)}, e.status)
        except FileNotFoundError as e:
            self._json({"error": "파일을 찾을 수 없어요" if e.errno else str(e)}, 404)
        except ValueError as e:
            self._json({"error": str(e) or "요청을 확인해 주세요"}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self._json({"error": "서버에서 요청을 처리하지 못했어요"}, 500)

    def _check(self, method):
        """다른 사이트·다른 이름(DNS rebinding)으로 오는 요청을 막아요."""
        port = self.server.server_port
        hosts = (f"localhost:{port}", f"127.0.0.1:{port}")
        if self.headers.get("Host", "") not in hosts:
            raise ApiError(403, "이 컴퓨터의 편집 화면에서만 쓸 수 있어요")
        if method == "POST":
            origin = self.headers.get("Origin")
            if origin is not None and origin not in [f"http://{h}" for h in hosts]:
                raise ApiError(403, "이 컴퓨터의 편집 화면에서만 쓸 수 있어요")
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                raise ApiError(415, "JSON 요청이 필요해요")

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "요청 크기를 확인해 주세요") from None
        if not 0 <= n <= MAX_BODY:
            raise ApiError(413, "요청이 너무 커요")
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise ApiError(400, "JSON 형식이 올바르지 않아요") from None
        if not isinstance(data, dict):
            raise ApiError(400, "요청은 JSON 객체여야 해요")
        return data

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _get(self, path):
        if path in ("/", "/index.html"):
            return self._file(UI / "index.html")
        if path.startswith("/ui/"):
            return self._file(_inside(UI, path[4:]))
        if path == "/api/projects":
            return self._json(list_projects())
        if m := re.fullmatch(r"/api/p/([^/]+)", path):
            return self._json(open_project(_name(m[1])))
        if m := re.fullmatch(r"/api/p/([^/]+)/rev", path):
            return self._json(rev(_name(m[1])))
        if m := re.fullmatch(r"/api/p/([^/]+)/words", path):
            q = parse_qs(urlsplit(self.path).query)
            return self._json(words_in(_name(m[1]), _arg(q, "from", 0.0), _arg(q, "to", 1e12)))
        if m := re.fullmatch(r"/api/jobs/([A-Za-z0-9]+)", path):
            if job := jobs.get(m[1]):
                return self._json(job)
            raise ApiError(404, "작업이 없어요")
        if m := MEDIA.fullmatch(path):
            d = project.project_dir(_name(m[1]))
            return self._file(_inside(d, m[2]))
        if m := ORIGINAL.fullmatch(path):  # 원본 그대로 재생: project.json 에 적힌 그 파일만 보내요
            src = next((s for s in project.load(_name(m[1]))["sources"] if s["id"] == m[2]), None)
            if not src:
                raise ApiError(404, "영상이 없어요")
            return self._file(src["path"], "video/mp4")
        raise ApiError(404, "찾을 수 없어요")

    def _post(self, path, body):
        if path == "/api/projects":
            return self._json(create_project(body))
        if path == "/api/pick":
            return self._json({"paths": pick(body.get("kind", "video"))})
        if path == "/api/resolve":
            return self._json({"paths": resolve(body.get("files"))})
        if path == "/api/open":
            return self._json(open_path(body))
        if m := ACTION.fullmatch(path):
            return self._json(ACTIONS[m[2]](_name(m[1]), body))
        raise ApiError(404, "찾을 수 없어요")

    def _file(self, path, ctype=None):
        """파일을 보내요. Range(206)·ETag(304) 를 지원하고 캐시는 늘 다시 확인하게(no-cache) 해요."""
        try:
            f = open(path, "rb")
        except OSError:
            raise ApiError(404, "파일이 없어요") from None
        with f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise ApiError(404, "파일이 없어요")
            size, tag = st.st_size, f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
            if self.headers.get("If-None-Match") == tag:
                self.send_response(304)
                self.send_header("ETag", tag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            start, end, status = 0, size - 1, 200
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            if m and (m[1] or m[2]):
                if m[1]:
                    start = int(m[1])
                    end = min(int(m[2]), size - 1) if m[2] else size - 1
                else:
                    start = max(0, size - int(m[2]))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", ctype or TYPES.get(Path(path).suffix.lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", tag)
            self.send_header("Cache-Control", "no-cache")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            f.seek(start)
            left = end - start + 1
            while left > 0 and (chunk := f.read(min(1 << 20, left))):
                self.wfile.write(chunk)
                left -= len(chunk)


def _name(raw):
    try:
        return project.check_name(raw)
    except ValueError:
        raise ApiError(404, "프로젝트가 없어요") from None


def _inside(base, rel):
    """base 안의 파일 경로만 돌려줘요 (.. · 숨김 파일 · 밖을 가리키는 링크는 거절)."""
    parts = rel.split("/")
    if any(not x or x.startswith(".") for x in parts):
        raise ApiError(404, "찾을 수 없어요")
    path = (base / rel).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ApiError(404, "찾을 수 없어요")
    return path


def _arg(q, key, default):
    try:
        v = float(q[key][0]) if q.get(key) and q[key][0] != "" else default
    except ValueError:
        raise ValueError(f"{key} 는 숫자(초)여야 해요") from None
    if v != v:
        raise ValueError(f"{key} 는 숫자(초)여야 해요")
    return v


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _mtime(path):
    try:
        return f"{os.stat(path).st_mtime_ns:x}"
    except OSError:
        return "0"


def _url(name, rel):
    """있는 파일이면 /media 주소 (?v=수정 시각, 다시 만들면 주소가 바뀌어요), 없으면 None."""
    try:
        st = (project.project_dir(name) / rel).stat()
    except OSError:
        return None
    return f"/media/{quote(name)}/{rel}?v={st.st_mtime_ns:x}"


# ---------- 읽기 ----------

def list_projects():
    """홈 목록 (+ path: 프로젝트 폴더 절대 경로, Finder 에서 보기용)."""
    out = project.list_projects()
    for x in out:
        x["path"] = str(project.project_dir(x["name"]))
    return out


def open_project(name):
    """프로젝트를 열어요: v1 이면 v2 로 옮기고, 서버가 다시 켜져 멈춘 분석을 정리하고, 빠진 썸네일 작업을 넣어요.
    {project, words_count, assets:{sid:{proxy, peaks, thumbs, thumbs_meta, silences}}, preview} — 없는 파일은 null."""
    project.migrate_v1(name)  # 이미 v2 면 아무것도 안 해요. 프로젝트가 아예 없으면 FileNotFoundError
    jobs.recover(name)
    p = project.load(name)
    jobs.ensure_thumbs(name, p)
    assets = {}
    for s in p["sources"]:
        rel = f"sources/{s['id']}/"
        meta = _read_json(project.project_dir(name) / rel / "thumbs.json")
        thumbs = _url(name, rel + "thumbs.jpg") if meta else None
        proxy = _url(name, rel + "proxy.mp4")
        play = proxy or (f"/media/{quote(name)}/{rel}original?v={_mtime(s['path'])}" if s.get("play") == "original" else None)
        assets[s["id"]] = {"proxy": proxy, "play": play, "peaks": _url(name, rel + "peaks.json"),
                           "thumbs": thumbs, "thumbs_meta": meta if thumbs else None,
                           "silences": _url(name, rel + "silences.json")}
    return {"project": p, "words_count": len(_words(name, p)), "assets": assets, "preview": _url(name, "preview.mp4")}


_WORDS, _REV = {}, {}


def _words(name, p):
    """소스들의 음성 인식 단어를 원본 시간축으로 (transcript.json 이 바뀌거나 순서가 바뀔 때만 다시 읽어요)."""
    from . import analyze
    d = project.project_dir(name)
    key = []
    for s in p["sources"]:
        try:
            key.append((s["id"], s["offset"], (d / "sources" / s["id"] / "transcript.json").stat().st_mtime_ns))
        except OSError:
            key.append((s["id"], s["offset"], 0))
    hit = _WORDS.get(name)
    if not hit or hit[0] != key:
        hit = _WORDS[name] = (key, analyze.words(p))
    return hit[1]


def words_in(name, lo, hi):
    """[lo, hi] 와 겹치는 단어 [{start, end, word, sid}] (원본 시간)."""
    return [w for w in _words(name, project.load(name)) if w["end"] >= lo and w["start"] <= hi]


def rev(name):
    """{rev, by, jobs} — 1초마다 불려서 project.json 이 바뀌었을 때만 다시 읽어요."""
    path = project.project_dir(name) / "project.json"
    try:
        st = path.stat()
        key = (st.st_ino, st.st_mtime_ns, st.st_size)
        hit = _REV.get(name)
        if not hit or hit[0] != key:
            p = json.loads(path.read_text(encoding="utf-8"))
            hit = _REV[name] = (key, p.get("rev", 0), p.get("by", ""))
    except FileNotFoundError:
        raise ApiError(404, "프로젝트가 없어요") from None
    return {"rev": hit[1], "by": hit[2], "jobs": jobs.active(name)}


# ---------- 바꾸기 ----------

def create_project(body):
    """{title?, paths[], name?} → 프로젝트를 만들고 분석 작업을 넣어요. {name, job}"""
    title = body.get("title")
    if title is not None and not isinstance(title, str):
        raise ValueError("제목은 글자여야 해요")
    name = project.create(title, body.get("paths"), name=body.get("name"))
    job = jobs.analyze(name, [s["id"] for s in project.load(name)["sources"]])
    return {"name": name, "job": job["id"]}


def ops(name, body):
    """{rev, ops[]} → {rev, changes}. 그 사이 다른 곳(Claude·분석)이 바꿨어도 최신 상태 위에 적용해요 (필드 단위로 합쳐져요).
    UI 가 모르는 사이 지워진 항목을 일부 필드로만 고치는 put 은 버리고 '지워짐' 기록으로 알려요.
    UI 의 rev 가 서버보다 앞서면(프로젝트가 통째로 바뀜) 409."""
    items, base = body.get("ops"), body.get("rev")
    if not isinstance(items, list):
        raise ValueError("ops 는 목록이어야 해요")
    if base is not None and (isinstance(base, bool) or not isinstance(base, int)):
        raise ValueError("rev 는 정수여야 해요")
    if not items:
        return {"rev": project.load(name)["rev"], "changes": []}

    def fn(p):
        if base is not None and base > p["rev"]:
            raise ApiError(409, "프로젝트가 다른 곳에서 바뀌었어요. 새로 불러올게요")
        keep, gone = [], []
        for op in items:
            if base is not None and base < p["rev"] and _gone(p, op):
                gone.append({"coll": op["coll"], "id": op["item"]["id"], "deleted": True})
            else:
                keep.append(op)
        return project.apply_ops(p, keep, "ui") + gone
    p, changes = project.transact(name, fn, by="ui")
    return {"rev": p["rev"], "changes": changes}


def _gone(p, op):
    """다른 곳에서 지워진 항목을 일부 필드만으로 고치려는 put 인지."""
    if not isinstance(op, dict) or op.get("op") != "put" or op.get("coll") not in ("cuts", "subtitles", "comments"):
        return False
    item = op.get("item")
    if not isinstance(item, dict) or item.get("id") is None or any(x["id"] == item["id"] for x in p[op["coll"]]):
        return False
    return not ({"t"} if op["coll"] == "comments" else {"start", "end"}) <= item.keys()


def sources(name, body):
    """{add:[경로]} 더하기 · {order:[sid]} 순서 · {remove:sid} 빼기 · {analyze:sid|[sid], transcribe?} 다시 분석 → {rev, job}"""
    if "add" in body:
        with jobs.pending(name):
            p, sids = project.transact(name, lambda p: project.add_sources(p, body["add"]), by="ui")
            job = jobs.analyze(name, sids)
        return {"rev": p["rev"], "job": job["id"], "sids": sids}
    if "order" in body or "remove" in body:
        if "order" in body:
            p, _ = project.transact(name, lambda p: project.reorder_sources(p, body["order"]), by="ui", snapshot="영상 순서")
        else:
            p, _ = project.transact(name, lambda p: project.remove_source(p, body["remove"]), by="ui", snapshot="영상 빼기")
        job = jobs.preview(name)
        jobs.wait(job["id"], PREVIEW_WAIT)  # 보통 몇 초라서 새 preview.mp4 를 만든 뒤 답해요 (UI 가 바로 다시 불러요)
        return {"rev": p["rev"], "job": job["id"]}
    if "analyze" in body:
        want, transcribe = body["analyze"], body.get("transcribe", True)
        want = [want] if isinstance(want, str) else want
        if not isinstance(want, list) or not want or not all(isinstance(x, str) for x in want) \
                or not isinstance(transcribe, bool):
            raise ValueError("analyze 에는 영상 id(들)를 보내 주세요")

        def fn(p):
            ids = {s["id"] for s in p["sources"]}
            if missing := [x for x in want if x not in ids]:
                raise ValueError(f"없는 영상이에요: {missing[0]}")
            for s in p["sources"]:
                if s["id"] in want:
                    s.update(status="queued", progress=0.0, step="", error="")
        with jobs.pending(name):
            p, _ = project.transact(name, fn, by="ui")
            job = jobs.analyze(name, want, transcribe)
        return {"rev": p["rev"], "job": job["id"]}
    raise ValueError("add · order · remove · analyze 중 하나를 보내 주세요")


def detect_cuts(name, body):
    """{settings?} (컷 설정 일부, 또는 {cut:{…}}) → 자동 컷을 다시 찾아요. {rev, changes}"""
    settings = body.get("settings")
    if isinstance(settings, dict) and isinstance(settings.get("cut"), dict):
        settings = settings["cut"]
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("settings 는 객체여야 해요")
    p, changes = jobs.detect(name, "ui", settings)
    return {"rev": p["rev"], "changes": changes}


def generate_subs(name, body):
    """음성 인식으로 자막을 새로 만들어요. 인식이 빠진 소스가 있으면 인식 작업부터 넣고 {job}."""
    missing = jobs.missing_words(project.load(name))
    if missing:
        return {"job": jobs.transcribe(name, missing, then_build=True)["id"]}
    p, changes = jobs.build_subs(name, "ui")
    return {"rev": p["rev"], "changes": changes}


def import_subs(name, body):
    """{text | path, filename?, base?, replace?} → {rev, changes, info}"""
    text = body.get("text")
    if text is None and isinstance(body.get("path"), str):
        text = jobs.read_text(body["path"])
    p, changes, info = jobs.import_subs(name, text, "ui", body.get("base") or "auto", body.get("replace", True))
    return {"rev": p["rev"], "changes": changes, "info": info}


def split_sub(name, body):
    p, changes = jobs.split_sub(name, body.get("id"), body.get("t"), "ui")
    return {"rev": p["rev"], "changes": changes}


def merge_subs(name, body):
    p, changes = jobs.merge_subs(name, body.get("ids"), "ui")
    return {"rev": p["rev"], "changes": changes}


def export(name, body):
    """{kind: capcut|mp4|srt, target?: folder|capcut, name?, burn?, include_subs?, copy_media?, fallback?} → {job}
    캡컷의 '자막 포함'은 include_subs (없으면 burn 을 같은 뜻으로 받아요). 캡컷 target 기본값은 folder
    (프로젝트 exports/ 에 영상까지 담은 폴더, 다른 맥으로 옮기기), capcut 은 이 맥 캡컷에 바로 넣어요.
    작업 결과(result)는 {kind: "capcut", target, path, name}."""
    def flag(key, default=True):
        v = body.get(key, default)
        if not isinstance(v, bool):
            raise ValueError(f"{key} 는 true/false 여야 해요")
        return v
    title = body.get("name")
    if title is not None and (not isinstance(title, str) or len(title) > 100):
        raise ValueError("이름은 100자 이하 글자여야 해요")
    burn = flag("burn")
    job = jobs.export(name, body.get("kind", "capcut"), title=(title or "").strip() or None, burn=burn,
                      include_subs=flag("include_subs", burn), copy_media=flag("copy_media"), fallback=flag("fallback", False),
                      target=body.get("target") or "folder")
    return {"job": job["id"]}


ACTIONS = {"ops": ops, "sources": sources, "cuts/detect": detect_cuts, "subs/generate": generate_subs,
           "subs/import": import_subs, "subs/split": split_sub, "subs/merge": merge_subs, "export": export}


# ---------- macOS (파일 고르기·찾기·Finder) ----------

def _osascript(script, *args):
    """AppleScript 를 실행해요. 사용자 값은 스크립트 글자에 끼워 넣지 않고 argv 로만 넘겨요."""
    try:
        return subprocess.run(["osascript", "-e", script, *args], capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([], 1, "", "(-128)")


def pick(kind):
    """macOS 파일 고르기 창 (여러 개). 취소하면 []."""
    if kind not in ("video", "script"):
        raise ValueError("kind 는 video 또는 script 예요")
    r = _osascript(PICK_SCRIPT, kind)
    if r.returncode:
        if "-128" in r.stderr:
            return []
        raise ValueError("파일 고르기 창을 열지 못했어요")
    return [x for x in r.stdout.splitlines() if x.strip() and Path(x).is_file()]


def _mdfind(name):
    try:
        return subprocess.run(["mdfind", "-name", name], capture_output=True, text=True, timeout=10).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return []


def _scan(name, depth=3, limit=50000):
    """Spotlight 가 못 찾으면 자주 쓰는 폴더와 외장 디스크를 얕게 뒤져요."""
    roots = [Path.home() / x for x in SCAN_DIRS] + [v for v in sorted(Path("/Volumes").glob("*")) if not v.is_symlink()]
    out, seen = [], [0]

    def walk(d, left):
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen[0] += 1
                    if seen[0] > limit or e.name.startswith(".") or e.name in ("Library", "CapCut"):
                        continue
                    if e.is_file(follow_symlinks=False):
                        if unicodedata.normalize("NFC", e.name) == name:
                            out.append(e.path)
                    elif left and e.is_dir(follow_symlinks=False):
                        walk(e.path, left - 1)
        except OSError:
            pass
    for r in roots:
        walk(r, depth)
    return out


def _rank(path):
    """원본일 것 같은 순서: 다른 곳 → 이 앱 폴더 → 캡컷 초안(내보낸 복사본) → 휴지통."""
    return next((i for i, x in ((3, "/.Trash/"), (2, "/Movies/CapCut/"), (1, str(project.ROOT) + os.sep)) if x in path), 0)


def _find(name, size, mtime):
    for search in (_mdfind, _scan):
        good = []
        for path in search(name):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and unicodedata.normalize("NFC", os.path.basename(path)) == name \
                    and (not isinstance(size, int) or st.st_size == size):
                good.append((path, st))
        if good:
            near = isinstance(mtime, (int, float)) and not isinstance(mtime, bool)
            return min(good, key=lambda x: (_rank(x[0]), abs(x[1].st_mtime - mtime) if near else 0))[0]
    return None


def resolve(files):
    """창에 끌어다 놓은 파일({name, size, mtime})의 실제 경로를 이름·크기로 찾아요. 못 찾은 파일은 빠져요."""
    if not isinstance(files, list):
        raise ValueError("files 목록이 필요해요")
    out = []
    for f in files[:100]:
        if not isinstance(f, dict) or not isinstance(f.get("name"), str):
            continue
        name = unicodedata.normalize("NFC", f["name"]).strip()
        if name and "/" not in name and name not in (".", "..") and (path := _find(name, f.get("size"), f.get("mtime"))):
            out.append(path)
    return list(dict.fromkeys(out))


def _open(args):
    """macOS open (경로는 인자로만 넘겨요). 실패하면 0 이 아닌 값."""
    try:
        return subprocess.run(["open", *args], capture_output=True, timeout=15).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def open_path(body):
    """{path} → Finder 에서 보기 (상대 경로는 앱 폴더 기준, 예: projects/<이름>) · {app: "CapCut"} → 캡컷 열기."""
    if "app" in body:
        if body["app"] != "CapCut":
            raise ValueError("열 수 있는 앱은 CapCut 뿐이에요")
        if _open(["-a", "CapCut"]):
            raise ApiError(404, "캡컷을 찾을 수 없어요")
        return {"ok": True}
    raw = body.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("경로가 필요해요")
    path = Path(raw).expanduser()
    path = Path(os.path.abspath(path if path.is_absolute() else project.ROOT / path))
    if not path.exists():
        raise ApiError(404, "파일을 찾을 수 없어요")
    if _open(["-R", str(path)]):
        raise ValueError("Finder 에서 열지 못했어요")
    return {"ok": True}


# ---------- 켜기 ----------

def make_server(port=8765):
    """서버를 만들어요 (port=0 이면 빈 포트). 띄우기는 serve_forever()."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    return srv


def serve(port=8765):
    """이 컴퓨터에서만 열리는 편집 서버를 켜요. Ctrl+C 로 끄면 도는 분석도 바로 멈춰요 (다음에 열 때 '중단됨')."""
    srv = make_server(port)
    print(f"편집기: http://localhost:{srv.server_port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    os._exit(0)

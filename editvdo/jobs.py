"""오래 걸리는 작업(분석·음성 인식·내보내기·썸네일·미리보기)을 백그라운드에서 돌리고,
UI(server.py)와 CLI(ev.py)가 같이 쓰는 묶음 편집(컷 다시 찾기·자막 만들기·대본 불러오기·나누기·합치기)을 맡아요.
작업은 메모리에만 있어요: {id, project, kind, state: queued|running|done|error, progress 0..1, message, result}.
분석·인식(whisper)은 무거워서 전체에서 한 번에 하나씩, 나머지는 두 개까지 같이 돌아요."""
import copy
import fcntl
import itertools
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from . import project

HEAVY = ThreadPoolExecutor(1, thread_name_prefix="ev-heavy")  # 분석·인식
LIGHT = ThreadPoolExecutor(2, thread_name_prefix="ev-light")  # 내보내기·썸네일·미리보기
LOCK = threading.Lock()
JOBS = {}      # id → 작업 (밑줄로 시작하는 키는 안에서만 써요)
PENDING = {}   # 프로젝트 → 소스를 queued 로 바꾸고 작업을 넣는 중인 요청 수
TRIED = set()  # 썸네일을 만들어 본 (프로젝트, 소스) — 실패해도 서버가 켜져 있는 동안 다시 하지 않아요
ACTIVE = ("queued", "running")
SHOW_DONE = 30  # 끝난 작업도 이 시간(초) 동안 /rev 에 보여요 (UI 가 끝난 걸 알아채게)
COLLS = ("cuts", "subtitles", "speakers", "comments")
KEYS = ("title", "notes", "splits", "settings.cut", "settings.subs", "settings.asr")
EXPORTS = ("capcut", "mp4", "srt")


# ---------- 작업 ----------

def submit(name, kind, fn, heavy=False, **extra):
    """작업을 등록하고 차례가 오면 fn(progress) 를 실행해요 (반환값 = result). progress(비율, 글) 로 진행을 알려요."""
    job = {"id": uuid.uuid4().hex[:12], "project": name, "kind": kind, "state": "queued", "progress": 0.0,
           "message": "", "result": None, **extra, "_done": 0.0, "_end": threading.Event()}
    with LOCK:
        _prune()
        JOBS[job["id"]] = job
        out = _public(job)
    (HEAVY if heavy else LIGHT).submit(_run, job, fn)
    return out


def _run(job, fn):
    def progress(frac=None, message=None):
        with LOCK:
            if frac is not None:
                job["progress"] = round(min(max(float(frac), 0.0), 1.0), 3)
            if message:
                job["message"] = str(message)
    _update(job, state="running")
    try:
        result = fn(progress)
    except KeyboardInterrupt:
        _update(job, state="error", message="중단됨")
        raise
    except BaseException as e:  # SystemExit 같은 것도 작업 오류로 남기고 서버는 계속 돌아요
        _trace(e)
        _update(job, state="error", message=error_text(e))
    else:
        _update(job, state="done", progress=1.0, message="", result=result)


def _update(job, **fields):
    with LOCK:
        job.update(fields)
        if job["state"] not in ACTIVE:
            job["_done"] = time.time()
            job["_end"].set()


def _prune(limit=200):
    old = sorted((j["_done"], i) for i, j in JOBS.items() if j["state"] not in ACTIVE)
    for _, i in old[:max(0, len(JOBS) + 1 - limit)]:
        del JOBS[i]


def _public(job):
    return {k: copy.deepcopy(v) for k, v in job.items() if not k.startswith("_")}


def _trace(e):
    """뜻밖의 오류만 터미널에 자세히 남겨요 (사용자용 오류는 메시지로 충분해요)."""
    if not isinstance(e, (ValueError, RuntimeError, OSError)):
        traceback.print_exception(e)


def error_text(e):
    """사용자에게 보여 줄 오류 문장."""
    if isinstance(e, (ValueError, RuntimeError, OSError)) and str(e):
        return str(e)
    return f"작업 중 오류가 생겼어요 ({type(e).__name__})"


def get(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        return _public(job) if job else None


def wait(job_id, timeout=None):
    """작업이 끝나거나 timeout(초)이 지날 때까지 기다려요. 작업 사본 (없는 작업이면 None)."""
    with LOCK:
        job = JOBS.get(job_id)
    if job:
        job["_end"].wait(timeout)
    return get(job_id)


def active(name):
    """프로젝트의 진행 중인 작업과 방금 끝난 작업 [{id, kind, state, progress, message, …}] (/rev 용)."""
    now = time.time()
    with LOCK:
        return [{k: v for k, v in _public(j).items() if k not in ("project", "result")} for j in JOBS.values()
                if j["project"] == name and (j["state"] in ACTIVE or now - j["_done"] < SHOW_DONE)]


def busy(name, kinds=None):
    """프로젝트에 기다리거나 도는 작업이 있는지 (kinds 로 종류를 좁혀요)."""
    with LOCK:
        return any(j["project"] == name and j["state"] in ACTIVE and (kinds is None or j["kind"] in kinds)
                   for j in JOBS.values())


# ---------- 분석 ----------

@contextmanager
def analysis_lock(name, wait=True):
    """프로젝트 분석 잠금(analyze.lock). 분석하는 쪽(서버 작업·ev.py)이 잡고 있어서, 서버가 다시 켜졌을 때
    멈춘 분석인지 다른 프로세스가 아직 하는 중인지 가려요. wait=False 면 기다리지 않고 잡았는지(bool)를 넘겨요."""
    with open(project.project_dir(name) / "analyze.lock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            got = True
        except BlockingIOError:
            got = False
        try:
            yield got
        finally:
            if got:
                fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def pending(name):
    """소스를 queued 로 바꾸고 작업을 넣기까지 recover 가 끼어들지 않게 해요."""
    with LOCK:
        PENDING[name] = PENDING.get(name, 0) + 1
    try:
        yield
    finally:
        with LOCK:
            PENDING[name] -= 1
            if not PENDING[name]:
                del PENDING[name]


def recover(name):
    """서버가 꺼졌다 켜져서 멈춘 분석(analyzing·queued 인데 도는 작업이 없음)을 '중단됨' 오류로 바꿔요.
    ev.py 가 분석 중이면 분석 잠금이 잡혀 있어서 건드리지 않아요. 바꾼 소스 id 를 돌려줘요."""
    p = project.load(name)
    stuck = {s["id"] for s in p["sources"] if s["status"] in ("queued", "analyzing")}
    if not stuck or PENDING.get(name) or busy(name, ("analyze", "transcribe")):
        return []
    with analysis_lock(name, wait=False) as got:
        if not got:
            return []

        def fn(p):
            out = []
            for s in p["sources"]:
                if s["id"] in stuck and s["status"] in ("queued", "analyzing"):
                    s.update(status="error", error="중단됨", step="")
                    out.append(s["id"])
            return out
        return project.transact(name, fn, by="job")[1]


def _step(report, i, n, sid):
    """analyze 의 진행 알림 on_progress(단계 글, 소스 안 비율) → report(전체 비율, 단계, 소스)."""
    def step(*args):
        frac = next((x for x in args if isinstance(x, (int, float)) and not isinstance(x, bool)), None)
        report(None if frac is None else (i + min(max(float(frac), 0.0), 1.0)) / n,
               next((x for x in args if isinstance(x, str)), None), sid)
    return step


def run_analysis(name, sids, transcribe=True, on_progress=None, words_only=False, lock=True):
    """소스들을 차례로 분석해요 (words_only 면 음성 인식만). CLI 는 그 자리에서, 서버는 작업 안에서 불러요.
    on_progress(전체 비율, 단계, 소스 id). 한 소스가 실패해도 나머지는 계속하고, 실패한 소스는 status=error 로 남겨요.
    실패가 있으면 끝에 ValueError. lock=False 는 부르는 쪽이 이미 analysis_lock 을 잡은 경우예요."""
    from . import analyze
    report = on_progress or (lambda *a: None)
    errors = []
    with analysis_lock(name) if lock else _nothing():
        for i, sid in enumerate(sids):
            if not any(s["id"] == sid for s in project.load(name)["sources"]):
                continue  # 그 사이 빠진 소스
            step = _step(report, i, len(sids), sid)
            try:
                if words_only:
                    analyze.transcribe_source(name, sid, step)
                else:
                    analyze.analyze_source(name, sid, step, transcribe=transcribe)
            except KeyboardInterrupt:
                _fail(name, sid, "중단됨")
                raise
            except BaseException as e:
                _trace(e)
                errors.append(f"{sid}: {error_text(e)}")
                _fail(name, sid, error_text(e))
            report((i + 1) / len(sids), None, sid)
    if errors:
        raise ValueError(errors[0] + (f" 외 {len(errors) - 1}개" if len(errors) > 1 else ""))


@contextmanager
def _nothing():
    yield True


def _fail(name, sid, message):
    """analyze 가 미처 남기지 못한 실패만 status=error 로 남겨요."""
    def fn(p):
        for s in p["sources"]:
            if s["id"] == sid and s["status"] != "error":
                s.update(status="error", error=message[:300], step="")
    try:
        project.transact(name, fn, by="job")
    except (OSError, ValueError):
        pass  # 프로젝트가 지워졌으면 남길 곳이 없어요


def analyze(name, sids, transcribe=True):
    """소스 분석 작업을 넣어요. 이미 기다리는 분석 작업에 든 소스는 빼고, 다 들어 있으면 그 작업을 돌려줘요."""
    sids = list(dict.fromkeys(sids))
    if not sids:
        raise ValueError("분석할 영상이 없어요")
    with LOCK:
        waiting = [j for j in JOBS.values() if j["project"] == name and j["kind"] == "analyze"
                   and j["state"] == "queued" and j["transcribe"] == transcribe]
        todo = [sid for sid in sids if not any(sid in j["sids"] for j in waiting)]
        if not todo:
            return _public(next(j for j in waiting if sids[0] in j["sids"]))

    def work(progress):
        run_analysis(name, todo, transcribe, lambda f, msg, sid: progress(f, msg))
    return submit(name, "analyze", work, heavy=True, sids=todo, transcribe=transcribe)


def missing_words(p):
    """분석은 끝났는데 음성 인식이 없는 (소리가 있는) 소스 id."""
    d = project.project_dir(p["name"])
    return [s["id"] for s in p["sources"] if s["status"] == "ready" and s.get("has_audio", True)
            and not (d / "sources" / s["id"] / "transcript.json").exists()]


def transcribe(name, sids, then_build=False):
    """음성 인식만 하는 작업. then_build 면 끝나고 자막도 새로 만들어요. 도는 인식 작업이 있으면 그걸 돌려줘요."""
    with LOCK:
        for j in JOBS.values():
            if j["project"] == name and j["kind"] == "transcribe" and j["state"] in ACTIVE:
                return _public(j)

    def work(progress):
        run_analysis(name, sids, on_progress=lambda f, msg, sid: progress(None if f is None else f * 0.95, msg),
                     words_only=True)
        if then_build:
            build_subs(name, "job")
    return submit(name, "transcribe", work, heavy=True, sids=list(sids))


# ---------- 썸네일·미리보기 ----------

def make_thumbs(name, sids, on_progress=None):
    """분석을 다시 하지 않고 필름스트립 썸네일만 만들어요 (v1 에서 옮긴 프로젝트 등). proxy 가 있으면 그걸로 빨리 만들어요."""
    from . import media
    d = project.project_dir(name)
    for i, sid in enumerate(sids):
        p = project.load(name)
        s = next((x for x in p["sources"] if x["id"] == sid), None)
        if s is None:
            continue
        sd = d / "sources" / sid
        sd.mkdir(parents=True, exist_ok=True)
        proxy = sd / "proxy.mp4"
        media.thumbs(proxy if proxy.exists() else s["path"], sd / "thumbs.jpg", sd / "thumbs.json", p["canvas"])
        if on_progress:
            on_progress((i + 1) / len(sids))
    project.transact(name, lambda p: [], by="job")  # rev 를 올려서 UI 가 새 썸네일을 받아 가게 해요


def ensure_thumbs(name, p):
    """분석이 끝났는데 썸네일이 없는 소스가 있으면 썸네일만 만드는 가벼운 작업을 넣어요. 작업 사본 또는 None."""
    d = project.project_dir(name)
    need = [s["id"] for s in p["sources"] if s["status"] == "ready" and not (d / "sources" / s["id"] / "thumbs.jpg").exists()]
    with LOCK:
        need = [sid for sid in need if (name, sid) not in TRIED]
        if not need:
            return None
        TRIED.update((name, sid) for sid in need)
    return submit(name, "thumbs", lambda progress: make_thumbs(name, need, progress), sids=need)


def rebuild_preview(name, wait=True):
    """소스 순서를 바꾸거나 뺀 뒤 preview.mp4 를 지금 순서로 다시 이어요 (소스가 없으면 지워요).
    분석이 안 끝난 소스가 있으면 False (그 분석이 끝날 때 analyze 가 다시 이어요).
    wait=False 면 이 프로젝트를 분석하는 중일 때 기다리지 않고 None."""
    from . import analyze
    with analysis_lock(name, wait) as got:
        if not got:
            return None
        if project.load(name)["sources"]:
            if not analyze.rebuild_preview(name):
                return False
        else:
            (project.project_dir(name) / "preview.mp4").unlink(missing_ok=True)
    project.transact(name, lambda p: [], by="job")  # UI 가 새 미리보기를 알아채게
    return True


def preview(name):
    """preview.mp4 를 다시 만드는 가벼운 작업 (소스 순서를 바꾸거나 뺀 뒤)."""
    return submit(name, "preview", lambda progress: rebuild_preview(name))


# ---------- 묶음 편집 (UI·CLI 공통) ----------

def change(name, fn, by, snapshot=None):
    """fn(p) 로 여러 항목을 한꺼번에 바꾸고 바뀐 항목마다 rev·by 를 찍어 변경 기록을 만들어요.
    (p, changes, fn 의 반환값) 을 돌려줘요. changes 모양은 project.apply_ops 와 같아요."""
    out = []

    def run(p):
        before = ({c: {x["id"]: copy.deepcopy(x) for x in p[c]} for c in COLLS},
                  {k: copy.deepcopy(_key(p, k)) for k in KEYS})
        out.append(fn(p))
        return _diff(before, p, "auto" if by == "job" else by)
    p, changes = project.transact(name, run, by=by, snapshot=snapshot)
    return p, changes, out[0]


def _key(p, key):
    for k in key.split("."):
        p = p.get(k) if isinstance(p, dict) else None
    return p


def _diff(before, p, by):
    items, keys = before
    rev, out = p["rev"] + 1, []
    for c in COLLS:
        seen = set()
        for x in p[c]:
            seen.add(x["id"])
            if items[c].get(x["id"]) != x:
                x["rev"] = rev
                x.setdefault("by", by)
                out.append({"coll": c, "item": copy.deepcopy(x)})
        out += [{"coll": c, "id": i, "deleted": True} for i in items[c] if i not in seen]
    return out + [{"key": k, "value": copy.deepcopy(_key(p, k))} for k in KEYS if _key(p, k) != keys[k]]


def detect(name, by, settings=None):
    """(주면) 컷 설정을 바꾸고 분석된 소스들의 자동 컷(by=auto)을 다시 찾아 바꿔요.
    확인한 결정은 analyze 가 이어받아요. 바꾸기 전 프로젝트는 history/ 에 남겨요. (p, changes)"""
    from . import analyze

    def fn(p):
        if settings:
            project.apply_ops(p, [{"op": "set", "key": "settings.cut", "value": settings}], by)
        analyze.redetect(p)
    return change(name, fn, by, snapshot="컷 다시 찾기")[:2]


def build_subs(name, by):
    """음성 인식 단어로 자막을 모두 새로 만들어요 (지금 자막은 history/ 에 남겨요). (p, changes)"""
    from . import analyze, subs

    def fn(p):
        if not analyze.words(p):
            waiting = any(s["status"] in ("queued", "analyzing") for s in p["sources"])
            raise ValueError("영상 분석이 끝나면 자막을 만들 수 있어요" if waiting else "인식된 말이 없어요")
        subs.generate(p)
    return change(name, fn, by, snapshot="자막 만들기")[:2]


def import_subs(name, text, by, base="auto", replace=True):
    """대본(SRT·VTT·시간+글·클로바노트·글만)을 말소리에 맞춰 자막으로 만들어요. (p, changes, info)"""
    from . import subs
    if not isinstance(text, str) or not text.strip():
        raise ValueError("대본 내용이 비어 있어요")
    if base not in ("auto", "orig", "edit"):
        raise ValueError("시간 기준(base)은 auto·orig·edit 중 하나예요")
    return change(name, lambda p: subs.import_script(p, text, base=base, replace=bool(replace), by=by), by,
                  snapshot="대본 불러오기")


def split_sub(name, sub_id, t, by):
    """자막 하나를 원본 시간 t 에서 둘로 나눠요. (p, changes)"""
    from . import subs
    return change(name, lambda p: subs.split_subtitle(p, sub_id, t, by=by), by)[:2]


def merge_subs(name, ids, by):
    """자막 여러 줄을 한 줄로 합쳐요. (p, changes)"""
    from . import subs
    return change(name, lambda p: subs.merge_subtitles(p, ids, by=by), by)[:2]


def read_text(path):
    """대본 파일을 읽어요 (UTF-8 → UTF-16(BOM) → CP949 순서로)."""
    raw = Path(path).expanduser().read_bytes()
    if len(raw) > 20 << 20:
        raise ValueError("대본 파일이 너무 커요")
    for enc in ("utf-16",) if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("대본 파일의 글자 인코딩을 읽을 수 없어요")


# ---------- 내보내기 ----------

def export(name, kind, title=None, burn=True, include_subs=True, copy_media=True, fallback=False, target="folder"):
    """내보내기 작업(capcut|mp4|srt)을 넣어요. 캡컷은 target 이 folder(exports/ 폴더, 기본)|capcut(이 맥 캡컷).
    작업 사본을 돌려줘요."""
    from . import capcut
    if kind not in EXPORTS:
        raise ValueError("내보낼 형식은 capcut·mp4·srt 중 하나예요")
    if target not in capcut.TARGETS:
        raise ValueError("내보낼 곳은 folder·capcut 중 하나예요")
    project.load(name)  # 없는 프로젝트면 바로 알려요

    def work(progress):
        return run_export(name, kind, title, burn, include_subs, copy_media, fallback, progress, target)
    return submit(name, "export", work, target=kind)


def run_export(name, kind, title=None, burn=True, include_subs=True, copy_media=True, fallback=False, on_progress=None,
               target="folder"):
    """편집본을 내보내고 {kind, path, …} 를 돌려줘요. 캡컷은 {kind, target, path, name}: target=folder 면
    exports/capcut-<시각>/<이름>/ (다른 맥으로 옮기기), capcut 이면 이 맥 캡컷 프로젝트 폴더에 써요.
    mp4·srt 는 exports/<kind>-<시각>/ 에 써요."""
    from . import capcut, render
    p = project.load(name)
    if kind == "capcut":
        folder = capcut.export_draft(p, title or p["title"], include_subs=include_subs, copy_media=copy_media,
                                     fallback=fallback, on_progress=on_progress, target=target)
        return {"kind": kind, "target": target, "path": str(folder), "name": folder.name}
    if kind not in EXPORTS:
        raise ValueError("내보낼 형식은 capcut·mp4·srt 중 하나예요")
    if kind == "srt" and not render.edited_subtitles(p):
        raise ValueError("내보낼 자막이 없어요")
    out = _export_dir(name, kind)
    try:
        if kind == "mp4":
            final = Path(render.render_mp4(p, out, burn=burn, on_progress=on_progress))
        else:
            final = Path(render.export_srt(p, out / "final.srt"))
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    return {"kind": kind, "path": str(final), "dir": out.name, "files": sorted(x.name for x in out.iterdir() if x.is_file()),
            "url": f"/media/{quote(name)}/exports/{out.name}/{final.name}"}


def _export_dir(name, kind):
    base = project.project_dir(name) / "exports"
    base.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in itertools.count(1):
        d = base / (f"{kind}-{stamp}" if n == 1 else f"{kind}-{stamp}-{n}")
        try:
            d.mkdir()
            return d
        except FileExistsError:
            continue

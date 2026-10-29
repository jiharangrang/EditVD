"""프로젝트(project.json) 저장, 편집 연산, 원본↔편집본 시간 변환, 소스 순서, v1 변환을 맡아요.
시간은 모두 원본 시간축(소스들을 순서대로 이어 붙인 축, 초, 소수 셋째 자리)이에요."""
import bisect
import copy
import fcntl
import functools
import itertools
import json
import math
import os
import re
import shutil
import tempfile
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import media

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"
DEFAULT_SETTINGS = {
    "cut": {"min_silence": 0.5, "pad_before": 0.15, "pad_after": 0.25, "fillers": True, "long_silence": 2.0},
    "subs": {"lead": 0.0, "hold": 0.3, "min": 0.8, "bridge": 0.3, "max_chars": 18},
    "asr": {"model": "mlx-community/whisper-large-v3-turbo", "language": "ko", "prompt": "", "clean": False},
}
SETTING_RULES = {
    "cut": {"min_silence": (0.1, 10), "pad_before": (0, 2), "pad_after": (0, 2), "long_silence": (0.5, 60),
            "fillers": bool},
    "subs": {"lead": (0, 2), "hold": (0, 5), "min": (0, 10), "bridge": (0, 5), "max_chars": (4, 80)},
    "asr": {"model": str, "language": str, "prompt": str, "clean": bool},
}
PALETTE = ("#40DAF5", "#F5A524", "#A78BFA", "#34D399", "#F472B6", "#60A5FA")
DEFAULT_POS = {"x": 50, "y": 88}
CUT_TYPES = ("silence", "long", "filler", "gap", "ng", "retake", "manual", "user", "clip")
FIELDS = {"cuts": {"start", "end", "type", "enabled", "reviewed", "reason"},
          "subtitles": {"start", "end", "text", "speech", "speaker", "pos", "locked", "check"},
          "speakers": {"name", "color", "pos"},
          "comments": {"t", "text", "done", "reply"}}
DECISIONS = {"cuts": {"enabled", "reviewed"}, "subtitles": {"check"}, "comments": {"done", "reply"}}  # 이것만 바꾸면 by 유지
ITEM_DEFAULTS = {
    "cuts": {"type": "manual", "enabled": True, "reviewed": False, "reason": "", "by": "auto", "rev": 0},
    "subtitles": {"text": "", "speaker": None, "pos": None, "locked": False, "check": False, "by": "auto", "rev": 0},
    "comments": {"text": "", "done": False, "reply": "", "by": "ui", "rev": 0},
}
PREFIX = {"cuts": "c", "subtitles": "t", "comments": "m"}
NAME_RE = re.compile(r"[A-Za-z0-9가-힣_-]{1,60}")
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
EPS = 0.0005


def check_name(name):
    """프로젝트 폴더 이름(영문·숫자·한글·_·- 1~60자)을 확인해요."""
    if isinstance(name, str):
        name = unicodedata.normalize("NFC", name)
        if NAME_RE.fullmatch(name):
            return name
    raise ValueError("프로젝트 이름은 영문·숫자·한글·_·- 로 60자까지 쓸 수 있어요")


def project_dir(name):
    return PROJECTS / check_name(name)


def new_id(prefix):
    return prefix + uuid.uuid4().hex[:6]


def _now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write(path, data):
    """같은 폴더의 임시 파일에 다 쓴 뒤 바꿔치기해요 (읽는 쪽은 언제나 온전한 파일을 봐요)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


@contextmanager
def _locked(d):
    with open(d / "project.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _retime(p):
    """자막 자동 타이밍을 다시 계산하고 바뀐 자막 id 를 돌려줘요 (subs.py 가 없으면 건너뛰어요)."""
    try:
        from . import subs
    except ImportError:
        subs = None
    return subs.retime(p) if subs else []


# ---------- 목록·만들기·읽기·저장 ----------

def list_projects():
    """홈 화면용 프로젝트 목록 (최근 수정 순). v1 프로젝트는 legacy: true."""
    out = []
    for d in sorted(PROJECTS.iterdir()) if PROJECTS.is_dir() else []:
        try:
            name = check_name(d.name)
            if (d / "project.json").exists():
                p = load(name)
                first = p["sources"][0]["id"] if p["sources"] else None
                cover = (f"/media/{quote(name)}/sources/{first}/thumbs.jpg"
                         if first and (d / "sources" / first / "thumbs.jpg").exists() else None)
                out.append({"name": name, "title": p["title"], "updated": p["updated"], "duration": total(p),
                            "edited": round(edited_duration(p), 3), "sources": len(p["sources"]), "cover": cover})
            elif (d / "meta.json").exists():
                out.append(_legacy_summary(d, name))
        except (OSError, ValueError, KeyError, TypeError):
            continue  # 깨진 폴더는 목록에서 빼요
    return sorted(out, key=lambda x: x["updated"], reverse=True)


def _legacy_summary(d, name):
    meta = _read(d / "meta.json")
    dur = round(float(meta["duration"]), 3)
    cuts = _v1_cuts(_read(d / "edit.json", {}) or {}, _read(d / "review.json", {}) or {}, dur)
    p = {"sources": [{"duration": dur}], "cuts": cuts, "canvas": {"fps": _canvas_fps(meta.get("fps") or 30)}}
    mtime = max((d / n).stat().st_mtime for n in ("meta.json", "edit.json", "review.json") if (d / n).exists())
    return {"name": name, "title": name, "updated": datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S"),
            "duration": dur, "edited": round(edited_duration(p), 3), "sources": 1, "cover": _legacy_cover(d, name, dur),
            "legacy": True}


def _legacy_cover(d, name, dur):
    """v1 표지: 처음 목록에 보일 때 preview.mp4 의 10% 지점 한 장을 cover.jpg(+cover.json) 로 만들어 둬요.
    다른 v1 파일은 건드리지 않고, 못 만들면 표지 없이 보여요."""
    jpg = d / "cover.jpg"
    if not jpg.exists() and (d / "preview.mp4").exists():
        try:
            media.cover(d / "preview.mp4", jpg, d / "cover.json", dur * 0.1)
        except (OSError, RuntimeError):
            return None
    return f"/media/{quote(name)}/cover.jpg" if jpg.exists() else None


def _canvas_fps(fps):
    return min((24, 25, 30, 50, 60), key=lambda f: abs(f - fps))


def _canvas(info):
    """첫 영상 기준 캔버스 (짝수 크기, fps 는 24/25/30/50/60 중 가까운 값)."""
    return {"w": max(2, int(info["w"]) // 2 * 2), "h": max(2, int(info["h"]) // 2 * 2),
            "fps": _canvas_fps(info["fps"])}


def _probe_all(paths):
    """경로들을 확인하고 각 영상을 읽어요 (하나라도 문제면 ValueError)."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ValueError("영상 파일을 하나 이상 골라 주세요")
    out = []
    for x in paths:
        if not isinstance(x, (str, Path)) or not str(x).strip():
            raise ValueError("파일 경로를 확인해 주세요")
        path = Path(x).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"파일을 찾을 수 없어요: {path.name}")
        out.append((path, media.probe(path)))
    return out


def _source(d, sid, path, info):
    sd = d / "sources" / sid
    sd.mkdir(parents=True, exist_ok=True)
    _write(sd / "probe.json", {"path": str(path), **info})
    return {"id": sid, "path": str(path), "name": path.name, "duration": info["duration"], "offset": 0.0,
            "w": info["w"], "h": info["h"], "fps": info["fps"], "has_audio": info["has_audio"],
            "created": info["created"], "play": "original" if info.get("playable") else "proxy",
            "status": "queued", "progress": 0.0, "step": "", "error": ""}


def create(title, paths, name=None):
    """영상들로 새 프로젝트를 만들어요 (소스는 분석 대기 queued, rev 1). 폴더 이름을 돌려줘요."""
    infos = _probe_all(paths)
    now = datetime.now()
    given = (title or "").strip()[:100]
    title = given or f"{now.month}월 {now.day}일"
    PROJECTS.mkdir(exist_ok=True)
    if name is not None:
        name = check_name(name)
        try:
            (PROJECTS / name).mkdir()
        except FileExistsError:
            raise ValueError(f"이미 있는 프로젝트예요: {name}") from None
    else:
        base = re.sub(r"[^A-Za-z0-9가-힣_-]+", "_", unicodedata.normalize("NFC", given)).strip("_-")[:50]
        base = base or now.strftime("%Y%m%d")
        for n in itertools.count(1):
            name = base if n == 1 else f"{base}-{n}"
            try:
                (PROJECTS / name).mkdir()
                break
            except FileExistsError:
                continue
    d = PROJECTS / name
    try:
        p = {"format": 2, "rev": 1, "updated": _now(), "by": "cli", "name": name, "title": title,
             "canvas": _canvas(infos[0][1]), "sources": [], "settings": copy.deepcopy(DEFAULT_SETTINGS),
             "cuts": [], "subtitles": [], "speakers": [], "splits": [], "comments": [], "notes": ""}
        p["sources"] = [_source(d, f"s{i}", path, info) for i, (path, info) in enumerate(infos, 1)]
        recompute_offsets(p)
        _write(d / "project.json", p)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return name


def _speaker_defaults(sid):
    i = ord(sid) - 65
    return {"name": f"화자 {i + 1}", "color": PALETTE[i % len(PALETTE)], "pos": dict(DEFAULT_POS)}


def _fill(p, name):
    """빠진 기본값을 채워요 (다른 모듈이 직접 넣은 항목도 같은 모양이 되게)."""
    p["format"], p["name"] = 2, name
    for k, v in (("rev", 0), ("updated", ""), ("by", "cli"), ("title", name), ("notes", ""),
                 ("canvas", {"w": 1920, "h": 1080, "fps": 30}), ("settings", {}), ("sources", []), ("cuts", []),
                 ("subtitles", []), ("speakers", []), ("splits", []), ("comments", [])):
        p.setdefault(k, copy.deepcopy(v))
    for k, v in DEFAULT_SETTINGS.items():
        p["settings"][k] = {**v, **(p["settings"].get(k) or {})}
    for s in p["sources"]:
        for k, v in (("status", "ready"), ("progress", 1.0), ("step", ""), ("error", ""), ("has_audio", True),
                     ("created", ""), ("name", Path(s.get("path", "")).name)):
            s.setdefault(k, v)
    for coll, defaults in ITEM_DEFAULTS.items():
        for x in p[coll]:
            for k, v in defaults.items():
                x.setdefault(k, v)
    for t in p["subtitles"]:
        t.setdefault("speech", [t["start"], t["end"]])
    for s in p["speakers"]:
        for k, v in _speaker_defaults(s["id"]).items():
            s.setdefault(k, v)
    recompute_offsets(p)
    return p


def load(name):
    """project.json 을 읽고 빠진 기본값을 채워요. 없으면 FileNotFoundError."""
    name = check_name(name)
    p = _read(PROJECTS / name / "project.json")
    if p is None:
        raise FileNotFoundError(f"프로젝트가 없어요: {name}")
    return _fill(p, name)


def transact(name, fn, by="cli", snapshot=None):
    """잠금 → 읽기 → fn(p) → rev+1 → 원자적 저장. (p, fn 이 돌려준 변경 목록) 을 돌려줘요.
    fn 이 예외를 내면 아무것도 쓰지 않아요. snapshot="이름" 이면 바꾸기 전 파일을 history/ 에 남겨요."""
    d = project_dir(name)
    if not (d / "project.json").exists():
        raise FileNotFoundError(f"프로젝트가 없어요: {name}")
    with _locked(d):
        p = load(name)
        if snapshot:
            _snapshot(d, snapshot)
        rev = p["rev"]
        changes = fn(p) or []
        p.update(rev=rev + 1, by=by, updated=_now())
        _write(d / "project.json", p)
    return p, changes


def snapshot(name, label):
    """지금 project.json 을 history/<시각>-<label>.json 으로 복사해 둬요 (큰 변경 전에). 경로를 돌려줘요."""
    return _snapshot(project_dir(name), label)


def _snapshot(d, label):
    h = d / "history"
    h.mkdir(exist_ok=True)
    label = re.sub(r"[^A-Za-z0-9가-힣_-]+", "_", str(label)).strip("_")[:40] or "snap"
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
    dst = h / f"{stem}.json"
    for n in itertools.count(2):
        if not dst.exists():
            break
        dst = h / f"{stem}-{n}.json"
    shutil.copyfile(d / "project.json", dst)
    return dst


# ---------- 편집 연산 ----------

def apply_ops(p, ops, by):
    """편집 연산 목록을 p 에 그대로 적용하고 바뀐 항목 기록을 돌려줘요 (잘못된 값이면 한국어 ValueError).
    put 은 같은 id 가 있으면 준 필드만 합치고 없으면 새로 만들어요 (id 를 안 주면 새 id).
    항목 rev = 새 프로젝트 rev, by = 쓴 쪽. 켜기/끄기·확인 같은 결정만 바꾸면 by(만든 쪽)는 그대로 둬요."""
    if not isinstance(ops, list):
        raise ValueError("ops 는 목록이어야 해요")
    rev = p.get("rev", 0) + 1
    touched, keys, retime = {}, {}, False
    for op in ops:
        kind = op.get("op") if isinstance(op, dict) else None
        if kind == "set":
            keys[op.get("key")] = _set(p, op.get("key"), op.get("value"))
            retime |= op.get("key") == "settings.subs"
        elif kind in ("put", "del"):
            coll = op.get("coll")
            if coll not in FIELDS:
                raise ValueError(f"알 수 없는 목록이에요: {coll}")
            item = op.get("item")
            before = next((x for x in p[coll] if x["id"] == item.get("id")), None) if kind == "put" and isinstance(item, dict) else None
            iid = _put(p, coll, op.get("item"), rev, by) if kind == "put" else _del(p, coll, op.get("id"), rev, touched)
            if iid:
                touched[(coll, iid)] = True
            if iid and kind == "del":
                retime |= coll != "comments"
            elif iid and coll in ("cuts", "subtitles"):
                timing = ("start", "end", "enabled", "type") if coll == "cuts" else ("start", "end", "speech", "speaker", "locked")
                retime |= before is None or any(k in item and before.get(k) != item[k] for k in timing)
        else:
            raise ValueError(f"알 수 없는 연산이에요: {kind}")
    for t in p["subtitles"]:  # 아직 없는 화자 글자를 쓰면 그 화자를 만들어 둬요
        spk = t.get("speaker")
        if spk is not None and not any(s["id"] == spk for s in p["speakers"]):
            p["speakers"].append({"id": spk, **_speaker_defaults(spk), "by": by, "rev": rev})
            touched[("speakers", spk)] = True
    if retime:  # 자막 자동 타이밍 (subs.py 가 아직 없으면 건너뛰어요)
        try:
            from . import subs
        except ImportError:
            subs = None
        if subs:
            for iid in subs.retime(p):
                touched.setdefault(("subtitles", iid), True)
    changes = []
    for coll, iid in touched:
        item = next((x for x in p[coll] if x["id"] == iid), None)
        changes.append({"coll": coll, "item": copy.deepcopy(item)} if item else {"coll": coll, "id": iid, "deleted": True})
    return changes + [{"key": k, "value": copy.deepcopy(v)} for k, v in keys.items()]


def _free_id(p, coll):
    used = {x["id"] for x in p[coll]}
    if coll == "speakers":
        free = [c for c in map(chr, range(65, 91)) if c not in used]
        if not free:
            raise ValueError("화자는 26명까지 만들 수 있어요")
        return free[0]
    while (iid := new_id(PREFIX[coll])) in used:
        pass
    return iid


def _put(p, coll, item, rev, by):
    if not isinstance(item, dict):
        raise ValueError("item 은 객체여야 해요")
    bad = set(item) - FIELDS[coll] - {"id", "rev", "by"}
    if bad:
        raise ValueError(f"알 수 없는 필드예요: {', '.join(sorted(bad))}")
    lst, iid = p[coll], item.get("id")
    i = next((k for k, x in enumerate(lst) if x["id"] == iid), None) if iid is not None else None
    if i is None:
        if iid is None:
            iid = _free_id(p, coll)
        elif not isinstance(iid, str) or not (re.fullmatch(r"[A-Z]", iid) if coll == "speakers" else ID_RE.fullmatch(iid)):
            raise ValueError(f"id 형식이 올바르지 않아요: {iid}")
        it = {"id": iid, **_blank(coll, iid, by)}
    else:
        it = copy.deepcopy(lst[i])
    fields = set(item) - {"id", "rev", "by"}
    it.update({k: copy.deepcopy(item[k]) for k in fields})
    _check_item(p, coll, it)
    if coll == "subtitles" and i is not None and "locked" not in item and (it["start"], it["end"]) != (lst[i]["start"], lst[i]["end"]):
        it["locked"] = True  # 시간을 직접 바꾼 줄은 자동 타이밍에서 빼요
    it["rev"] = rev
    if i is None or fields - DECISIONS.get(coll, set()):
        it["by"] = by
    if i is None:
        lst.append(it)
    else:
        lst[i] = it
    return iid


def _blank(coll, iid, by):
    if coll == "cuts":
        return {"type": "user" if by == "ui" else "manual", "enabled": True, "reviewed": by == "ui", "reason": ""}
    if coll == "subtitles":
        return {"text": "", "speaker": None, "pos": None, "locked": False, "check": False}
    if coll == "comments":
        return {"text": "", "done": False, "reply": ""}
    return _speaker_defaults(iid)


def _del(p, coll, iid, rev, touched):
    lst = p[coll]
    keep = [x for x in lst if x["id"] != iid]
    if len(keep) == len(lst):
        return None  # 이미 없으면 그냥 넘어가요
    lst[:] = keep
    if coll == "speakers":
        for t in p["subtitles"]:
            if t.get("speaker") == iid:
                t["speaker"], t["rev"] = None, rev
                touched[("subtitles", t["id"])] = True
    return iid


def _num(v, label, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{label}: 숫자가 필요해요")
    if lo is not None and not lo <= v <= hi:
        raise ValueError(f"{label}: {lo:g}~{hi:g} 사이여야 해요")
    return float(v)


def _span(s, e, tot, label):
    s, e = _num(s, f"{label} 시작"), _num(e, f"{label} 끝")
    if s < -EPS or e > tot + EPS:
        raise ValueError(f"{label} 시간이 영상 범위(0~{tot:g}초)를 벗어났어요")
    s, e = round(max(s, 0.0), 3), round(min(e, tot), 3)
    if s >= e:
        raise ValueError(f"{label} 끝은 시작보다 뒤여야 해요")
    return s, e


def _flags(it, *keys):
    for k in keys:
        if not isinstance(it.get(k), bool):
            raise ValueError(f"{k} 는 true/false 여야 해요")


def _text(it, key, limit):
    if not isinstance(it.get(key), str) or len(it[key]) > limit:
        raise ValueError(f"{key} 는 {limit}자 이하 글자여야 해요")


def _pos(v):
    if v is None:
        return None
    if not isinstance(v, dict) or set(v) != {"x", "y"}:
        raise ValueError("위치는 {x, y} (화면 %) 여야 해요")
    return {k: round(_num(v[k], f"위치 {k}", 0, 100), 2) for k in ("x", "y")}


def _check_item(p, coll, it):
    """항목 하나를 검사하고 정리해요 (시간 반올림 등)."""
    tot = total(p)
    if coll in ("cuts", "subtitles"):
        it["start"], it["end"] = _span(it.get("start"), it.get("end"), tot, "컷" if coll == "cuts" else "자막")
    if coll == "cuts":
        if it.get("type") not in CUT_TYPES:
            raise ValueError(f"알 수 없는 컷 종류예요: {it.get('type')}")
        _flags(it, "enabled", "reviewed")
        _text(it, "reason", 500)
    elif coll == "subtitles":
        _text(it, "text", 2000)
        sp = it.get("speech")
        if sp is None:  # 손으로 넣은 줄: 지금 타이밍 설정으로 다시 계산해도 같은 시간이 나오게
            st = {**DEFAULT_SETTINGS["subs"], **(p.get("settings") or {}).get("subs", {})}
            a, b = it["start"] + st["lead"], it["end"] - st["hold"]
            sp = [a, b] if b - a >= 0.1 else [it["start"], it["end"]]
        if not isinstance(sp, (list, tuple)) or len(sp) != 2:
            raise ValueError("speech 는 [시작, 끝] 이어야 해요")
        a, b = (_num(x, "speech") for x in sp)
        if a < -EPS or b > tot + EPS or b < a:
            raise ValueError("speech(말소리 구간)가 올바르지 않아요")
        it["speech"] = [round(max(a, 0.0), 3), round(min(b, tot), 3)]
        spk = it.setdefault("speaker", None)
        if spk is not None and not (isinstance(spk, str) and re.fullmatch(r"[A-Z]", spk)):
            raise ValueError(f"화자는 A~Z 중 하나여야 해요: {spk}")
        it["pos"] = _pos(it.get("pos"))
        _flags(it, "locked", "check")
    elif coll == "speakers":
        _text(it, "name", 40)
        if not it["name"].strip():
            raise ValueError("화자 이름을 적어 주세요")
        if not (isinstance(it.get("color"), str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", it["color"])):
            raise ValueError("화자 색은 #RRGGBB 형식이어야 해요")
        it["pos"] = _pos(it.get("pos"))
    else:
        t = _num(it.get("t"), "메모 위치")
        if not -EPS <= t <= tot + EPS:
            raise ValueError(f"메모 위치가 영상 범위(0~{tot:g}초)를 벗어났어요")
        it["t"] = round(min(max(t, 0.0), tot), 3)
        _text(it, "text", 2000)
        _text(it, "reply", 2000)
        _flags(it, "done")


def _set(p, key, value):
    if key == "title":
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
            raise ValueError("제목은 1~100자로 적어 주세요")
        p["title"] = value.strip()
        return p["title"]
    if key == "notes":
        if not isinstance(value, str) or len(value) > 20000:
            raise ValueError("메모는 20000자 이하 글자여야 해요")
        p["notes"] = value
        return value
    if key == "splits":
        if not isinstance(value, list):
            raise ValueError("분할 위치는 목록이어야 해요")
        tot = total(p)
        p["splits"] = sorted({round(min(max(_num(v, "분할 위치", -EPS, tot + EPS), 0.0), tot), 3) for v in value})
        return p["splits"]
    sec = key[9:] if isinstance(key, str) and key.startswith("settings.") else None
    if sec not in SETTING_RULES:
        raise ValueError(f"바꿀 수 없는 항목이에요: {key}")
    if not isinstance(value, dict):
        raise ValueError("설정은 객체여야 해요")
    rules = SETTING_RULES[sec]
    bad = set(value) - set(rules)
    if bad:
        raise ValueError(f"알 수 없는 설정이에요: {', '.join(sorted(bad))}")
    cur = dict(p["settings"][sec])
    for k, v in value.items():
        rule = rules[k]
        if rule in (bool, str):
            if not isinstance(v, rule) or (rule is str and len(v) > 2000):
                raise ValueError(f"{k}: {'true/false' if rule is bool else '글자'}가 필요해요")
        else:
            v = _num(v, k, *rule)
            if k == "max_chars":
                if v != int(v):
                    raise ValueError("max_chars 는 정수여야 해요")
                v = int(v)
        cur[k] = v
    p["settings"][sec] = cur
    return cur


# ---------- 시간축 ----------

def total(p):
    """원본 전체 길이 (모든 소스 길이 합)."""
    return round(sum(s["duration"] for s in p["sources"]), 3)


def recompute_offsets(p):
    t = 0.0
    for s in p["sources"]:
        s["offset"] = round(t, 3)
        t += s["duration"]


def source_at(p, t):
    """원본 시간 t 가 속한 (소스, 소스 안 시간). 소스 경계에서는 뒤 소스예요."""
    src = p["sources"]
    if not src:
        raise ValueError("영상이 없어요")
    s = src[max(0, bisect.bisect_right([x["offset"] for x in src], t) - 1)]
    return s, min(max(t - s["offset"], 0.0), s["duration"])


def _timeline(p):
    cuts = tuple((c["start"], c["end"]) for c in p["cuts"] if c.get("enabled", True) or c.get("type") == "clip")
    return _build_timeline(cuts, tuple(s["duration"] for s in p["sources"]), (p.get("canvas") or {}).get("fps") or 30)


@functools.lru_cache(maxsize=64)
def _build_timeline(cuts, durs, fps):
    """(합친 컷 구간, 남는 조각, 조각 시작들, 조각의 편집본 시작 누적) — 같은 입력이면 다시 계산하지 않아요."""
    tot = round(sum(durs), 3)
    iv = []
    for s, e in sorted((max(0.0, s), min(tot, e)) for s, e in cuts):
        if e <= s:
            continue
        if iv and s <= iv[-1][1]:
            iv[-1] = (iv[-1][0], max(iv[-1][1], e))
        else:
            iv.append((s, e))
    bounds = [round(b, 3) for b in itertools.accumulate(durs)][:-1]
    segs, t = [], 0.0
    for s, e in iv + [(tot, tot)]:
        if s > t:
            edges = [t] + [b for b in bounds if t < b < s] + [s]
            segs += [(a, b) for a, b in zip(edges, edges[1:]) if b - a >= 1 / fps - 1e-9]
        t = max(t, e)
    cum = tuple(itertools.accumulate((b - a for a, b in segs), initial=0.0))
    return tuple(iv), tuple(segs), tuple(a for a, _ in segs), cum


def cut_intervals(p):
    """실제로 잘리는 구간 (켜진 컷 + clip 컷) 을 합쳐 정렬한 목록."""
    return list(_timeline(p)[0])


def keep_segments(p):
    """남는 조각 (원본 시간). 소스 경계에서 나누고 1프레임보다 짧은 조각은 버려요."""
    return list(_timeline(p)[1])


def edited_duration(p):
    return _timeline(p)[3][-1]


def to_edited(p, t):
    """원본 시간 → 편집본 시간. 잘린 곳이면 그 컷이 시작되는 편집본 위치."""
    _, segs, starts, cum = _timeline(p)
    i = bisect.bisect_right(starts, t) - 1
    if i < 0:
        return 0.0
    a, b = segs[i]
    return cum[i] + (min(t, b) - a)


def to_original(p, e):
    """편집본 시간 → 원본 시간. 조각 경계에서는 다음 조각의 시작이에요."""
    _, segs, _, cum = _timeline(p)
    if not segs:
        return 0.0
    i = min(max(bisect.bisect_right(cum, e) - 1, 0), len(segs) - 1)
    a, b = segs[i]
    return min(a + max(e - cum[i], 0.0), b)


# ---------- 소스 ----------

def add_sources(p, paths):
    """영상을 뒤에 붙여요 (분석 대기 queued). 새 소스 id 목록 (지운 소스의 id 는 다시 쓰지 않아요)."""
    infos = _probe_all(paths)
    d = project_dir(p["name"])
    used = {s["id"] for s in p["sources"]} | ({x.name for x in (d / "sources").iterdir()} if (d / "sources").is_dir() else set())
    n = max((int(x[1:]) for x in used if re.fullmatch(r"s\d+", x)), default=0)
    if not p["sources"]:
        p["canvas"] = _canvas(infos[0][1])
    sids = []
    for path, info in infos:
        n += 1
        p["sources"].append(_source(d, f"s{n}", path, info))
        sids.append(f"s{n}")
    recompute_offsets(p)
    return sids


def reorder_sources(p, order):
    """소스 순서를 바꾸고 컷·자막(speech 포함)·분할·메모를 각자의 소스와 함께 옮겨요."""
    ids = [s["id"] for s in p["sources"]]
    if not isinstance(order, list) or len(order) != len(ids) or set(order) != set(ids):
        raise ValueError("영상 순서 목록이 지금 영상들과 맞지 않아요")
    _move(p, order)


def remove_source(p, sid):
    """소스 하나를 빼요. 그 안의 항목은 지우고 뒤 항목은 앞으로 당겨요."""
    ids = [s["id"] for s in p["sources"]]
    if sid not in ids:
        raise ValueError(f"없는 영상이에요: {sid}")
    _move(p, [i for i in ids if i != sid])


def _move(p, order):
    """order(빠진 소스는 삭제) 대로 소스를 다시 놓고 시간이 있는 항목을 옮겨요.
    소스 경계를 넘는 컷은 소스별로 나누고, 자막은 가장 많이 겹치는 소스 안으로 잘라 넣어요."""
    recompute_offsets(p)
    spans = [(s["offset"], s["offset"] + s["duration"], s["id"]) for s in p["sources"]]
    starts = [a for a, _, _ in spans]
    by_id = {s["id"]: s for s in p["sources"]}
    p["sources"] = [by_id[i] for i in order]
    recompute_offsets(p)
    new = {s["id"]: s["offset"] for s in p["sources"]}

    def owner(t):
        return spans[max(0, bisect.bisect_right(starts, t) - 1)]

    def shift(t, sp):
        return round(min(max(t, sp[0]), sp[1]) - sp[0] + new[sp[2]], 3)

    used, cuts = {c["id"] for c in p["cuts"]}, []
    for c in p["cuts"]:
        s0, e0 = c["start"], c["end"]
        pieces = [sp for sp in spans if sp[2] in new and min(e0, sp[1]) - max(s0, sp[0]) > EPS]
        for k, sp in enumerate(pieces):
            x = c if k == 0 else copy.deepcopy(c)
            while k and x["id"] in used:
                x["id"] = new_id("c")
            used.add(x["id"])
            x["start"], x["end"] = shift(max(s0, sp[0]), sp), shift(min(e0, sp[1]), sp)
            cuts.append(x)
    subs = []
    for t in p["subtitles"]:
        sp = max(spans, key=lambda x: min(t["end"], x[1]) - max(t["start"], x[0]))
        if sp[2] not in new:
            continue
        a, b = max(t["start"], sp[0]), min(t["end"], sp[1])
        s0, s1 = (max(t["speech"][0], sp[0]), min(t["speech"][1], sp[1])) if t.get("speech") else (a, b)
        if s1 < s0:
            s0, s1 = a, b
        t["start"], t["end"], t["speech"] = shift(a, sp), shift(b, sp), [shift(s0, sp), shift(s1, sp)]
        subs.append(t)
    p["cuts"] = sorted(cuts, key=lambda c: (c["start"], c["end"]))
    p["subtitles"] = sorted(subs, key=lambda t: (t["start"], t["end"]))
    comments = []
    for m in p.get("comments", []):
        sp = owner(m["t"])
        if sp[2] in new:
            m["t"] = shift(m["t"], sp)
            comments.append(m)
    p["comments"] = sorted(comments, key=lambda m: m["t"])
    p["splits"] = sorted(shift(x, owner(x)) for x in p.get("splits", []) if owner(x)[2] in new)
    _retime(p)


# ---------- v1 → v2 ----------

def migrate_v1(name):
    """v1 프로젝트(meta·edit·review.json)를 project.json 으로 옮겨요. v1 파일은 건드리지 않고, 옮겼으면 True.
    s1 폴더에는 v1 미리보기·소리·파형·무음·대본을 하드링크로 연결해요 (안 되면 심볼릭 링크)."""
    d = project_dir(name)
    if (d / "project.json").exists():
        return False
    if not (d / "meta.json").exists():
        raise FileNotFoundError(f"프로젝트가 없어요: {name}")
    with _locked(d):
        if (d / "project.json").exists():
            return False
        p, info = _convert_v1(d)
        sd = d / "sources" / "s1"
        sd.mkdir(parents=True, exist_ok=True)
        for a, b in (("preview.mp4", "proxy.mp4"), ("audio.wav", "audio.wav"), ("peaks.json", "peaks.json"),
                     ("silences.json", "silences.json"), ("transcript.json", "transcript.json")):
            _link(d / a, sd / b)
        _write(sd / "probe.json", {"path": p["sources"][0]["path"], **info})
        _write(d / "project.json", p)
    return True


def _link(src, dst):
    """복사 없이 연결해요. 하드링크라서 나중에 preview.mp4 를 다시 만들어도 proxy.mp4 는 그대로예요."""
    if not src.exists():
        return
    dst.unlink(missing_ok=True)
    try:
        os.link(src.resolve(), dst)
    except OSError:
        dst.symlink_to(os.path.relpath(src.resolve(), dst.parent))


def _v1_time(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M").strftime("%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return ""


def _convert_v1(d):
    """v1 폴더를 읽어 메모리에서 (v2 project, s1 probe 정보) 를 만들어요. 아무 파일도 쓰지 않아요."""
    name = check_name(d.name)
    meta = _read(d / "meta.json")
    edit, review = _read(d / "edit.json", {}) or {}, _read(d / "review.json", {}) or {}
    dur = round(float(meta["duration"]), 3)
    path = meta.get("source_path") or str(d / meta.get("source", "source.mp4"))
    try:
        found = media.probe(path)
    except ValueError:
        found = {}
    info = {"duration": dur, "w": meta.get("width") or found.get("w") or 1920, "h": meta.get("height") or found.get("h") or 1080,
            "fps": round(float(meta.get("fps") or found.get("fps") or 30), 3),
            "has_audio": found.get("has_audio", meta.get("has_audio", True)),
            "created": found.get("created") or _v1_time(meta.get("created")), "rotation": found.get("rotation", 0)}
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    if meta.get("whisper_model"):
        settings["asr"]["model"] = meta["whisper_model"]
    if meta.get("prompt"):
        settings["asr"]["prompt"] = meta["prompt"]
    subs, speakers = _v1_subs(edit, review, (_read(d / "transcript.json", {}) or {}).get("words") or [], dur)
    comments = []
    for m in review.get("comments") or []:
        try:
            t = round(min(max(float(m["t"]), 0.0), dur), 3)
        except (KeyError, TypeError, ValueError):
            continue
        mid = m["id"] if isinstance(m.get("id"), str) and ID_RE.fullmatch(m["id"]) else new_id("m")
        comments.append({"id": mid, "t": t, "text": str(m.get("text", "")), "done": False, "reply": "", "by": "ui", "rev": 1})
    splits = sorted({round(min(max(float(x), 0.0), dur), 3) for x in review.get("split_points") or []
                     if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)})
    p = {"format": 2, "rev": 1, "updated": _now(), "by": "cli", "name": name, "title": name, "canvas": _canvas(info),
         "sources": [{"id": "s1", "path": path, "name": Path(path).name, "duration": dur, "offset": 0.0,
                      **{k: info[k] for k in ("w", "h", "fps", "has_audio", "created")},
                      "status": "ready", "progress": 1.0, "step": "", "error": ""}],
         "settings": settings, "cuts": _v1_cuts(edit, review, dur), "subtitles": subs, "speakers": speakers,
         "splits": splits, "comments": comments, "notes": str(edit.get("notes") or "")}
    _retime(p)
    return p, info


def _v1_span(a, b, dur):
    try:
        a, b = round(max(float(a), 0.0), 3), round(min(float(b), dur), 3)
    except (TypeError, ValueError):
        return None
    return (a, b) if b - a >= 0.001 else None


def _v1_cuts(edit, review, dur):
    """v1 컷 + review(toggles·cut_times·new_cuts·cut_meta) → v2 컷. chunk → manual.
    reviewed: v1 에서 켜기/끄기를 직접 한 컷 (+ UI 에서 직접 자른 컷, 결정이 확정된 컷)."""
    toggles, times, meta = review.get("toggles") or {}, review.get("cut_times") or {}, review.get("cut_meta") or {}
    base = edit.get("cuts") or []
    out, seen = [], set()
    for i, c in enumerate(base + (review.get("new_cuts") or [])):
        new = i >= len(base)
        cid = c.get("id")
        if not (isinstance(cid, str) and ID_RE.fullmatch(cid)) or cid in seen:
            cid = new_id("c")
        span = _v1_span(*times.get(cid, (c.get("start"), c.get("end"))), dur)
        if not span:
            continue
        typ = c.get("type") if c.get("type") in CUT_TYPES else "manual"
        m = meta.get(cid) or {}
        decided = m.get("decision", c.get("decision")) in ("accepted", "rejected")
        out.append({"id": cid, "start": span[0], "end": span[1], "type": typ,
                    "enabled": bool(toggles.get(cid, c.get("enabled", True))),
                    "reviewed": cid in toggles or decided or (new and typ == "user"),
                    "reason": str(m.get("reason", c.get("reason", "")))[:500],
                    "by": "ui" if new else "auto" if typ in ("silence", "long", "filler", "gap") else "cli", "rev": 1})
        seen.add(cid)
    return sorted(out, key=lambda c: (c["start"], c["end"]))


def _v1_subs(edit, review, words, dur):
    """v1 자막 + review(sub_edits·sub_times·sub_styles·new_subs·deleted_subs) → (v2 자막, 화자).
    speech = 자막 안 단어들의 [첫 시작, 마지막 끝] (자막 구간 안으로), 단어가 없으면 [start, max(start+0.2, end-0.3)]."""
    texts, times, styles = review.get("sub_edits") or {}, review.get("sub_times") or {}, review.get("sub_styles") or {}
    gone = set(review.get("deleted_subs") or [])
    ws = sorted(((w["start"] + w["end"]) / 2, w["start"], w["end"]) for w in words
                if isinstance(w.get("start"), (int, float)) and isinstance(w.get("end"), (int, float)))
    mids = [m for m, _, _ in ws]
    base = edit.get("subtitles") or []
    out, seen, names = [], set(), {}
    for i, s in enumerate(base + (review.get("new_subs") or [])):
        sid = s.get("id")
        if sid in gone:
            continue
        if not (isinstance(sid, str) and ID_RE.fullmatch(sid)) or sid in seen:
            sid = new_id("t")
        span = _v1_span(*times.get(sid, (s.get("start"), s.get("end"))), dur)
        if not span:
            continue
        a, b = span
        text = texts.get(sid, s.get("text", ""))
        style = {**(s.get("style") or {}), **(styles.get(sid) or {})}
        pos = None
        if "x" in style or "y" in style:
            pos = {k: round(min(max(float(style.get(k, d)), 0.0), 100.0), 2) for k, d in (("x", 50), ("y", 90))}
        inside = ws[bisect.bisect_left(mids, a):bisect.bisect_left(mids, b)]
        sp = [max(min(w[1] for w in inside), a), min(max(w[2] for w in inside), b)] if inside else []
        if not sp or sp[1] - sp[0] < 0.05:  # v1 이 소리 경계에 맞춘 시작·끝보다 넓히지 않아요
            sp = [a, max(a + 0.2, b - 0.3)]
        sp = [round(min(max(sp[0], 0.0), dur), 3), round(min(max(sp[1], 0.0), dur), 3)]
        spk = s.get("speaker").strip() if isinstance(s.get("speaker"), str) else ""
        if spk and spk not in names and len(names) < 26:
            names[spk] = chr(65 + len(names))
        edited = i >= len(base) or sid in texts or sid in times or sid in styles or text != s.get("auto", text)
        out.append({"id": sid, "start": a, "end": b, "text": str(text), "speech": sp, "speaker": names.get(spk),
                    "pos": pos, "locked": False, "check": bool(s.get("check")), "by": "ui" if edited else "auto",
                    "rev": 1})
        seen.add(sid)
    speakers = [{"id": c, **_speaker_defaults(c), "name": n[:40]} for n, c in names.items()]
    return sorted(out, key=lambda t: (t["start"], t["end"])), speakers

"""소스(영상 파일) 하나를 분석해요: 미리보기·음성·파형·썸네일·무음·음성 인식(whisper) 파일을 만들고,
설정(settings.cut)에 맞춰 자동 컷(무음·긴 무음·필러·말 없는 구간)을 찾아요.
sources/<sid>/*.json 의 시간은 소스 파일 안 시간, project.json 의 시간은 원본 시간축이에요."""
import bisect
import contextlib
import copy
import functools
import importlib
import json
import math
import os
import re
import subprocess
import threading
import time
import types
import wave
from pathlib import Path

import numpy as np

from . import media, project, subs

FINE = 0.15         # 경계 맞추기용 짧은 쉼
EDGE = 0.05         # 소스 시작·끝으로 보는 거리
MIN_CUT = 0.15      # 여유를 빼고 남은 컷이 이보다 짧으면 안 잘라요
GAP = 3.0           # 이보다 긴 '말 없는 구간'은 통째로 판단해요
AMBIENT_DB = 12     # 말소리 중간값에서 이 안쪽이면 현장음 (B-roll 후보)
QUIET_SOUND = 0.4   # 조용한 긴 구간에서 소리 난 부분이 이 비율 이하면 통째로 잘라요
FRAME = 0.02        # 음량 계산 단위 (20 ms)
FILLERS = {"음", "음음", "으음", "어", "어어", "엄", "흠", "으", "아", "에", "그", "저", "저기"}
PROMPT_KO = "음, 어, 그러니까 오늘 브이로그는요."
STAGES = (("proxy", "변환", 30), ("audio", "분석", 4), ("peaks", "분석", 1), ("thumbs", "분석", 8),
          ("silence", "분석", 4), ("whisper", "인식", 50), ("preview", "변환", 2), ("cuts", "분석", 3))
_WHISPER = threading.Lock()  # whisper 는 무거워서 한 번에 하나만 돌려요


class SourceGone(Exception):
    """분석하는 동안 소스(또는 프로젝트)가 빠졌어요."""


# ---------- 소스 분석 ----------

def analyze_source(name, sid, on_progress=None, transcribe=True):
    """소스 하나를 처음부터 분석해요: 미리보기 → 음성 → 파형 → 썸네일 → 무음 → (음성 인식).
    on_progress(단계 글자, 전체 진행 0..1). 진행 상황은 0.5초에 한 번 project.json 에 써요 (by="job").
    다 되면 모든 소스가 준비된 경우 preview.mp4 를 다시 잇고, 이 소스의 자동 컷을 다시 찾고,
    자막이 없거나 이 소스 자막이 모두 자동이면 단어로 자막을 만들어요. 요약 dict 를 돌려줘요.
    음성 인식만 실패하면 소스는 준비(ready, 소리로만 찾은 컷)로 두고 그 내용을 source.error 와 warnings 에 남겨요
    (다시 인식은 transcribe_source). 다른 실패는 source.status=error 로 남기고 예외를 그대로 내요."""
    p = project.load(name)
    src = _src(p, sid)
    sd = _sdir(name, sid)
    sd.mkdir(parents=True, exist_ok=True)
    direct = src.get("play") == "original"  # 크롬이 원본을 바로 재생해요 → 변환·이어 붙이기 없음
    st = _Status(name, sid, on_progress, [k for k, _, _ in STAGES if (transcribe or k != "whisper")
                                          and not (direct and k in ("proxy", "preview"))])
    problems, n_words, order = [], None, None
    try:
        if not direct:
            st("proxy")
            media.make_proxy(src["path"], sd / "proxy.mp4", p["canvas"], on_progress=lambda f: st("proxy", f))
        st("audio")
        media.extract_audio(src["path"], sd / "audio.wav", clean=bool(p["settings"]["asr"].get("clean")))
        st("peaks")
        media.peaks(sd / "audio.wav", sd / "peaks.json")
        st("thumbs")
        thumb_src = sd / "proxy.mp4" if not direct else Path(src["path"])
        media.thumbs(thumb_src, sd / "thumbs.jpg", sd / "thumbs.json", p["canvas"])
        st("silence")
        _write_json(sd / "silences.json", detect_silences(sd / "audio.wav", p["settings"]["cut"]["min_silence"]))
        if transcribe:
            told = []  # 진행 알림(취소 등)에서 난 예외는 인식 실패로 삼키지 않아요

            def tell(f):
                try:
                    st("whisper", f)
                except BaseException as e:
                    told.append(e)
                    raise
            try:
                n_words = _transcribe(sd, project.load(name)["settings"]["asr"], tell, src.get("has_audio", True))
            except Exception as e:  # 예전 인식 결과가 있으면 그대로 써요 (같은 파일이라 유효해요)
                if told:
                    raise
                problems.append(f"음성 인식에 실패했어요: {str(e)[:200]}")
        if not direct:
            st("preview")
            order = _ready_order(name, sid)
            if order:
                _concat(name, project.load(name), lambda f: st("preview", f))
        st("cuts")
        _levels(sd / "audio.wav")  # 잠금 밖에서 미리 계산해 둬요 (캐시)
        p, info = project.transact(name, lambda p: _finish(p, sid, sd, " / ".join(problems)), by="job")
    except SourceGone:
        return {"sid": sid, "removed": True}
    except BaseException as e:
        _fail(name, sid, e)
        raise
    if order and [(s["id"], s["duration"]) for s in p["sources"]] != order:
        rebuild_preview(name)  # 그사이 순서가 바뀌었으면 다시 이어요
    return {**info, "words": n_words, "preview": bool(order), "warnings": problems}


def transcribe_source(name, sid, on_progress=None):
    """음성 인식만 다시 해요 (sources/<sid>/transcript.json). 단어 수를 돌려줘요.
    소리 파일·무음 목록이 없으면 먼저 만들어요. 끝나면 소스 상태를 원래대로 돌려놔요."""
    p = project.load(name)
    src = _src(p, sid)
    sd = _sdir(name, sid)
    sd.mkdir(parents=True, exist_ok=True)
    before = (src["status"], src.get("error", ""))
    st = _Status(name, sid, on_progress, ["whisper"] + ["audio"] * (not (sd / "audio.wav").exists())
                 + ["silence"] * (not (sd / "silences.json").exists()))
    try:
        if not (sd / "audio.wav").exists():
            st("audio")
            media.extract_audio(src["path"], sd / "audio.wav", clean=bool(p["settings"]["asr"].get("clean")))
        if not (sd / "silences.json").exists():
            st("silence")
            _write_json(sd / "silences.json", detect_silences(sd / "audio.wav", p["settings"]["cut"]["min_silence"]))
        st("whisper")
        n = _transcribe(sd, p["settings"]["asr"], lambda f: st("whisper", f), src.get("has_audio", True))
        status = "ready" if before[0] == "analyzing" else before[0]
        _set(name, sid, status=status, progress=1.0 if status == "ready" else src.get("progress", 0.0), step="",
             error=before[1] if status == "error" else "")
    except SourceGone:
        return 0
    except BaseException as e:
        _fail(name, sid, e)
        raise
    return n


def rebuild_preview(name, on_progress=None):
    """모든 소스가 준비됐으면 preview.mp4 를 지금 순서대로 다시 이어 붙여요 (순서 바꾸기·빼기 뒤에 불러요).
    형식이 다른 미리보기(v1 에서 옮긴 것 등)는 다시 만들어요. 이어 붙였으면 True."""
    p = project.load(name)
    if not p["sources"] or any(s["status"] != "ready" or s.get("play") == "original" for s in p["sources"]):
        return False
    _concat(name, p, on_progress)
    return True


def _ready_order(name, sid):
    """sid 말고 다른 소스가 모두 준비됐으면 지금 순서 [(id, 길이)], 아니면 None."""
    p = project.load(name)
    if any(s.get("play") == "original" for s in p["sources"]):
        return None  # 원본을 바로 재생하는 소스가 있으면 preview.mp4 는 안 만들어요 (재생기는 소스마다 따로 열어요)
    if all(s["status"] == "ready" for s in p["sources"] if s["id"] != sid):
        return [(s["id"], s["duration"]) for s in p["sources"]]
    return None


def _concat(name, p, on_progress=None):
    d = project.project_dir(name)
    paths = [d / "sources" / s["id"] / "proxy.mp4" for s in p["sources"]]
    bad = [(s, x) for s, x in zip(p["sources"], paths)
           if not x.exists() or (len(paths) > 1 and not _proxy_ok(x, p["canvas"]))]
    for i, (s, x) in enumerate(bad):
        media.make_proxy(s["path"], x, p["canvas"],
                         on_progress=on_progress and (lambda f, i=i: on_progress((i + f) / len(bad))))
    media.concat_preview(paths, d / "preview.mp4")


def _proxy_ok(path, canvas):
    """v2 미리보기 형식(H.264 상자 크기, B 프레임 없음, AAC 48k 스테레오)인지 확인해요."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                           capture_output=True, text=True, timeout=60)
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    return (v.get("codec_name") == "h264" and (v.get("width"), v.get("height")) == media._box(canvas)
            and not int(v.get("has_b_frames") or 0) and a.get("codec_name") == "aac"
            and str(a.get("sample_rate")) == "48000" and a.get("channels") == 2)


def _finish(p, sid, sd, warning=""):
    """분석 끝: 상태 ready, 이 소스 자동 컷 다시 찾기, 필요하면 자막 만들기 (transact 안에서)."""
    s = next((s for s in p["sources"] if s["id"] == sid), None)
    if s is None:
        raise SourceGone(sid)
    s.update(status="ready", progress=1.0, step="", error=warning[:300])
    redetect(p, [sid])
    made = None
    if (sd / "transcript.json").exists() and _wants_subs(p, sid):
        made = len(subs.generate(p, [sid]))
    return {"sid": sid, "cuts": sum(1 for c in p["cuts"] if c["by"] == "auto" and _sid_of(p, c) == sid),
            "subtitles": made}


def _wants_subs(p, sid):
    """자막이 하나도 없거나, 이 소스 자막이 모두 자동(손대지 않음)이면 단어로 다시 만들어요.
    다른 소스 자막이 모두 대본·손으로 만든 것이면 (대본 프로젝트) 새 소스에 자동 자막을 넣지 않아요."""
    mine = [t for t in p["subtitles"] if _sid_of(p, t) == sid]
    if any(t.get("by") != "auto" for t in mine):
        return False
    others = [t for t in p["subtitles"] if _sid_of(p, t) != sid]
    return bool(mine) or not others or any(t.get("by") == "auto" for t in others)


class _Status:
    """진행 상황: on_progress 는 매번, project.json 은 0.5초에 한 번만 써요."""

    def __init__(self, name, sid, on_progress, stages):
        self.name, self.sid, self.cb = name, sid, on_progress
        plan = [(k, step, w) for k, step, w in STAGES if k in stages]
        tot, acc, self.at = sum(w for _, _, w in plan), 0.0, {}
        for k, step, w in plan:
            self.at[k] = (acc / tot, w / tot, step)
            acc += w
        self.t, self.last = 0.0, None

    def __call__(self, stage, frac=0.0):
        a, w, step = self.at[stage]
        x = round(a + w * min(max(float(frac), 0.0), 1.0), 3)
        if self.cb:
            self.cb(step, x)
        now = time.monotonic()
        if self.last is None or (now - self.t >= 0.5 and (step, x) != self.last):
            self.t, self.last = now, (step, x)
            _set(self.name, self.sid, status="analyzing", progress=x, step=step, error="")


def _set(name, sid, **fields):
    def fn(p):
        s = next((s for s in p["sources"] if s["id"] == sid), None)
        if s is None:
            raise SourceGone(sid)
        s.update(fields)
    try:
        project.transact(name, fn, by="job")
    except FileNotFoundError:
        raise SourceGone(sid) from None


def _fail(name, sid, e):
    if isinstance(e, KeyboardInterrupt):
        msg = "중단됐어요"
    elif isinstance(e, (ValueError, RuntimeError, OSError)) and str(e):
        msg = str(e)
    else:
        msg = f"분석하지 못했어요 ({type(e).__name__})"
    try:
        _set(name, sid, status="error", step="", error=msg[:300])
    except Exception:
        pass


def _src(p, sid):
    s = next((s for s in p["sources"] if s["id"] == sid), None)
    if s is None:
        raise ValueError(f"없는 영상이에요: {sid}")
    return s


def _sdir(name, sid):
    if not re.fullmatch(r"s\d+", str(sid)):
        raise ValueError(f"영상 id 가 올바르지 않아요: {sid}")
    return project.project_dir(name) / "sources" / sid


def _sid_of(p, x):
    """컷·자막이 속한 소스 id (가운데 시점 기준)."""
    if not p["sources"]:
        return None
    return project.source_at(p, (x["start"] + x["end"]) / 2)[0]["id"]


def _write_json(path, data):
    """임시 파일에 쓰고 바꿔치기해요 (v1 에서 하드링크로 옮긴 파일도 원본은 그대로 남아요)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


@functools.lru_cache(maxsize=32)
def _json_at(path, mtime, size):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load(path):
    """JSON 을 읽어요 (수정 시각이 같으면 캐시, 없으면 None). 돌려받은 값은 고치지 마세요."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return _json_at(str(path), st.st_mtime_ns, st.st_size)


def _ff(args):
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"ffmpeg 실패: {r.stderr.strip()[-600:]}")


# ---------- 음량·무음 ----------

def _levels(wav):
    """(20 ms 음량 dB 배열, 무음 기준 dB). 같은 파일이면 다시 계산하지 않아요."""
    st = os.stat(wav)
    return _levels_at(str(wav), st.st_mtime_ns, st.st_size)


@functools.lru_cache(maxsize=4)
def _levels_at(path, mtime, size):
    with wave.open(path, "rb") as f:
        rate, ch, width = f.getframerate(), f.getnchannels(), f.getsampwidth()
        raw = f.readframes(f.getnframes())
    if width != 2:
        raise ValueError("분석용 소리는 16bit PCM wav 여야 해요")
    a = np.frombuffer(raw, dtype="<i2")
    if ch > 1:
        a = a[: len(a) // ch * ch].reshape(-1, ch)[:, 0]
    hop = max(1, rate // 100)  # 10 ms
    n = len(a) // hop
    e10 = np.empty(n, np.float64)
    step = 60000
    for i in range(0, n, step):
        blk = a[i * hop:min(n, i + step) * hop].astype(np.float32) / 32768
        e10[i:i + len(blk) // hop] = (blk.reshape(-1, hop) ** 2).mean(axis=1)
    e20 = e10[: n // 2 * 2].reshape(-1, 2).mean(axis=1)
    e50 = e10[: n // 5 * 5].reshape(-1, 5).mean(axis=1)
    floor = 10 * np.log10(np.percentile(e50, 10) + 1e-12) if len(e50) else -90.0
    return (10 * np.log10(e20 + 1e-12)).astype(np.float32), float(np.clip(floor + 10, -50, -28))


def noise_threshold_db(wav):
    """현장 소음 수준에 맞춘 무음 기준: 50 ms 음량 하위 10% + 10 dB (-50 ~ -28 dB)."""
    return _levels(wav)[1]


def quiet_runs(wav, thr, min_dur, bridge=0.06):
    """20 ms 음량이 thr 아래인 구간 → [[시작, 끝]] (min_dur 이상).
    기준(thr)을 잰 방식 그대로(평균 음량) 비교해요. bridge 이하로 잠깐 튄 소리(발소리·잡음 한 번)는 쉼으로 이어 봐요."""
    lv = _levels(wav)[0]
    q = np.concatenate(([False], lv < thr, [False]))
    edges = np.flatnonzero(np.diff(q.astype(np.int8)))
    runs = edges.reshape(-1, 2).tolist()
    out = []
    for a, b in runs:
        if out and (a - out[-1][1]) * FRAME <= bridge:
            out[-1][1] = b
        else:
            out.append([a, b])
    dur = len(lv) * FRAME
    return [[round(a * FRAME, 3), round(min(b * FRAME, dur), 3)] for a, b in out if (b - a) * FRAME >= min_dur]


def detect_silences(wav, min_silence=0.5):
    """fine: 0.15초 이상 쉼 (경계 맞추기·컷 찾기용), silences: min_silence 이상 무음, threshold_db: 무음 기준.
    조용한 구간을 그대로 재므로 fine 을 길이로 거르면 min_silence 결과와 같아요."""
    thr = noise_threshold_db(wav)
    fine = quiet_runs(wav, thr, FINE)
    return {"threshold_db": thr, "silences": [x for x in fine if x[1] - x[0] >= min_silence], "fine": fine}


# ---------- 음성 인식 ----------

def _transcribe(sd, asr, on_progress=None, has_audio=True):
    """sources/<sid>/audio.wav → transcript.json (소스 안 시간). 단어 수를 돌려줘요. 소리 없는 영상은 빈 대본."""
    norm = sd / "audio_norm.part.wav"
    try:
        if has_audio:
            _ff(["-i", sd / "audio.wav", "-af", "highpass=f=80,dynaudnorm=f=150:g=15:p=0.9:m=20", norm])
            res = _whisper(norm, asr, on_progress)
        else:
            res = {"segments": [], "language": asr.get("language")}
    finally:
        norm.unlink(missing_ok=True)
    # 환각 거르기는 바닥 소음 가까이(무음 기준 -8 dB) 조용한 1초 이상만: 작게 말한 진짜 말은 남겨요
    quiet = quiet_runs(sd / "audio.wav", noise_threshold_db(sd / "audio.wav") - 8, 1.0)
    lang = res.get("language") or asr.get("language")
    segs, words, dropped = [], [], 0
    for s in res.get("segments", []):
        text = s.get("text", "").strip()
        if not text or (s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -0.8) or is_garbage(text, lang):
            dropped += 1
            continue
        segs.append({"start": round(s["start"], 3), "end": round(s["end"], 3), "text": text,
                     "logprob": round(s.get("avg_logprob", 0), 2)})
        for w in s.get("words", []):
            if w["word"].strip():
                a = round(max(0.0, w["start"]), 3)
                words.append({"start": a, "end": round(max(a, w["end"]), 3), "word": w["word"].strip()})
    kept = drop_words_in_silence(words, quiet)
    _write_json(sd / "transcript.json", {"model": asr.get("model"), "language": lang, "prompt": asr.get("prompt", ""),
                                         "segments": segs, "words": kept, "dropped": dropped,
                                         "dropped_words": len(words) - len(kept)})
    return len(kept)


def _whisper(wav, asr, on_progress=None):
    import mlx_whisper
    lang = (asr.get("language") or "").strip() or None
    lang = None if lang == "auto" else lang
    prompt = asr.get("prompt") or (PROMPT_KO if lang == "ko" else None)
    model = asr.get("model") or project.DEFAULT_SETTINGS["asr"]["model"]
    with _WHISPER, _whisper_progress(on_progress):
        return mlx_whisper.transcribe(str(wav), path_or_hf_repo=model, language=lang, word_timestamps=True,
                                      condition_on_previous_text=False, hallucination_silence_threshold=1.0,
                                      initial_prompt=prompt, verbose=None)


@contextlib.contextmanager
def _whisper_progress(cb):
    """mlx_whisper 의 진행 막대(tqdm)를 잠깐 바꿔 끼워서 진행률을 받아요."""
    mod = importlib.import_module("mlx_whisper.transcribe") if cb else None
    real = getattr(mod, "tqdm", None)
    if real is None:
        yield
        return

    class Bar:
        def __init__(self, *a, total=None, **kw):
            self.total, self.n = total or 0, 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def update(self, k=1):
            self.n += k
            if self.total:
                cb(min(1.0, self.n / self.total))

    mod.tqdm = types.SimpleNamespace(tqdm=Bar)
    try:
        yield
    finally:
        mod.tqdm = real


def is_garbage(text, language="ko"):
    """인식이 무너진 줄: 같은 말 반복 루프, (한국어일 때) 한글·로마자가 아닌 글자(일본어·중국어 등) 환각.
    영어 단어(로마자)는 그대로 둬요."""
    toks = re.findall(r"\w+", text)
    if len(toks) >= 6 and len(set(toks)) / len(toks) < 0.35:
        return True
    if language == "ko":
        letters = [c for c in text if c.isalpha()]
        odd = sum(1 for c in letters if not ("가" <= c <= "힣" or "ㄱ" <= c <= "ㅣ" or c.isascii()))
        return bool(letters) and odd / len(letters) > 0.3
    return False


def _minus_words(fine, words, db=None):
    """무음 목록에서 인식된 단어의 가운데 절반을 빼요 (단어 시간은 앞뒤로 조금씩 틀려서 가장자리는 쉼에 맞춰도 돼요).
    db(20 ms 음량)가 있으면 그 자리에 실제로 들리는 소리(무음 기준 -2 dB 위)가 있는 단어만 지켜요:
    whisper 가 단어를 1초쯤 앞의 조용한 곳에 찍는 일이 있어서요. FINE 보다 짧아진 조각은 버려요."""
    spans = [(w["start"] + (w["end"] - w["start"]) / 4, w["end"] - (w["end"] - w["start"]) / 4) for w in words]
    if db is not None and len(db):
        thr = float(np.clip(np.percentile(db, 10) + 10, -50, -28))
        heard = lambda a, b: len(fr := _frames(db, a, b)) and float(np.percentile(fr, 90)) > thr - 2
        spans = [(a, b) for a, b in spans if heard(a, b)]
    spans.sort()
    out = []
    for s, e in fine:
        cur = s
        for a, b in spans:
            if b <= cur or a >= e:
                continue
            if a - cur >= FINE:
                out.append([cur, a])
            cur = max(cur, b)
        if e - cur >= FINE:
            out.append([cur, e])
    return [[round(a, 3), round(b, 3)] for a, b in out]


def drop_words_in_silence(words, silences):
    """1초 넘는 무음 위에 80% 이상 찍힌 단어는 환각으로 보고 버려요."""
    longs = [(s, e) for s, e in silences if e - s >= 1.0]
    keep = []
    for w in words:
        dur = max(w["end"] - w["start"], 1e-3)
        covered = sum(max(0.0, min(e, w["end"]) - max(s, w["start"])) for s, e in longs)
        if covered / dur < 0.8:
            keep.append(w)
    return keep


# ---------- 단어 ----------

def words(p):
    """모든 소스의 인식 단어를 원본 시간축으로 합쳐요: [{start, end, word, sid}] (정렬)."""
    if not p.get("sources"):
        return []
    d = project.project_dir(p["name"])
    out = []
    for s in p["sources"]:
        tr = _load(d / "sources" / s["id"] / "transcript.json") or {}
        for w in tr.get("words") or []:
            a, b, text = w.get("start"), w.get("end"), str(w.get("word", "")).strip()
            if not text or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (a, b)):
                continue
            a = min(max(a, 0.0), s["duration"])
            b = min(max(b, a), s["duration"])
            out.append({**{k: w[k] for k in ("u", "spk", "guess") if k in w},
                        "start": round(s["offset"] + a, 3), "end": round(s["offset"] + b, 3), "word": text, "sid": s["id"]})
    out.sort(key=lambda w: (w["start"], w["end"]))
    return out


def _norm(word):
    return re.sub(r"[^\w]", "", word)


def snap(p, t, side, reach=0.35):
    """컷 경계 t(원본 시간)를 가까운(±reach) 짧은 쉼 안으로 옮겨요. side="start" 는 컷 시작(앞말 뒤 여유를 남김),
    "end" 는 컷 끝(뒷말 앞 여유를 남김). 쉼이 없거나 분석 전이면 t 그대로."""
    if not p.get("sources"):
        return t
    src, local = project.source_at(p, t)
    sil = _load(project.project_dir(p["name"]) / "sources" / src["id"] / "silences.json") or {}
    cs = {**project.DEFAULT_SETTINGS["cut"], **(p.get("settings") or {}).get("cut", {})}
    best = None
    for s, e in sil.get("fine") or []:
        if s - reach <= local <= e + reach:
            dist = 0 if s <= local <= e else min(abs(local - s), abs(local - e))
            if best is None or dist < best[0]:
                best = (dist, s, e)
    if best is None:
        return t
    _, s, e = best
    lo, hi = (s + cs["pad_after"], e) if side == "start" else (s, e - cs["pad_before"])
    x = (s + e) / 2 if lo > hi else min(max(local, lo), hi)
    return round(src["offset"] + min(max(x, 0.0), src["duration"]), 3)


# ---------- 자동 컷 ----------

def detect_cuts(p, sids=None):
    """sids 소스(기본: 분석된 모든 소스)의 새 자동 컷 목록 (원본 시간, settings.cut 기준).
    예전 확인한 자동 컷과 50% 이상 겹치면 그 결정(enabled·reviewed)을 이어받아요. 자동 컷이 아닌 컷은 건드리지 않아요.
    부르는 쪽이 그 소스들의 by="auto" 컷을 이 목록으로 바꿔요 (redetect 가 그렇게 해요)."""
    return _detect(p, sids)[0]


def redetect(p, sids=None):
    """sids 소스(기본 전체)의 자동 컷을 지금 설정으로 다시 찾아 바꾸고 자막 타이밍도 다시 맞춰요.
    apply_ops 와 같은 모양의 변경 기록을 돌려줘요."""
    before = {"cuts": copy.deepcopy(p["cuts"]), "subtitles": copy.deepcopy(p["subtitles"])}
    new, done = _detect(p, sids)
    keep = [c for c in p["cuts"] if not (c.get("by") == "auto" and _sid_of(p, c) in done)]
    p["cuts"] = sorted(keep + new, key=lambda c: (c["start"], c["end"]))
    subs.retime(p)
    return subs.diff(before, p)


def _detect(p, sids=None):
    d = project.project_dir(p["name"])
    cs = {**project.DEFAULT_SETTINGS["cut"], **(p.get("settings") or {}).get("cut", {})}
    want = None if sids is None else set(sids)
    raw, done = [], set()
    for s in p["sources"]:
        if want is not None and s["id"] not in want:
            continue
        sd = d / "sources" / s["id"]
        sil = _load(sd / "silences.json")
        if sil is None:
            continue
        if (sd / "audio.wav").exists():  # 다시 찾을 때마다 지금 기준으로 무음을 다시 재요 (빨라요, 음량은 캐시)
            sil = detect_silences(sd / "audio.wav", cs["min_silence"])
            _write_json(sd / "silences.json", sil)
        done.add(s["id"])
        if not s.get("has_audio", True):
            continue
        tr = _load(sd / "transcript.json")
        ws = tr.get("words") if tr else None
        db = _levels(sd / "audio.wav")[0] if ws is not None and (sd / "audio.wav").exists() else None
        for c in source_cuts(s["duration"], sil.get("fine") or sil.get("silences") or [], ws, cs, db):
            raw.append({**c, "start": round(s["offset"] + c["start"], 3), "end": round(s["offset"] + c["end"], 3)})
    old = [c for c in p["cuts"] if c.get("by") == "auto" and _sid_of(p, c) in done]
    return _inherit(p, raw, old), done


def _ov(a, b):
    return min(a["end"], b["end"]) - max(a["start"], b["start"])


def _inherit(p, raw, old):
    """새 자동 컷에 id 와 결정을 붙여요. 거의 같은 예전 자동 컷의 id 는 그대로 쓰고 (안 바뀌었으면 rev 도),
    확인한(reviewed) 예전 컷과 새 컷 길이의 50% 이상 겹치면 enabled·reviewed 를 이어받아요."""
    rev = p.get("rev", 0) + 1
    old = sorted(old, key=lambda c: c["start"])
    starts = [c["start"] for c in old]
    span = max((c["end"] - c["start"] for c in old), default=0.0)
    used, taken, out = {c["id"] for c in p["cuts"]}, set(), []
    for c in sorted(raw, key=lambda c: (c["start"], c["end"])):
        n = c["end"] - c["start"]
        near = old[bisect.bisect_left(starts, c["start"] - span):bisect.bisect_left(starts, c["end"])]
        near = [(_ov(c, o), o) for o in near if _ov(c, o) > 0]
        item = {"id": None, "start": c["start"], "end": c["end"], "type": c["type"], "enabled": c["enabled"],
                "reviewed": False, "reason": c["reason"], "by": "auto", "rev": rev}
        done = [(v, o) for v, o in near if o.get("reviewed")]
        if done:
            v, o = max(done, key=lambda x: x[0])
            if v >= 0.5 * n:
                item.update(enabled=bool(o["enabled"]), reviewed=True)
        same = [(v, o) for v, o in near if o["id"] not in taken and v >= 0.5 * max(n, o["end"] - o["start"])]
        if same:
            o = max(same, key=lambda x: x[0])[1]
            taken.add(o["id"])
            item["id"] = o["id"]
            if all(o.get(k) == item[k] for k in ("start", "end", "type", "enabled", "reviewed", "reason")):
                item["rev"] = o.get("rev", rev)
        else:
            while (cid := project.new_id("c")) in used:
                pass
            used.add(cid)
            item["id"] = cid
        out.append(item)
    return out


def _frames(db, a, b):
    return db[max(0, int(a / FRAME)):max(0, int(math.ceil(b / FRAME)))]


def _energy_db(db, spans):
    parts = [_frames(db, a, b) for a, b in spans]
    x = np.concatenate(parts) if parts else np.zeros(0, np.float32)
    return float(10 * np.log10(np.mean(10 ** (x.astype(np.float64) / 10)))) if len(x) else -120.0


def source_cuts(dur, fine, words, cs, db=None):
    """소스 하나의 자동 컷 후보 (소스 안 시간, id 없음). cs = settings.cut.
    fine: 0.15초 이상 무음 목록. words: 인식 단어 (None 이면 대본 없이 소리만 보고 v1 처럼 모든 쉼을 잘라요).
    db: 20 ms 음량 — 말 없는 긴 구간이 현장음인지(B-roll 후보, gap) 조용한 빈 구간인지(자름) 볼 때 써요.
    - 말과 말 사이 쉼 ≥ min_silence → [쉼 시작+pad_after, 쉼 끝-pad_before] (silence, long_silence 이상이면 long)
    - 앞뒤가 쉼인 짧은 필러 소리 덩어리 → filler (settings.cut.fillers 일 때만 켜짐)
    - 3초 넘게 말이 없는 구간: 현장음(말소리 중간값 12 dB 안쪽)이면 gap 제안(꺼짐), 대부분 조용하면 통째로 자름
    - 소스 앞뒤 무음 → silence. 통째로 조용한 소스·말이 하나도 없는 소스(B-roll)는 앞뒤 무음만 잘라요."""
    pa, pb, ms, lg = (float(cs[k]) for k in ("pad_after", "pad_before", "min_silence", "long_silence"))
    if words:
        fine = _minus_words(fine, words, db)  # 작게 말한 말(멀리 있는 사람 등)은 조용해도 무음으로 보지 않아요
    sil = []
    for s, e in sorted((max(0.0, float(s)), min(dur, float(e))) for s, e in fine):
        if e - s <= 0:
            continue
        if sil and s <= sil[-1][1]:
            sil[-1] = (sil[-1][0], max(sil[-1][1], e))
        else:
            sil.append((s, e))
    if any(s <= EDGE and e >= dur - EDGE for s, e in sil):
        return []  # 통째로 조용한 클립
    isl, t = [], 0.0
    for s, e in sil:
        if s > t:
            isl.append((t, s))
        t = e
    if t < dur:
        isl.append((t, dur))
    label, fill = _labels(isl, words)
    talk = words is None or any(x != "other" for x in label)
    sil_of, starts = {}, [a for a, _ in isl]  # 소리 덩어리 i 앞뒤 무음
    for s, e in sil:
        i = bisect.bisect_right(starts, s) - 1
        if 0 <= i < len(isl) and abs(isl[i][1] - s) < 1e-6:
            sil_of.setdefault(i, [None, None])[1] = (s, e)
        if i + 1 < len(isl) and abs(isl[i + 1][0] - e) < 1e-6:
            sil_of.setdefault(i + 1, [None, None])[0] = (s, e)
    out, skip = [], set()

    def add(a, b, typ, reason, on=True):
        a, b = max(0.0, a), min(dur, b)
        if b - a >= MIN_CUT:
            out.append({"start": round(a, 3), "end": round(b, 3), "type": typ, "enabled": on, "reason": reason})

    if words is not None and talk:
        speech_db = None
        i = 0
        while i < len(isl):
            if label[i] != "other":
                i += 1
                continue
            j = i
            while j + 1 < len(isl) and label[j + 1] == "other":
                j += 1
            x = isl[i - 1][1] if i > 0 else 0.0
            y = isl[j + 1][0] if j + 1 < len(isl) else dur
            if y - x >= GAP:
                inner = {k for k, (s, e) in enumerate(sil) if x - 1e-6 <= s and e <= y + 1e-6}
                sound = isl[i:j + 1]
                frac = sum(b - a for a, b in sound) / (y - x)
                if db is not None and speech_db is None:
                    speech = [ab for ab, lab in zip(isl, label) if lab != "other"]
                    fr = np.concatenate([_frames(db, a, b) for a, b in speech]) if speech else np.zeros(0)
                    speech_db = float(np.median(fr)) if len(fr) else -120.0
                lvl = _energy_db(db, sound) if db is not None else 0.0
                if db is None or lvl >= speech_db - AMBIENT_DB or frac > QUIET_SOUND:
                    a, b = sound[0][0] - pb, sound[-1][1] + pa
                    add(max(a, x + pa if x > 0 else 0.0), min(b, y - pb if y < dur else dur), "gap",
                        f"{y - x:.1f}초 말 없음 · 현장음" if db is None or lvl >= speech_db - AMBIENT_DB
                        else f"{y - x:.1f}초 말 없음", on=False)
                    skip |= {k for k in inner if x + 1e-6 < sil[k][0] and sil[k][1] < y - 1e-6}
                else:
                    add(x + pa if x > 0 else 0.0, y - pb if y < dur else dur, "long" if y - x >= lg else "silence",
                        f"{y - x:.1f}초 말 없음")
                    skip |= inner
            i = j + 1
    for k, (s, e) in enumerate(sil):
        if k in skip:
            continue
        if s <= EDGE:
            add(0.0, e - pb, "silence", f"{e - s:.1f}초 무음")
        elif e >= dur - EDGE:
            add(s + pa, dur, "silence", f"{e - s:.1f}초 무음")
        elif talk and e - s >= ms:
            add(s + pa, e - pb, "long" if e - s >= lg else "silence", f"{e - s:.1f}초 무음")
    for i, w in fill.items():
        a, b = isl[i]
        before, after = sil_of.get(i, [None, None])
        p0 = before[0] if before else a
        n1 = after[1] if after else b
        add(max(a - pb, p0 + min(pa, (a - p0) / 2)) if before else a,
            min(b + pa, n1 - min(pb, (n1 - b) / 2)) if after else b,
            "filler", f'필러 "{w}"', on=bool(cs.get("fillers", True)))
    return sorted(out, key=lambda c: (c["start"], c["end"]))


def _labels(isl, words):
    """소리 덩어리마다 speech / filler / other. 필러는 v1 방식: 필러 단어 근처의 짧은(1.2초 미만) 덩어리 중
    내용 단어가 없는 것 (whisper 단어 시간이 0.3~1초씩 틀어져서 실제 소리 덩어리로 잡아요)."""
    if words is None:
        return ["speech"] * len(isl), {}
    content, fillers = [], []
    for w in words:
        n = _norm(str(w.get("word", "")))
        if n:
            (fillers if n in FILLERS else content).append(w)
    mids = sorted((w["start"] + w["end"]) / 2 for w in content)
    starts = [a for a, _ in isl]
    has_mid = [bisect.bisect_left(mids, b) > bisect.bisect_left(mids, a) for a, b in isl]
    label = ["other"] * len(isl)
    cover = [0.0] * len(isl)
    for w in content:
        i = max(0, bisect.bisect_right(starts, w["start"]) - 1)
        while i < len(isl) and isl[i][0] < w["end"]:
            cover[i] += max(0.0, min(w["end"], isl[i][1]) - max(w["start"], isl[i][0]))
            i += 1
    for i, (a, b) in enumerate(isl):
        if has_mid[i] or cover[i] >= min(0.25, 0.5 * (b - a)):
            label[i] = "speech"
    fill = {}
    for w in fillers:
        lo, hi = w["start"] - 0.4, w["end"] + 0.4
        cands = [(min(hi, b) - max(lo, a), i) for i, (a, b) in enumerate(isl)
                 if min(hi, b) > max(lo, a) and b - a < 1.2 and not has_mid[i]]
        if cands:
            i = max(cands)[1]
            label[i] = "filler"
            fill.setdefault(i, _norm(w["word"]))
    return label, fill

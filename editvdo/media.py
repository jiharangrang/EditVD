"""영상 정보 읽기와 미리보기·음성·파형·썸네일 파일 만들기 (ffmpeg)를 맡아요."""
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

FF = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
PROXY_FPS = 30
PROXY_OUT = ["-fps_mode", "cfr", "-r", str(PROXY_FPS), "-g", "15", "-keyint_min", "15", "-bf", "0",
             "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
             "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart"]
SOFT_H264 = ["-c:v", "libx264", "-crf", "23", "-preset", "veryfast", "-sc_threshold", "0"]
THUMB_W, THUMB_COLS = 160, 10
# 크롬이 원본을 그대로 재생할 수 있는 형식 → 미리보기 변환 없이 원본을 바로 보여줘요
PLAY_VIDEO, PLAY_PIX, PLAY_AUDIO = {"h264", "hevc"}, {"yuv420p", "yuvj420p", "yuv420p10le"}, {"aac", "mp3"}


def _run(cmd, text=False):
    """명령을 실행하고 실패하면 마지막 오류 줄을 담은 RuntimeError 를 내요."""
    r = subprocess.run([str(x) for x in cmd], capture_output=True)
    if r.returncode:
        raise RuntimeError(f"{cmd[0]} 실패: {r.stderr.decode(errors='replace').strip()[-600:]}")
    return r.stdout.decode() if text else r.stdout


def _part(dst):
    """같은 폴더의 임시 이름 (다 만든 뒤 os.replace 로 바꿔치기해요)."""
    dst = Path(dst)
    return dst.with_name(f"{dst.stem}.part{dst.suffix}")


def _even(x):
    return max(2, int(round(x / 2)) * 2)


def _rate(s):
    try:
        n, d = (s or "0/0").split("/")
        return float(n) / float(d) if float(d) else 0.0
    except ValueError:
        return 0.0


def _created(info, path):
    """촬영 시각(현지 시간). 메타데이터에 없으면 파일 수정 시각을 써요."""
    tags = [info.get("format", {}).get("tags", {})] + [s.get("tags", {}) for s in info.get("streams", [])]
    for key in ("com.apple.quicktime.creationdate", "creation_time"):
        for t in tags:
            try:
                d = datetime.fromisoformat(t[key])
            except (KeyError, TypeError, ValueError):
                continue
            if d.year >= 2000:
                return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%dT%H:%M:%S")


def probe(path):
    """길이·크기(회전 반영)·fps·소리 유무·촬영 시각·회전(시계 방향 도)을 읽어요."""
    try:
        info = json.loads(_run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path], text=True))
    except RuntimeError:
        raise ValueError(f"영상을 읽을 수 없어요: {Path(path).name}") from None
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    if v is None:
        raise ValueError("영상 스트림이 없어요")
    rot = -next((int(round(float(sd["rotation"]))) for sd in v.get("side_data_list", []) if "rotation" in sd), 0) % 360
    w, h = (v["height"], v["width"]) if rot in (90, 270) else (v["width"], v["height"])
    dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    if not dur > 0:
        raise ValueError(f"영상 길이를 읽을 수 없어요: {Path(path).name}")
    fps = _rate(v.get("avg_frame_rate")) or _rate(v.get("r_frame_rate")) or 30.0
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    playable = (bool({"mov", "mp4"} & set(info.get("format", {}).get("format_name", "").split(",")))
                and v.get("codec_name") in PLAY_VIDEO and v.get("pix_fmt") in PLAY_PIX
                and all(a.get("codec_name") in PLAY_AUDIO for a in audio))
    return {"duration": round(dur, 3), "w": w, "h": h, "fps": round(fps, 3),
            "has_audio": bool(audio), "created": _created(info, path), "rotation": rot, "playable": playable}


def _box(canvas):
    """캔버스 비율을 가로 1280x720 (세로 캔버스면 720x1280) 상자 안에 맞춘 미리보기 크기."""
    w, h = canvas["w"], canvas["h"]
    bw, bh = (1280, 720) if w >= h else (720, 1280)
    s = min(bw / w, bh / h)
    return _even(w * s), _even(h * s)


def _fit(w, h):
    """비율을 지키며 w x h 안에 넣고 남는 곳은 검게 채우는 필터."""
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")


def _ffmpeg_progress(cmd, duration, on_progress):
    """-progress pipe:1 출력을 읽어 on_progress(0..1) 로 알려요."""
    with tempfile.TemporaryFile() as err, \
            subprocess.Popen([str(x) for x in cmd], stdout=subprocess.PIPE, stderr=err, text=True) as proc:
        try:
            for line in proc.stdout:
                if on_progress and line.startswith("out_time_us=") and duration > 0:
                    try:
                        on_progress(min(1.0, max(0.0, int(line[12:]) / 1e6 / duration)))
                    except ValueError:
                        pass
        except BaseException:
            proc.kill()
            raise
        if proc.wait():
            err.seek(0)
            raise RuntimeError(f"ffmpeg 실패: {err.read().decode(errors='replace').strip()[-600:]}")


def make_proxy(src, dst, canvas, on_progress=None):
    """빨리 탐색되는 미리보기: 캔버스 비율 상자에 맞춤, 30fps 고정, 15프레임마다 키프레임, AAC 48k 스테레오.
    소리 트랙 길이를 원본 길이에 정확히 맞춰서 concat_preview 가 이어 붙일 자리로 써요."""
    info = probe(src)
    dur = info["duration"]
    bw, bh = _box(canvas)
    silent = [] if info["has_audio"] else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    out = ["-map", "0:V:0", "-map", "0:a:0" if info["has_audio"] else "1:a:0",
           "-vf", _fit(bw, bh) + ",format=yuv420p", "-af", "apad", *PROXY_OUT, "-t", f"{dur:.3f}",
           "-progress", "pipe:1", "-nostats"]
    tries = ((["-hwaccel", "videotoolbox"], ["-c:v", "h264_videotoolbox", "-q:v", "60"]),
             ([], SOFT_H264))  # 하드웨어가 안 되면
    tmp = _part(dst)
    try:
        for i, (dec, enc) in enumerate(tries):
            try:
                _ffmpeg_progress(FF + dec + ["-i", src] + silent + out + enc + [tmp], dur, on_progress)
                break
            except RuntimeError:
                if i == len(tries) - 1:
                    raise
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    if on_progress:
        on_progress(1.0)


def extract_audio(src, dst_wav, clean=False):
    """음성 인식·무음 감지용 16 kHz 모노 wav. clean=True 면 demucs 로 목소리만 남겨요 (시끄러운 현장용)."""
    info = probe(src)
    tmp = _part(dst_wav)
    try:
        if not info["has_audio"]:
            _run(FF + ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", f"{info['duration']:.3f}",
                       "-c:a", "pcm_s16le", tmp])
        elif clean:
            _separate_voice(src, tmp)
        else:
            _run(FF + ["-i", src, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", tmp])
        os.replace(tmp, dst_wav)
    finally:
        tmp.unlink(missing_ok=True)


def _separate_voice(src, out):
    """demucs(htdemucs) 로 목소리 트랙만 분리해서 16 kHz 모노로 써요."""
    work = Path(tempfile.mkdtemp(prefix="demucs-", dir=Path(out).parent))
    try:
        mix = work / "mix.wav"
        _run(FF + ["-i", src, "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", mix])
        r = subprocess.run([sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs", "-d", "mps",
                            "-o", str(work / "sep"), str(mix)], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"목소리 분리에 실패했어요: {r.stderr.strip()[-600:]}")
        _run(FF + ["-i", work / "sep" / "htdemucs" / "mix" / "vocals.wav", "-ac", "1", "-ar", "16000",
                   "-c:a", "pcm_s16le", out])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _write_json(path, data):
    tmp = _part(path)
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def peaks(wav, dst_json, per_sec=50):
    r"""오디오를 청크 단위로 읽어 작은 소리도 보이는 파형을 만들어요.

    $$p_k = \operatorname{round}\left(255\sqrt{\max_{i\in W_k,c}|a_{i,c}|/32768}\right)$$

    마지막의 짧은 구간도 포함하고 기존과 같은 per_sec·data JSON을 저장해요.
    """
    data = {"per_sec": per_sec, "data": []}
    with wave.open(str(wav), "rb") as f:
        rate, ch = f.getframerate(), f.getnchannels()
        win = max(1, rate // per_sec)
        frames = win * max(1, 65536 // win)
        while raw := f.readframes(frames):
            a = np.frombuffer(raw, dtype="<i2").reshape(-1, ch).astype(np.int32)
            # 채널별 절댓값을 구해요: $$b_{i,c}=|a_{i,c}|$$
            np.abs(a, out=a)
            # 각 프레임의 가장 큰 채널을 골라요: $$m_i=\max_c b_{i,c}$$
            a = a.max(axis=1)
            starts = np.arange(0, len(a), win)
            # 마지막의 짧은 구간까지 최댓값을 구해요: $$M_k=\max_{i\in W_k}m_i$$
            maxima = np.maximum.reduceat(a, starts)
            # 작은 소리가 보이도록 제곱근 눈금을 적용해요: $$p_k=\operatorname{round}(255\sqrt{M_k/32768})$$
            values = np.round(np.sqrt(maxima / 32768) * 255).astype(int)
            data["data"].extend(values.tolist())
    _write_json(dst_json, data)
    return data


def _dense_keyframes(src, limit=1.0):
    """앞 5초의 키프레임 간격이 limit 초 이하인지 (그러면 키프레임만 풀어도 썸네일이 충분히 정확해요)."""
    out = _run(["ffprobe", "-v", "error", "-select_streams", "V:0", "-read_intervals", "%+5",
                "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", src], text=True)
    keys = []
    for line in out.splitlines():
        t, flags = (line.split(",") + [""])[:2]
        if "K" in flags and t not in ("", "N/A"):
            keys.append(float(t))
    return len(keys) >= 3 and max(b - a for a, b in zip(keys, keys[1:])) <= limit


def thumbs(src, dst_jpg, dst_json, canvas):
    """필름스트립 썸네일을 가로 10칸 한 장으로 붙여요. json: {interval, w, h, cols, count}.
    i 번째 칸 = i*interval 초 무렵 장면 (캔버스 비율, 남는 곳은 검게)."""
    dur = probe(src)["duration"]
    interval = max(1.0, dur / 400)
    tw, th = THUMB_W, _even(THUMB_W * canvas["h"] / canvas["w"])
    # 키프레임이 칸 간격보다 촘촘하면 키프레임만 풀어요. 하드웨어 디코딩이면 2.7K 6분도 몇 초 (안 되면 소프트웨어로)
    skip = ["-skip_frame", "nokey"] if _dense_keyframes(src, interval + 0.05) else []
    args = skip + ["-i", src, "-map", "0:V:0", "-an", "-vf", f"fps={1 / interval:.6f}," + _fit(tw, th),
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    try:
        raw = _run(FF + ["-hwaccel", "videotoolbox"] + args)
    except RuntimeError:
        raw = _run(FF + args)
    size = tw * th * 3
    count = len(raw) // size
    if not count:
        raise RuntimeError("썸네일을 만들지 못했어요")
    sheet = Image.new("RGB", (tw * THUMB_COLS, th * math.ceil(count / THUMB_COLS)))
    for i in range(count):
        sheet.paste(Image.frombytes("RGB", (tw, th), raw[i * size:(i + 1) * size]),
                    (i % THUMB_COLS * tw, i // THUMB_COLS * th))
    tmp = _part(dst_jpg)
    sheet.save(tmp, "JPEG", quality=80)
    os.replace(tmp, dst_jpg)
    meta = {"interval": round(interval, 4), "w": tw, "h": th, "cols": THUMB_COLS, "count": count}
    _write_json(dst_json, meta)
    return meta


def cover(src, dst_jpg, dst_json, at, width=320):
    """홈 화면 표지 한 장: at 초 무렵 장면을 가로 width 로. json 은 thumbs 와 같은 모양 {w, h, cols: 1, count: 1}."""
    tmp = _part(dst_jpg)
    try:
        _run(FF + ["-ss", f"{max(0.0, at):.3f}", "-i", src, "-map", "0:V:0", "-frames:v", "1",
                   "-vf", f"scale={width}:-2", "-q:v", "3", tmp])
        with Image.open(tmp) as im:
            w, h = im.size
        _write_json(dst_json, {"w": w, "h": h, "cols": 1, "count": 1})
        os.replace(tmp, dst_jpg)
    finally:
        tmp.unlink(missing_ok=True)


def _stream_key(path):
    """이어 붙이기 호환 확인용 (형식, 이 파일이 차지할 길이=소리 길이)."""
    info = json.loads(_run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-show_data_hash", "md5",
                            "-of", "json", path], text=True))
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), {})
    key = tuple(x.get(k) for x, k in ((v, "codec_name"), (v, "width"), (v, "height"), (v, "pix_fmt"),
                                      (v, "has_b_frames"), (v, "extradata_hash"), (a, "codec_name"),
                                      (a, "sample_rate"), (a, "channels"), (a, "extradata_hash")))
    return key, float(a.get("duration") or info["format"]["duration"])


def _same_format(src, dst, size, dur):
    """이어 붙일 수 있게 미리보기 설정 그대로 소프트웨어(libx264)로 다시 만들어요. 소리 길이(=차지할 자리)는 dur 그대로."""
    _run(FF + ["-i", src, "-map", "0:V:0", "-map", "0:a:0", "-vf", _fit(*size) + ",format=yuv420p", "-af", "apad",
               *PROXY_OUT, *SOFT_H264, "-t", f"{dur:.6f}", dst])


def concat_preview(proxy_paths, dst):
    """소스 미리보기들을 순서대로 이어 붙여요 (-c copy). 각 파일은 원본 길이 자리에 정확히 놓여서
    preview.mp4 의 시간 = 원본 시간축이 돼요. 형식이 다른 파일이 섞이면(하드웨어·소프트웨어 인코딩, v1 미리보기)
    모두 같은 설정으로 다시 만든 뒤 같은 자리에 이어 붙여요."""
    paths = [Path(x).resolve() for x in proxy_paths]
    if not paths:
        raise ValueError("이어 붙일 영상이 없어요")
    tmp = _part(dst)
    redo = []
    try:
        if len(paths) == 1:
            try:
                tmp.unlink(missing_ok=True)
                os.link(paths[0], tmp)  # 한 개면 복사 없이 하드링크
                os.replace(tmp, dst)
                return
            except OSError:
                pass
        keys = [_stream_key(x) for x in paths]
        if any(k != keys[0][0] for k, _ in keys):
            size = keys[0][0][1:3]  # 첫 파일의 (가로, 세로)
            redo = [tmp.with_name(f"{tmp.stem}{i}{tmp.suffix}") for i in range(len(paths))]
            for x, y, (_, d) in zip(paths, redo, keys):
                _same_format(x, y, size, d)
            paths, keys = redo, [(_stream_key(y)[0], d) for y, (_, d) in zip(redo, keys)]
        for x, (k, _) in zip(paths, keys):
            if k != keys[0][0]:
                raise ValueError(f"미리보기 형식이 달라서 이어 붙일 수 없어요: {x.parent.name}/{x.name}")
        lst = tmp.with_suffix(".txt")
        lst.write_text("".join("file '{}'\ninpoint 0\nduration {:.6f}\n".format(str(x).replace("'", "'\\''"), d)
                               for x, (_, d) in zip(paths, keys)), encoding="utf-8")
        try:
            _run(FF + ["-f", "concat", "-safe", "0", "-i", lst, "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                       "-movflags", "+faststart", tmp])
        finally:
            lst.unlink(missing_ok=True)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
        for y in redo:
            y.unlink(missing_ok=True)

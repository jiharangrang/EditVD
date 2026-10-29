"""편집본을 MP4(자막 입히기 선택)와 SRT로 내보내요.
원본 구간별 프레임 수와 오디오 길이를 맞추고, 여러 구간을 한 FFmpeg 실행에서 처리한 뒤 이어 붙여요."""
import functools
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import project

FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
FONT_BOLD_INDEX = 6
FADE = 0.008  # 컷 경계 '틱' 소리 방지용 아주 짧은 페이드
RATE = 48000
DEFAULT_POS = {"x": 50, "y": 88}
WORKERS = 3
MP4_BATCH = 3


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(subs, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, s in enumerate(subs, 1):
            f.write(f"{i}\n{srt_time(s['start'])} --> {srt_time(s['end'])}\n{s['text']}\n\n")


def even(n):
    return int(n) - int(n) % 2


def plan(p, at_splits=False):
    """남는 구간마다 원본 파일·파일 안 시작점·편집본 프레임 위치(f0~f1)를 정해요.
    프레임 경계는 편집본 누적 시간을 반올림한 절대 위치라서 구간이 많아도 밀리지 않아요.
    at_splits=True 면 분할 표시에서도 나눠요 (캡컷 클립 단위)."""
    fps = p["canvas"]["fps"]
    keeps = project.keep_segments(p)
    if at_splits:
        marks = sorted(p.get("splits", []))
        keeps = [pair for a, b in keeps for pair in _pairs([a, *[t for t in marks if a < t < b], b])]
    out, at = [], 0.0
    for a, b in keeps:
        src, _ = project.source_at(p, (a + b) / 2)
        out.append({"sid": src["id"], "path": src["path"], "has_audio": src.get("has_audio", True),
                    "a": a, "b": b, "src_in": a - src["offset"],
                    "f0": round(at * fps), "f1": round((at + b - a) * fps)})
        at += b - a
    return out


def _pairs(edges):
    return list(zip(edges, edges[1:]))


def require_files(segs):
    missing = sorted({Path(s["path"]).name for s in segs if not Path(s["path"]).exists()})
    if missing:
        raise ValueError("원본 영상을 찾을 수 없어요: " + ", ".join(missing))


def frame_parts(start, end, segs, fps):
    """원본 [start, end] 가운데 남는 부분을 편집본 프레임 조각 [(f0, f1)] 으로 바꿔요.
    원본에서도 이어지는 조각(분할 표시·소스 경계)은 하나로 합쳐서, 잘린 구간에서만 나뉘어요."""
    parts = []
    for s in segs:
        a, b = max(s["a"], start), min(s["b"], end)
        if b <= a:
            continue
        f0 = s["f0"] if a <= s["a"] else min(s["f1"], s["f0"] + round((a - s["a"]) * fps))
        f1 = s["f1"] if b >= s["b"] else min(s["f1"], s["f0"] + round((b - s["a"]) * fps))
        if f1 <= f0:
            continue
        if parts and parts[-1][1] == f0 and parts[-1][3] == a:
            parts[-1][1], parts[-1][3] = f1, b
        else:
            parts.append([f0, f1, a, b])
    return [(f0, f1) for f0, f1, _, _ in parts]


def position(sub, speakers):
    """자막 위치(%): 자막 pos → 화자 pos → 기본 위치 순서로 정해요."""
    pos = sub.get("pos") or (speakers.get(sub.get("speaker")) or {}).get("pos") or DEFAULT_POS
    return float(pos["x"]), float(pos["y"])


def edited_subtitles(p, segs=None):
    """편집본에 보이는 자막을 편집본 프레임(f0~f1, 초 start/end)과 화면 위치(x, y %)로 바꿔 시간순으로 돌려줘요.
    다 잘린 자막은 빠지고, 화자가 다른 자막끼리는 겹쳐도 그대로 둬요."""
    fps = p["canvas"]["fps"]
    segs = plan(p) if segs is None else segs
    speakers = {s["id"]: s for s in p.get("speakers", [])}
    out = []
    for sub in p.get("subtitles", []):
        text = "\n".join(line.strip() for line in sub["text"].split("\n") if line.strip())
        parts = frame_parts(sub["start"], sub["end"], segs, fps) if text else []
        if parts:
            x, y = position(sub, speakers)
            out.append({"id": sub["id"], "text": text, "f0": parts[0][0], "f1": parts[-1][1], "parts": parts,
                        "start": parts[0][0] / fps, "end": parts[-1][1] / fps, "x": x, "y": y})
    return sorted(out, key=lambda s: (s["f0"], s["f1"]))


def export_srt(p, path, segs=None):
    """편집본 시간 기준 SRT를 써요."""
    write_srt(edited_subtitles(p, segs), path)
    return Path(path)


# ---------- 자막 입히기 (이 Mac 의 ffmpeg 에는 libass 가 없어서 Pillow 로 그린 PNG를 겹쳐요) ----------

@functools.lru_cache(maxsize=8)
def font(size):
    return ImageFont.truetype(FONT, size, index=FONT_BOLD_INDEX)


def wrap(draw, text, font, max_w):
    """폭에 맞춰 긴 단어도 글자 단위로 줄을 나눠요."""
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            current = trial
            continue
        if current:
            lines.append(current)
            current = ""
        for char in word:
            if current and draw.textlength(current + char, font=font) > max_w:
                lines.append(current)
                current = ""
            current += char
    if current:
        lines.append(current)
    return lines


def draw_sub(text, x, y, w, h, path):
    """미리보기와 같은 모양(흰 굵은 글씨·검은 테두리, 화면 높이의 5.2%, 폭 88%에서 줄바꿈)으로 PNG를 그려요.
    (x, y)% 가 글 상자 가운데예요. 화면에 놓을 왼쪽 위 좌표를 돌려줘요."""
    size = max(8, round(h * 0.052))
    stroke = max(1, round(size * 0.07))
    f = font(size)
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    body = "\n".join(part for line in text.split("\n") for part in wrap(measure, line, f, w * 0.88))
    spacing = round(size * 0.2)
    left, top, right, bottom = measure.multiline_textbbox((0, 0), body, font=f, spacing=spacing,
                                                          stroke_width=stroke, align="center")
    image = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
    ImageDraw.Draw(image).multiline_text((-left, -top), body, font=f, fill="white", spacing=spacing, align="center",
                                         stroke_width=stroke, stroke_fill="black")
    image.save(path)
    return round(w * x / 100 - (right - left) / 2), round(h * y / 100 - (bottom - top) / 2)


# ---------- 인코딩 ----------

def encode_segment(seg, out, canvas, encoder="vt", overlays=(), audio="pcm"):
    """원본 구간 하나를 프레임 수와 오디오 길이에 맞춰 인코딩해요."""
    encode_batch([seg], [out], canvas, encoder, [overlays], audio)


def encode_batch(segs, outs, canvas, encoder="vt", overlays=None, audio="pcm"):
    r"""독립된 구간들을 한 FFmpeg 실행에서 각각 인코딩해요.

    $$T_i = \frac{f_{1,i}-f_{0,i}}{F}$$

    각 입력의 탐색·프레임 수·음성 페이드·자막 프레임은 구간 하나를 처리할 때와 같아요.
    F는 캔버스 프레임률이고 T_i는 해당 구간의 오디오 길이예요.
    """
    w, h, fps = even(canvas["w"]), even(canvas["h"]), canvas["fps"]
    overlays = overlays if overlays is not None else [()] * len(segs)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    if len(segs) > 1:
        cmd += ["-filter_complex_threads", "1"]
    graph, outputs, input_index = [], [], 0
    for i, (seg, out, layers) in enumerate(zip(segs, outs, overlays)):
        # 구간의 출력 프레임 수를 구해요: $$N_i=f_{1,i}-f_{0,i}$$
        n = seg["f1"] - seg["f0"]
        # 출력 프레임 수에 맞춰 오디오 길이를 정해요: $$T_i=N_i/F$$
        dur = n / fps
        base = input_index
        cmd += ["-ss", f"{max(seg['src_in'], 0):.6f}"]
        if len(segs) > 1:
            cmd += ["-threads", "2"]
        cmd += ["-i", str(seg["path"])]
        input_index += 1
        if seg["has_audio"]:
            sound = f"{base}:a:0"
        else:
            cmd += ["-f", "lavfi", "-i", f"anullsrc=r={RATE}:cl=stereo"]
            sound = f"{input_index}:a:0"
            input_index += 1
        first = input_index
        for png, *_ in layers:
            cmd += ["-i", str(png)]
            input_index += 1
        tag = f"s{i}_"
        graph.append(f"[{base}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                     f"setsar=1,fps={fps},tpad=stop_mode=clone:stop_duration=1,setpts=N/({fps}*TB)[{tag}v0]")
        for k, (_, x, y, n0, n1) in enumerate(layers):
            graph.append(f"[{tag}v{k}][{first + k}:v]overlay=x={x}:y={y}:enable='between(n,{n0},{n1 - 1})'[{tag}v{k + 1}]")
        graph.append(f"[{tag}v{len(layers)}]format=yuv420p[{tag}v]")
        graph.append(f"[{sound}]aresample={RATE},aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS,afade=t=in:d={FADE},"
                     f"apad,atrim=end_sample={round(dur * RATE)},afade=t=out:st={max(dur - FADE, 0):.6f}:d={FADE}[{tag}a]")
        outputs.append((tag, n, out))
    if encoder == "vt":
        # 출력 면적에 맞춰 비트레이트를 정해요: $$B=\max(4,\operatorname{round}(12wh/(1920\cdot1080)))$$
        bitrate = max(4, round(12 * w * h / (1920 * 1080)))
        video = ["-c:v", "h264_videotoolbox", "-b:v", f"{bitrate}M", "-profile:v", "high"]
    else:
        video = ["-c:v", "libx264", "-crf", "18", "-preset", "fast"]
    sound_codec = ["-c:a", "pcm_s16le"] if audio == "pcm" else ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    cmd += ["-filter_complex", ";".join(graph)]
    for tag, n, out in outputs:
        cmd += ["-map", f"[{tag}v]", "-map", f"[{tag}a]", "-frames:v", str(n), *video,
                "-pix_fmt", "yuv420p", *sound_codec, "-ar", str(RATE), "-video_track_timescale", "90000", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)


def encode_all(segs, outs, canvas, overlays=None, audio="pcm", on_progress=None, batch_size=1):
    """동시 입력 수를 제한해 구간들을 묶어 인코딩하고, 하드웨어 실패 시 전부 소프트웨어로 다시 해요."""
    overlays = overlays or [()] * len(segs)
    batch_size = max(1, min(int(batch_size), WORKERS))
    for encoder in ("vt", "x264"):
        # 하드웨어는 실행을 묶고, 소프트웨어 대체 경로는 기존 병렬 처리량을 유지해요.
        size = batch_size if encoder == "vt" else 1
        batches = [slice(i, i + size) for i in range(0, len(segs), size)]
        pool = ThreadPoolExecutor(WORKERS)
        futures = {}
        for batch in batches:
            ss, oo, layers = segs[batch], outs[batch], overlays[batch]
            if len(ss) == 1:
                future = pool.submit(encode_segment, ss[0], oo[0], canvas, encoder, layers[0], audio)
            else:
                future = pool.submit(encode_batch, ss, oo, canvas, encoder, layers, audio)
            futures[future] = len(ss)
        try:
            done = 0
            for future in as_completed(futures):
                future.result()
                done += futures[future]
                if on_progress:
                    on_progress(done / len(segs))
            return encoder
        except subprocess.CalledProcessError as exc:
            if encoder == "x264":
                raise ValueError("영상 인코딩에 실패했어요: " + exc.stderr.decode(errors="replace").strip()[-300:]) from exc
        finally:
            pool.shutdown(cancel_futures=True)


def render_mp4(p, out_dir, burn=True, on_progress=None):
    """편집본을 out_dir/final.mp4 (+ 자막이 있으면 final.srt) 로 만들어요. 진행률은 0..1 로 알려요.
    burn=True 면 자막을 화면에 입히고(겹치는 자막은 각자 위치에), False 면 켜고 끄는 자막 트랙으로 넣어요."""
    out_dir = Path(out_dir)
    progress = on_progress or (lambda f: None)
    canvas = p["canvas"]
    segs = [s for s in plan(p) if s["f1"] > s["f0"]]
    if not segs:
        raise ValueError("남은 영상이 없어요. 컷을 하나 이상 살려 주세요")
    require_files(segs)
    subs = edited_subtitles(p, segs)
    tmp = out_dir / "_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        srt = out_dir / "final.srt"
        if subs:
            write_srt(subs, srt)
        overlays = [[] for _ in segs]
        if burn:
            for i, sub in enumerate(subs):
                png = tmp / f"sub{i:05d}.png"
                x, y = draw_sub(sub["text"], sub["x"], sub["y"], even(canvas["w"]), even(canvas["h"]), png)
                for seg, layer in zip(segs, overlays):
                    n0, n1 = max(sub["f0"], seg["f0"]) - seg["f0"], min(sub["f1"], seg["f1"]) - seg["f0"]
                    if n1 > n0:
                        layer.append((png, x, y, n0, n1))
        parts = [tmp / f"p{i:05d}.mov" for i in range(len(segs))]
        encode_all(segs, parts, canvas, overlays, "pcm", lambda f: progress(0.95 * f), batch_size=MP4_BATCH)
        listing = tmp / "parts.txt"
        listing.write_text("".join("file '" + str(x).replace("'", "'\\''") + "'\n" for x in parts), encoding="utf-8")
        final = out_dir / "final.mp4"
        soft = bool(subs) and not burn
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing)]
        cmd += ["-i", str(srt), "-map", "0:v", "-map", "0:a", "-map", "1:s", "-c:s", "mov_text",
                "-metadata:s:s:0", "language=kor"] if soft else ["-map", "0:v", "-map", "0:a"]
        try:
            subprocess.run(cmd + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(final)],
                           check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            raise ValueError("영상을 이어 붙이지 못했어요: " + exc.stderr.decode(errors="replace").strip()[-300:]) from exc
        progress(1.0)
        return final
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

"""ffmpeg 로 만드는 미리보기·음성·파형·썸네일·이어 붙이기 결과를 확인해요."""
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from PIL import Image, ImageStat

from editvdo import media

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "test_vlog.mp4"
CANVAS = {"w": 1280, "h": 720, "fps": 30}


def ff(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *map(str, args)], check=True)


def info(path):
    """(format, {codec_type: stream})"""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                         check=True, capture_output=True, text=True).stdout
    d = json.loads(out)
    return d["format"], {s["codec_type"]: s for s in reversed(d["streams"])}


def keyframes(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
                          "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return [float(x.strip(",")) for x in out.split()]


def gray_frame(path, t, w, h):
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"], check=True, capture_output=True).stdout
    return Image.frombytes("L", (w, h), raw)


class MediaFiles(unittest.TestCase):
    """샘플과 합성 클립(세로·무음, 회전·고fps)으로 media 함수들을 실제로 돌려 봐요."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="editvdo-media-"))
        cls.portrait = cls.tmp / "portrait.mp4"  # 720x1280, 29.97fps, 5.005초, 소리 없음, 촬영 시각 있음
        ff("-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30000/1001", "-t", "5.005", "-c:v", "libx264",
           "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-metadata", "creation_time=2026-10-16T03:36:49Z",
           cls.portrait)
        cls.rot = cls.tmp / "rot.mov"  # 1920x1080 에 회전 90° 표시, 59.94fps, 소리 44.1k 모노 3.2초
        ff("-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=60000/1001", "-f", "lavfi",
           "-i", "sine=frequency=440:sample_rate=44100", "-t", "3.2", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", cls.tmp / "rot0.mov")
        ff("-display_rotation:v:0", "90", "-i", cls.tmp / "rot0.mov", "-c", "copy", cls.rot)
        cls.progress = []
        cls.p1, cls.p2, cls.p3 = (cls.tmp / f"p{i}.mp4" for i in (1, 2, 3))
        media.make_proxy(SAMPLE, cls.p1, CANVAS, cls.progress.append)
        media.make_proxy(cls.portrait, cls.p2, CANVAS)
        media.make_proxy(cls.rot, cls.p3, CANVAS)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_probe(self):
        """길이·크기·fps·소리·촬영 시각을 읽고, 회전 영상은 가로세로를 바꿔요."""
        p = media.probe(SAMPLE)
        self.assertEqual((p["duration"], p["w"], p["h"], p["fps"], p["has_audio"], p["rotation"]),
                         (34.033, 1280, 720, 30.0, True, 0))
        self.assertRegex(p["created"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        r = media.probe(self.rot)
        self.assertEqual((r["w"], r["h"], r["fps"], r["has_audio"]), (1080, 1920, 59.94, True))
        self.assertIn(r["rotation"], (90, 270))
        q = media.probe(self.portrait)
        self.assertEqual((q["duration"], q["w"], q["h"], q["fps"], q["has_audio"]), (5.005, 720, 1280, 29.97, False))
        local = datetime(2026, 10, 16, 3, 36, 49, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
        self.assertEqual(q["created"], local)

    def test_probe_errors(self):
        """영상 스트림이 없거나 읽을 수 없는 파일은 ValueError 예요."""
        audio = self.tmp / "only.m4a"
        ff("-f", "lavfi", "-i", "sine=frequency=300", "-t", "1", "-c:a", "aac", audio)
        with self.assertRaisesRegex(ValueError, "영상 스트림이 없어요"):
            media.probe(audio)
        text = self.tmp / "note.txt"
        text.write_text("not a video")
        with self.assertRaises(ValueError):
            media.probe(text)

    def test_proxy_format_and_length(self):
        """미리보기는 1280x720 30fps H.264(B프레임 없음) + AAC 48k 스테레오, 소리 길이 = 원본 길이."""
        fmt, st = info(self.p1)
        v, a = st["video"], st["audio"]
        self.assertEqual((v["codec_name"], v["width"], v["height"], v["r_frame_rate"], v["has_b_frames"], v["pix_fmt"]),
                         ("h264", 1280, 720, "30/1", 0, "yuv420p"))
        self.assertEqual((a["codec_name"], a["sample_rate"], a["channels"]), ("aac", "48000", 2))
        self.assertAlmostEqual(float(a["duration"]), 34.033, delta=0.0005)
        self.assertAlmostEqual(float(fmt["duration"]), 34.033, delta=1 / 30)
        self.assertTrue(self.progress and all(0 <= x <= 1 for x in self.progress))
        self.assertEqual(self.progress, sorted(self.progress))
        self.assertEqual(self.progress[-1], 1.0)
        self.assertFalse(list(self.tmp.glob("*.part*")))

    def test_proxy_keyframes_every_15_frames(self):
        """15프레임(0.5초)마다 키프레임이라 어디든 빨리 찾아가요."""
        for proxy in (self.p1, self.p2, self.p3):
            keys = keyframes(proxy)
            frames = int(info(proxy)[1]["video"]["nb_frames"])
            self.assertEqual(len(keys), math.ceil(frames / 15), proxy.name)
            self.assertTrue(all(abs(b - a - 0.5) < 0.001 for a, b in zip(keys, keys[1:])), proxy.name)

    def test_proxy_fit_pad_and_silent_audio(self):
        """세로·회전 영상은 캔버스 상자 가운데에 맞추고 양옆을 검게, 소리가 없으면 무음 트랙을 넣어요."""
        for proxy, dur in ((self.p2, 5.005), (self.p3, 3.2)):
            _, st = info(proxy)
            self.assertEqual((st["video"]["width"], st["video"]["height"]), (1280, 720))
            self.assertAlmostEqual(float(st["audio"]["duration"]), dur, delta=0.0005)
            img = gray_frame(proxy, 1, 1280, 720)
            side = img.crop((0, 0, 300, 720)).getextrema()[1]
            middle = ImageStat.Stat(img.crop((560, 200, 720, 520))).mean[0]
            self.assertLess(side, 30, proxy.name)
            self.assertGreater(middle, 40, proxy.name)
        tall = self.tmp / "tall.mp4"
        media.make_proxy(self.rot, tall, {"w": 1080, "h": 1920, "fps": 60})
        _, st = info(tall)
        self.assertEqual((st["video"]["width"], st["video"]["height"], st["video"]["r_frame_rate"]), (720, 1280, "30/1"))

    def test_proxy_software_fallback(self):
        """하드웨어 디코딩·인코딩이 실패하면 소프트웨어(libx264)로 다시 만들어요. 키프레임 간격은 같아요."""
        real, calls = media._ffmpeg_progress, []

        def flaky(cmd, duration, on_progress):
            calls.append([str(x) for x in cmd])
            if len(calls) == 1:
                raise RuntimeError("ffmpeg 실패: videotoolbox")
            return real(cmd, duration, on_progress)

        dst, seen = self.tmp / "sw.mp4", []
        with mock.patch.object(media, "_ffmpeg_progress", flaky):
            media.make_proxy(self.portrait, dst, CANVAS, seen.append)
        self.assertEqual(len(calls), 2)
        self.assertIn("-hwaccel", calls[0])
        self.assertNotIn("-hwaccel", calls[1])
        self.assertIn("libx264", calls[1])
        self.assertEqual(seen[-1], 1.0)
        keys = keyframes(dst)
        self.assertTrue(len(keys) > 3 and all(abs(b - a - 0.5) < 0.001 for a, b in zip(keys, keys[1:])))
        _, st = info(dst)
        self.assertEqual((st["video"]["width"], st["video"]["height"], st["video"]["has_b_frames"]), (1280, 720, 0))
        self.assertAlmostEqual(float(st["audio"]["duration"]), 5.005, delta=0.0005)

    def test_extract_audio(self):
        """16 kHz 모노 s16 wav. 소리가 없는 영상은 같은 길이의 무음이에요."""
        wav = self.tmp / "a1.wav"
        media.extract_audio(SAMPLE, wav)
        with wave.open(str(wav)) as f:
            self.assertEqual((f.getframerate(), f.getnchannels(), f.getsampwidth()), (16000, 1, 2))
            self.assertAlmostEqual(f.getnframes() / 16000, 34.03, delta=0.05)
        silent = self.tmp / "a2.wav"
        media.extract_audio(self.portrait, silent)
        with wave.open(str(silent)) as f:
            self.assertAlmostEqual(f.getnframes() / 16000, 5.005, delta=0.01)
            self.assertFalse(any(f.readframes(f.getnframes())))

    def test_extract_audio_clean_runs_demucs(self):
        """clean=True 면 demucs 로 목소리만 분리해서 16 kHz 모노로 바꾸고 작업 폴더를 지워요 (demucs 는 흉내만 내요)."""
        real, seen = subprocess.run, []

        def fake(cmd, *a, **kw):
            if "demucs" in cmd:
                seen.append(cmd)
                out = Path(cmd[cmd.index("-o") + 1]) / "htdemucs" / "mix"
                out.mkdir(parents=True)
                shutil.copy(cmd[-1], out / "vocals.wav")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return real(cmd, *a, **kw)

        dst = self.tmp / "clean.wav"
        with mock.patch.object(media.subprocess, "run", fake):
            media.extract_audio(SAMPLE, dst, clean=True)
        self.assertEqual(seen[0][1:8], ["-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs", "-d"])
        with wave.open(str(dst)) as f:
            self.assertEqual((f.getframerate(), f.getnchannels()), (16000, 1))
            self.assertAlmostEqual(f.getnframes() / 16000, 34.03, delta=0.05)
        self.assertFalse(list(self.tmp.glob("demucs-*")))

    def test_peaks(self):
        """초당 50개, 0~255. json 파일과 돌려준 값이 같아요."""
        wav, dst = self.tmp / "pk.wav", self.tmp / "peaks.json"
        media.extract_audio(SAMPLE, wav)
        data = media.peaks(wav, dst)
        with wave.open(str(wav)) as f:
            n = f.getnframes()
        self.assertEqual(data["per_sec"], 50)
        self.assertEqual(len(data["data"]), math.ceil(n / 320))
        self.assertTrue(all(isinstance(x, int) and 0 <= x <= 255 for x in data["data"]))
        self.assertGreater(max(data["data"]), 100)
        self.assertEqual(json.loads(dst.read_text()), data)

    def test_thumbs(self):
        """10칸 한 장 + {interval,w,h,cols,count}. 키프레임만 풀어도 미리보기·원본 모두 같아요."""
        for src in (self.p1, SAMPLE):
            jpg, js = self.tmp / f"{src.stem}.jpg", self.tmp / f"{src.stem}.json"
            meta = media.thumbs(src, jpg, js, CANVAS)
            self.assertEqual({k: meta[k] for k in ("interval", "w", "h", "cols")},
                             {"interval": 1.0, "w": 160, "h": 90, "cols": 10})
            self.assertIn(meta["count"], (34, 35))
            self.assertEqual(json.loads(js.read_text()), meta)
            with Image.open(jpg) as im:
                self.assertEqual(im.size, (1600, 90 * math.ceil(meta["count"] / 10)))
        long_clip = self.tmp / "long.mp4"  # 450초 → 간격 1.125초, 400칸 안팎
        ff("-f", "lavfi", "-i", "testsrc=size=128x72:rate=10", "-t", "450", "-c:v", "libx264", "-preset", "ultrafast",
           "-g", "5", "-pix_fmt", "yuv420p", long_clip)
        meta = media.thumbs(long_clip, self.tmp / "long.jpg", self.tmp / "long.json", {"w": 1080, "h": 1920, "fps": 30})
        self.assertEqual((meta["interval"], meta["w"], meta["h"]), (1.125, 160, 284))
        self.assertTrue(398 <= meta["count"] <= 401, meta["count"])

    def test_concat_preview_keeps_original_axis(self):
        """이어 붙인 미리보기에서 각 클립은 원본 시간축의 자기 자리(앞 클립 길이 합)에서 시작해요."""
        dst = self.tmp / "preview.mp4"
        media.concat_preview([self.p2, self.p1, self.p3], dst)
        fmt, st = info(dst)
        self.assertAlmostEqual(float(st["audio"]["duration"]), 5.005 + 34.033 + 3.2, delta=0.002)
        self.assertAlmostEqual(float(fmt["duration"]), 42.238, delta=1 / 30)
        keys = keyframes(dst)
        for start in (5.005, 39.038):
            self.assertTrue(any(abs(k - start) < 0.002 for k in keys), (start, keys[8:12]))
        check = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(dst), "-f", "null", "-"],
                               capture_output=True, text=True)
        self.assertEqual((check.returncode, check.stderr), (0, ""))
        self.assertFalse(list(self.tmp.glob("preview.part*")))

    def test_concat_single_is_hardlink_and_mixed_formats_are_reencoded(self):
        """한 개면 하드링크로 복사 없이. 형식이 다른 파일(소프트웨어 인코딩·v1 미리보기)이 섞이면
        같은 설정으로 다시 만들어 이어 붙이고, 각 클립은 그대로 원본 시간축의 자기 자리에서 시작해요."""
        one = self.tmp / "one.mp4"
        media.concat_preview([self.p1], one)
        self.assertEqual(os.stat(one).st_ino, os.stat(self.p1).st_ino)
        old = self.tmp / "v1style.mp4"  # v1 미리보기처럼 44.1k 모노 소리
        ff("-i", SAMPLE, "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-g", "15", "-r", "30",
           "-c:a", "aac", "-ar", "44100", "-ac", "1", old)
        sw, real = self.tmp / "sw-rot.mp4", media._ffmpeg_progress  # 하드웨어 인코딩이 안 돼서 libx264 로 만든 미리보기

        def no_hw(cmd, duration, on_progress):
            if "h264_videotoolbox" in map(str, cmd):
                raise RuntimeError("ffmpeg 실패: videotoolbox")
            return real(cmd, duration, on_progress)
        with mock.patch.object(media, "_ffmpeg_progress", no_hw):
            media.make_proxy(self.rot, sw, CANVAS)
        d_old = float(info(old)[1]["audio"]["duration"])
        dst = self.tmp / "mixed.mp4"
        media.concat_preview([self.p2, old, sw], dst)
        fmt, st = info(dst)
        v, a = st["video"], st["audio"]
        self.assertEqual((v["codec_name"], v["width"], v["height"], v["has_b_frames"], a["sample_rate"], a["channels"]),
                         ("h264", 1280, 720, 0, "48000", 2))
        self.assertAlmostEqual(float(a["duration"]), 5.005 + d_old + 3.2, delta=0.002)
        keys = keyframes(dst)
        for start in (5.005, 5.005 + d_old):
            self.assertTrue(any(abs(k - start) < 0.002 for k in keys), (start, keys))
        check = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(dst), "-f", "null", "-"],
                               capture_output=True, text=True)
        self.assertEqual((check.returncode, check.stderr), (0, ""))
        self.assertFalse([x for x in self.tmp.iterdir() if re.search(r"\.part", x.name)])


if __name__ == "__main__":
    unittest.main()

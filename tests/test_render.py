"""MP4·SRT 출력: 여러 원본에서 프레임 단위로 자른 길이와 겹치는 자막(각자 위치)을 확인해요."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from editvdo import render

REPO = Path(__file__).resolve().parent.parent
SAMPLE = REPO / "samples" / "test_vlog.mp4"
D = 34.033333
W, H = 1280, 720


def project_two_sources():
    """test_vlog.mp4 두 번(s1, s2): 남는 구간 [3, 4.5](s1) · [D+20, D+22](s2) = 3.5초(105프레임).
    자막 둘은 1.0~2.5초에 겹치고, 하나는 자기 pos(왼쪽 아래), 하나는 화자 pos(오른쪽 위)를 써요."""
    src = {"path": str(SAMPLE), "duration": D, "w": W, "h": H, "fps": 30.0, "has_audio": True, "status": "ready"}
    return {"format": 2, "name": "_t_render", "title": "렌더 테스트", "canvas": {"w": W, "h": H, "fps": 30},
            "sources": [dict(src, id="s1", name="a.mp4", offset=0.0), dict(src, id="s2", name="b.mp4", offset=D)],
            "cuts": [{"id": "c1", "start": 0.0, "end": 3.0, "type": "user", "enabled": True},
                     {"id": "c2", "start": 4.5, "end": D + 20.0, "type": "user", "enabled": True},
                     {"id": "c3", "start": D + 22.0, "end": 2 * D, "type": "user", "enabled": True}],
            "subtitles": [{"id": "t1", "start": 3.5, "end": D + 21.0, "text": "왼쪽 아래 자막", "pos": {"x": 25, "y": 85}},
                          {"id": "t2", "start": 4.0, "end": D + 21.5, "text": "오른쪽 위", "pos": None, "speaker": "B"},
                          {"id": "t3", "start": 10.0, "end": 12.0, "text": "다 잘림", "pos": None}],
            "speakers": [{"id": "B", "name": "화자 2", "color": "#F5A524", "pos": {"x": 75, "y": 15}}],
            "splits": [], "comments": [], "notes": "",
            "settings": {"cut": {"min_silence": 0.5, "pad_before": 0.15, "pad_after": 0.25, "fillers": True, "long_silence": 2.0},
                         "subs": {"lead": 0.0, "hold": 0.3, "min": 0.8, "bridge": 0.3, "max_chars": 18},
                         "asr": {"model": "mlx-community/whisper-large-v3-turbo", "language": "ko", "prompt": "", "clean": False}}}


def probe(path, entries, stream="v:0"):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", stream, "-show_entries", entries,
                          "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, check=True).stdout
    return out.split()


def frame(path, index=None, at=None):
    """회색조 한 프레임 (index: 출력 프레임 번호, at: 원본 시각 초)."""
    pick = ["-ss", f"{at:.4f}", "-i", str(path), "-frames:v", "1"] if at is not None else \
        ["-i", str(path), "-vf", f"select=eq(n\\,{index})", "-frames:v", "1"]
    raw = subprocess.run(["ffmpeg", "-v", "error", *pick, "-s", f"{W}x{H}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(H, W).astype(float)


def region(img, x, y, half=0.08):
    cx, cy = int(W * x / 100), int(H * y / 100)
    return img[cy - int(H * half): cy + int(H * half), cx - int(W * half): cx + int(W * half)]


class Pure(unittest.TestCase):
    """ffmpeg 없이 계산만 확인해요."""
    SEGS = [{"a": 3.0, "b": 4.5, "f0": 0, "f1": 45}, {"a": D + 20, "b": D + 22, "f0": 45, "f1": 105}]

    def test_edited_subtitles_merge_and_positions(self):
        subs = render.edited_subtitles(project_two_sources(), self.SEGS)
        self.assertEqual([(s["id"], s["f0"], s["f1"], s["x"], s["y"]) for s in subs],
                         [("t1", 15, 75, 25.0, 85.0), ("t2", 30, 90, 75.0, 15.0)])
        self.assertEqual((subs[0]["start"], subs[0]["end"]), (0.5, 2.5))
        self.assertEqual(subs[0]["parts"], [(15, 45), (45, 75)])

    def test_default_position_and_text_cleanup(self):
        p = project_two_sources()
        p["subtitles"] = [{"id": "x", "start": 3.2, "end": 3.4, "text": "  첫 줄 \n\n 둘째 줄 ", "pos": None, "speaker": "없음"},
                          {"id": "y", "start": 3.5, "end": 3.6, "text": "   ", "pos": None}]
        subs = render.edited_subtitles(p, self.SEGS)
        self.assertEqual([(s["text"], s["x"], s["y"]) for s in subs], [("첫 줄\n둘째 줄", 50.0, 88.0)])

    def test_srt_on_edited_axis(self):
        out = Path(tempfile.mkdtemp(prefix="editvdo-srt-"))
        self.addCleanup(shutil.rmtree, out, True)
        path = render.export_srt(project_two_sources(), out / "a.srt", self.SEGS)
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "1\n00:00:00,500 --> 00:00:02,500\n왼쪽 아래 자막\n\n2\n00:00:01,000 --> 00:00:03,000\n오른쪽 위\n\n")
        self.assertEqual(render.srt_time(3725.0004), "01:02:05,000")

    def test_hardware_encoder_failure_redoes_every_segment_with_x264(self):
        """videotoolbox 가 한 구간이라도 실패하면 이어 붙일 수 있게 모든 구간을 libx264 로 다시 해요."""
        calls = []

        def flaky(seg, out, canvas, encoder, overlays, audio):
            calls.append((seg["f0"], encoder))
            if encoder == "vt" and seg["f0"] == 45:
                raise subprocess.CalledProcessError(1, "ffmpeg", stderr=b"vt busy")
        with patch.object(render, "encode_segment", flaky):
            self.assertEqual(render.encode_all(self.SEGS, ["a", "b"], {"w": W, "h": H, "fps": 30}), "x264")
        self.assertEqual(sorted(c for c in calls if c[1] == "x264"), [(0, "x264"), (45, "x264")])

        def broken(*args):
            raise subprocess.CalledProcessError(1, "ffmpeg", stderr=b"No such filter: 'x'")
        with patch.object(render, "encode_segment", broken):
            with self.assertRaisesRegex(ValueError, "인코딩에 실패했어요: No such filter"):
                render.encode_all(self.SEGS, ["a", "b"], {"w": W, "h": H, "fps": 30})

    def test_plan_frames_from_project(self):
        segs = render.plan(project_two_sources())
        self.assertEqual([(s["sid"], round(s["src_in"], 6), s["f0"], s["f1"]) for s in segs],
                         [("s1", 3.0, 0, 45), ("s2", 20.0, 45, 105)])


class Mp4(unittest.TestCase):
    """실제 인코딩: 길이(±1프레임)·내용·자막 입히기를 확인해요."""

    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp(prefix="editvdo-render-"))
        p = project_two_sources()
        cls.burned = render.render_mp4(p, cls.out / "burn", burn=True)
        cls.plain = render.render_mp4(p, cls.out / "plain", burn=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, True)

    def test_length_is_frame_exact(self):
        for path in (self.burned, self.plain):
            self.assertEqual(probe(path, "stream=nb_read_packets"), ["105"])
            self.assertAlmostEqual(float(probe(path, "format=duration")[0]), 3.5, delta=1 / 30)
            self.assertAlmostEqual(float(probe(path, "stream=duration", "a:0")[0]), 3.5, delta=0.03)
        self.assertEqual(probe(self.plain, "stream=codec_name", "s:0"), ["mov_text"])
        self.assertEqual(probe(self.burned, "stream=codec_name", "s"), [])
        self.assertTrue((self.out / "burn" / "final.srt").exists())
        self.assertFalse((self.out / "burn" / "_tmp").exists())

    def test_seam_frames_are_exact_source_frames(self):
        """예제 영상 왼쪽 위의 프레임 번호로 비교해요: 출력 프레임이 ±1 이웃이 아니라 정확히 그 원본 프레임인지."""
        counter = lambda img: img[0:40, 0:110]
        for index, want in ((0, 90), (44, 134), (45, 600), (60, 615), (104, 659)):   # s1 3.0초 · s2 20.0초부터
            got = counter(frame(self.plain, index=index))
            diffs = {n: np.abs(got - counter(frame(SAMPLE, at=(n - 0.5) / 30))).mean() for n in (want - 1, want, want + 1)}
            self.assertEqual(min(diffs, key=diffs.get), want, (index, diffs))

    def test_overlapping_subtitles_burned_at_their_own_positions(self):
        both = frame(self.burned, index=50) - frame(self.plain, index=50)     # 두 자막 다 보이는 때
        self.assertGreater(np.abs(region(both, 25, 85)).mean(), 15)
        self.assertGreater(np.abs(region(both, 75, 15)).mean(), 15)
        self.assertLess(np.abs(region(both, 50, 50)).mean(), 3)
        first_only = frame(self.burned, index=20) - frame(self.plain, index=20)
        self.assertGreater(np.abs(region(first_only, 25, 85)).mean(), 15)
        self.assertLess(np.abs(region(first_only, 75, 15)).mean(), 3)
        none = frame(self.burned, index=100) - frame(self.plain, index=100)   # 자막 끝난 뒤
        self.assertLess(np.abs(none).mean(), 2)


class Mp4Edges(unittest.TestCase):
    """소리 없는 다른 크기·fps 원본, 다 잘린 편집, 없는 파일."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="editvdo-render-"))
        self.addCleanup(shutil.rmtree, self.out, True)

    def test_silent_portrait_source_is_letterboxed_with_silence(self):
        clip = self.out / "silent.mp4"   # 세로 360x640 25fps → 1280x720 30fps 안에 405px 폭(홀수)으로 맞춰져요
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=25", "-t", "2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
        p = project_two_sources()
        p["sources"] = [{"id": "s1", "path": str(clip), "name": "silent.mp4", "duration": 2.0, "offset": 0.0,
                         "w": 360, "h": 640, "fps": 25.0, "has_audio": False}]
        p["cuts"], p["subtitles"] = [{"id": "c", "start": 0.5, "end": 1.0, "type": "user", "enabled": True}], []
        final = render.render_mp4(p, self.out / "r", burn=True)
        self.assertEqual(probe(final, "stream=nb_read_packets"), ["45"])
        self.assertEqual(probe(final, "stream=width,height"), ["1280", "720"])
        self.assertEqual(probe(final, "stream=codec_name", "a:0"), ["aac"])
        self.assertFalse((self.out / "r" / "final.srt").exists())

    def test_everything_cut_or_missing_file(self):
        p = project_two_sources()
        p["cuts"] = [{"id": "all", "start": 0.0, "end": 2 * D, "type": "user", "enabled": True}]
        with self.assertRaisesRegex(ValueError, "남은 영상이 없어요"):
            render.render_mp4(p, self.out / "x")
        p = project_two_sources()
        p["sources"][1]["path"] = str(self.out / "없는 파일.mp4")
        with self.assertRaisesRegex(ValueError, "없는 파일.mp4"):
            render.render_mp4(p, self.out / "y")


if __name__ == "__main__":
    unittest.main()

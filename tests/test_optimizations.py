"""파형 메모리와 자막 재계산을 확인하고, 묶음 인코딩의 영상·오디오가 기존 방식과 같은지 검사해요."""
import copy
import json
import subprocess
import tempfile
import tracemalloc
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from editvdo import analyze, media, project, render, subs

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "test_vlog.mp4"


class Peaks(unittest.TestCase):
    """청크 경계와 마지막 구간에서도 기존 파형 값을 유지하는지 확인해요."""

    def test_values_match_whole_audio_for_mono_and_stereo(self):
        r"""채널·표본률·청크 경계가 달라도 기존 최대 음량 눈금과 같은지 확인해요.

        $$p_k=\operatorname{round}(255\sqrt{M_k/32768})$$
        """
        rng = np.random.default_rng(41)
        with tempfile.TemporaryDirectory() as tmp:
            for channels, rate in ((1, 16000), (2, 11025), (2, 48000)):
                with self.subTest(channels=channels, rate=rate):
                    values = rng.integers(-32768, 32768, size=(200123, channels), dtype=np.int16)
                    values[65535:65538] = -32768
                    wav, out = Path(tmp) / "audio.wav", Path(tmp) / "peaks.json"
                    with wave.open(str(wav), "wb") as f:
                        f.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
                        f.writeframes(values.astype("<i2").tobytes())
                    # 각 프레임의 최대 절댓값을 구해요: $$m_i=\max_c|a_{i,c}|$$
                    amplitude = np.abs(values.astype(np.int32)).max(axis=1)
                    win = max(1, rate // 50)
                    # 구간별 최대 음량을 구해요: $$M_k=\max_{i\in W_k}m_i$$
                    maxima = np.array([amplitude[i:i + win].max() for i in range(0, len(values), win)])
                    # 기존 제곱근 눈금의 정수 값을 구해요: $$p_k=\operatorname{round}(255\sqrt{M_k/32768})$$
                    expected = np.round(np.sqrt(maxima / 32768) * 255).astype(int).tolist()
                    got = media.peaks(wav, out)
                    self.assertEqual(got, {"per_sec": 50, "data": expected})
                    self.assertEqual(json.loads(out.read_text()), got)

    def test_long_audio_has_bounded_working_memory(self):
        """긴 오디오에서도 전체 PCM 배열을 메모리에 올리지 않는지 확인해요."""
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "long.wav"
            with wave.open(str(wav), "wb") as f:
                f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                for _ in range(1200):
                    f.writeframesraw(bytes(32000))
            tracemalloc.start()
            try:
                result = media.peaks(wav, Path(tmp) / "peaks.json")
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(result["data"], [0] * 60000)
            self.assertLess(peak, 16 * 1024 * 1024)


class Timing(unittest.TestCase):
    """표시 속성 편집은 시간을 보존하고 시간 관련 편집은 재계산하는지 확인해요."""

    def setUp(self):
        """컷과 서로 다른 화자의 잠긴 자막을 포함하는 프로젝트를 준비해요."""
        self.p = project._fill({"sources": [{"id": "s1", "duration": 30, "path": str(SAMPLE)}],
                                "cuts": [{"id": "c1", "start": 6.0, "end": 8.0, "enabled": True}],
                                "speakers": [{"id": "A"}, {"id": "B"}],
                                "subtitles": [{"id": "t1", "start": 1.0, "end": 3.0, "speech": [1.0, 2.6], "speaker": "A"},
                                              {"id": "t2", "start": 4.0, "end": 5.8, "speech": [4.0, 5.4], "speaker": "A"},
                                              {"id": "t3", "start": 4.2, "end": 5.5, "speech": [4.2, 5.1], "speaker": "B", "locked": True}]}, "test")
        subs.retime(self.p)

    def test_visual_edits_keep_every_subtitle_time(self):
        """글·위치·확인 표시·화자 꾸미기를 바꿔도 전체 타이밍을 다시 계산하지 않아요."""
        for coll, item in (("subtitles", {"id": "t1", "text": "바꾼 글"}),
                           ("subtitles", {"id": "t1", "pos": {"x": 20, "y": 80}, "check": True}),
                           ("speakers", {"id": "A", "name": "이름", "color": "#FFFFFF", "pos": {"x": 50, "y": 70}}),
                           ("cuts", {"id": "c1", "reviewed": True, "reason": "확인했어요"})):
            with self.subTest(coll=coll, item=item):
                p = copy.deepcopy(self.p)
                before = [(s["start"], s["end"]) for s in p["subtitles"]]
                with patch.object(subs, "retime", wraps=subs.retime) as retime:
                    project.apply_ops(p, [{"op": "put", "coll": coll, "item": item}], "ui")
                retime.assert_not_called()
                self.assertEqual([(s["start"], s["end"]) for s in p["subtitles"]], before)
                self.assertEqual(subs.retime(p), [])

    def test_timing_edits_still_retime_and_honor_locks(self):
        """컷·발화·잠금·화자·자막 추가·삭제에서는 기존 타이밍 규칙을 적용해요."""
        operations = [{"op": "put", "coll": "cuts", "item": {"id": "c1", "enabled": False}},
                      {"op": "put", "coll": "subtitles", "item": {"id": "t1", "speech": [1.2, 2.8]}},
                      {"op": "put", "coll": "subtitles", "item": {"id": "t1", "start": 1.5}},
                      {"op": "put", "coll": "subtitles", "item": {"id": "t3", "locked": False}},
                      {"op": "put", "coll": "subtitles", "item": {"id": "t2", "speaker": "B"}},
                      {"op": "put", "coll": "subtitles", "item": {"id": "t4", "start": 9.0, "end": 10.0}},
                      {"op": "del", "coll": "subtitles", "id": "t2"},
                      {"op": "del", "coll": "speakers", "id": "A"},
                      {"op": "set", "key": "settings.subs", "value": {"hold": 0.5}}]
        for op in operations:
            with self.subTest(op=op):
                p = copy.deepcopy(self.p)
                with patch.object(subs, "retime", wraps=subs.retime) as retime:
                    project.apply_ops(p, [op], "ui")
                retime.assert_called_once()
                self.assertEqual(subs.retime(p), [])
                locked = next(s for s in p["subtitles"] if s["id"] == "t3")
                if locked["locked"]:
                    self.assertEqual((locked["start"], locked["end"]), (4.2, 5.5))


class BatchEncoding(unittest.TestCase):
    """실제 FFmpeg로 묶음 처리 전후의 프레임과 오디오 표본을 비교해요."""

    def test_frames_and_pcm_match_separate_encoding(self):
        """짧은 컷·원본 끝·무음·겹친 자막·서로 다른 출력 fps에서도 프레임과 PCM을 보존해요."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            png = root / "sub.png"
            render.draw_sub("싱크", 50, 80, 320, 180, png)
            for fps in (30, 29.97):
                with self.subTest(fps=fps):
                    canvas = {"w": 320, "h": 180, "fps": fps}
                    segs = [{"path": str(SAMPLE), "src_in": at, "has_audio": sound, "f0": 0, "f1": count}
                            for at, sound, count in ((0.013, True, 3), (3.167, False, 11), (33.98, True, 5))]
                    layers = [[(png, 80, 100, 0, 2)], [(png, 0, 0, 1, 4), (png, 80, 100, 2, 8)], []]
                    single = [root / f"single-{fps}-{i}.mov" for i in range(3)]
                    batch = [root / f"batch-{fps}-{i}.mov" for i in range(3)]
                    for seg, out, overlay in zip(segs, single, layers):
                        render.encode_segment(seg, out, canvas, "x264", overlay)
                    render.encode_batch(segs, batch, canvas, "x264", layers)
                    for old, new in zip(single, batch):
                        for stream, args in (("video", ["-map", "0:v:0", "-f", "framemd5"]),
                                             ("audio", ["-map", "0:a:0", "-c:a", "pcm_s16le", "-f", "s16le"])):
                            with self.subTest(fps=fps, file=old.name, stream=stream):
                                decoded = [subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), *args, "-"],
                                                          check=True, capture_output=True).stdout for path in (old, new)]
                                self.assertEqual(decoded[0], decoded[1])

    def test_batch_failure_reencodes_every_segment(self):
        """묶음 하나가 하드웨어 인코딩에 실패하면 모든 구간을 같은 소프트웨어 인코더로 다시 만들어요."""
        segs = [{"f0": i} for i in range(7)]
        calls, progress = [], []

        def batch(items, outs, canvas, encoder, overlays, audio):
            """호출된 구간을 기록하고 가운데 묶음의 하드웨어 실패를 재현해요."""
            calls.extend((s["f0"], encoder) for s in items)
            if encoder == "vt" and items[0]["f0"] == 3:
                raise subprocess.CalledProcessError(1, "ffmpeg", stderr=b"vt busy")

        def single(seg, out, canvas, encoder, overlays, audio):
            """마지막 단독 구간도 같은 기록에 넣어요."""
            batch([seg], [out], canvas, encoder, [overlays], audio)

        with patch.object(render, "encode_batch", batch), patch.object(render, "encode_segment", single):
            encoder = render.encode_all(segs, [str(i) for i in range(7)], {}, on_progress=progress.append, batch_size=3)
        self.assertEqual(encoder, "x264")
        self.assertEqual(sorted(i for i, enc in calls if enc == "x264"), list(range(7)))
        self.assertEqual(progress[-1], 1)


class ThumbnailInputs(unittest.TestCase):
    """원본 직접 재생과 프록시 재생에서 알맞은 썸네일 입력을 선택하는지 확인해요."""

    def test_uses_fresh_proxy_only_for_proxy_sources(self):
        """프록시를 만든 경우에만 재사용하고 원본 직접 재생에서는 오래된 프록시를 사용하지 않아요."""
        with tempfile.TemporaryDirectory() as tmp, patch.object(project, "PROJECTS", Path(tmp)):
            for mode in ("original", "proxy"):
                with self.subTest(mode=mode):
                    name = project.create("썸네일 입력", [SAMPLE], name=mode)
                    project.transact(name, lambda p: p["sources"][0].update(play=mode))
                    sd = project.project_dir(name) / "sources" / "s1"
                    if mode == "original":
                        (sd / "proxy.mp4").write_bytes(b"old proxy")
                    with patch.object(media, "thumbs", wraps=media.thumbs) as thumbs:
                        analyze.analyze_source(name, "s1", transcribe=False)
                    expected = sd / "proxy.mp4" if mode == "proxy" else SAMPLE
                    self.assertEqual(Path(thumbs.call_args.args[0]), expected)
                    self.assertEqual(project.load(name)["sources"][0]["status"], "ready")


if __name__ == "__main__":
    unittest.main()

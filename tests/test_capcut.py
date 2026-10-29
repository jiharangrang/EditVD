"""캡컷 초안 JSON과 내보내기 안전장치를 확인해요. 실제 캡컷 폴더에는 절대 쓰지 않고 임시 폴더만 써요."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from editvdo import capcut, project, render

REPO = Path(__file__).resolve().parent.parent
SAMPLE = REPO / "samples" / "test_vlog.mp4"
D = 34.033333
CANVAS = {"w": 1920, "h": 1080, "fps": 30}
SOURCES = [{"id": "s1", "path": "/v/IMG_0001.MOV", "name": "IMG_0001.MOV", "duration": D, "w": 1920, "h": 1080, "has_audio": True},
           {"id": "s2", "path": "/v/IMG_0002.MOV", "duration": 10.0, "w": 1080, "h": 1920, "has_audio": False}]
SEGMENTS = [{"sid": "s1", "src_in": 0.5, "f0": 0, "f1": 120},
            {"sid": "s1", "src_in": 10.0, "f0": 120, "f1": 413},
            {"sid": "s2", "src_in": 9.5, "f0": 413, "f1": 443}]   # 파일 끝을 넘으면 시작점을 당겨요
DRAFTS_ROOT = capcut.drafts_root  # 테스트마다 막아 두기 전의 원래 함수
CAPTIONS = [{"text": "안녕하세요", "f0": 10, "f1": 60, "x": 50, "y": 88},
            {"text": "좋아요👍", "f0": 30, "f1": 90, "x": 25, "y": 80},          # 겹침 → 두 번째 자막 트랙
            {"text": "둘째 줄\n이어서", "f0": 100, "f1": 150, "x": 75, "y": 20}]


def seed():
    return json.loads((capcut.SEED / "draft_info.json").read_text(encoding="utf-8"))


def build(folder="/tmp/root/테스트"):
    return capcut.build_draft(folder, CANVAS, SOURCES, SEGMENTS, CAPTIONS, 1793240000.25)


def sample_project(name="_t_capcut_synth"):
    """test_vlog.mp4 두 번(s1, s2)으로 만든 v2 프로젝트: 남는 구간 3개(s1 둘, s2 하나), 자막 셋."""
    return {"format": 2, "rev": 1, "name": name, "title": "캡컷 테스트", "canvas": {"w": 1280, "h": 720, "fps": 30},
            "sources": [{"id": "s1", "path": str(SAMPLE), "name": "a.mp4", "duration": D, "offset": 0.0,
                         "w": 1280, "h": 720, "fps": 30.0, "has_audio": True, "status": "ready"},
                        {"id": "s2", "path": str(SAMPLE), "name": "b.mp4", "duration": D, "offset": D,
                         "w": 1280, "h": 720, "fps": 30.0, "has_audio": True, "status": "ready"}],
            "settings": {"cut": {"min_silence": 0.5, "pad_before": 0.15, "pad_after": 0.25, "fillers": True, "long_silence": 2.0},
                         "subs": {"lead": 0.0, "hold": 0.3, "min": 0.8, "bridge": 0.3, "max_chars": 18},
                         "asr": {"model": "mlx-community/whisper-large-v3-turbo", "language": "ko", "prompt": "", "clean": False}},
            "cuts": [{"id": "c1", "start": 2.0, "end": 5.0, "type": "user", "enabled": True},
                     {"id": "c2", "start": 7.0, "end": D + 1.0, "type": "user", "enabled": True},
                     {"id": "c3", "start": D + 3.0, "end": 2 * D, "type": "user", "enabled": True},
                     {"id": "c4", "start": 0.5, "end": 1.0, "type": "gap", "enabled": False}],
            "subtitles": [{"id": "t1", "start": 1.0, "end": 6.0, "text": "잘린 곳을 넘는 자막", "pos": {"x": 30, "y": 80}},
                          {"id": "t2", "start": 1.5, "end": 1.9, "text": "겹치는 자막😀", "pos": None, "speaker": "A"},
                          {"id": "t3", "start": 3.0, "end": 4.0, "text": "다 잘린 자막", "pos": None}],
            "speakers": [{"id": "A", "name": "화자 1", "color": "#40DAF5", "pos": {"x": 70, "y": 20}}],
            "splits": [D + 2.0], "comments": [], "notes": ""}


def ids(info):
    out = [info["id"]]
    for value in info["materials"].values():
        if isinstance(value, list):
            out += [m["id"] for m in value if isinstance(m, dict) and "id" in m]
    for track in info["tracks"]:
        out += [track["id"]] + [s["id"] for s in track["segments"]]
    return out


class DraftJson(unittest.TestCase):
    """씨앗을 복제한 초안 JSON의 연결·시간·모양을 확인해요."""

    def test_ids_unique_and_every_reference_resolves(self):
        info, _, _ = build()
        every = ids(info)
        self.assertEqual(len(every), len(set(every)))
        owner = {m["id"]: key for key, value in info["materials"].items() if isinstance(value, list) for m in value}
        used = []
        for track in info["tracks"]:
            for seg in track["segments"]:
                kinds = [owner[r] for r in seg["extra_material_refs"]]
                if track["type"] == "video":
                    self.assertEqual(owner[seg["material_id"]], "videos")
                    self.assertEqual(kinds, list(capcut.VIDEO_EXTRAS))
                else:
                    self.assertEqual(owner[seg["material_id"]], "texts")
                    self.assertEqual(kinds, ["material_animations"])
                used += seg["extra_material_refs"] + [seg["material_id"]]
        self.assertEqual(len(used), len(set(used)))   # 클립마다 자기 소재 (캡컷은 자르기 등을 소재에 저장해요)
        self.assertEqual(set(owner) - set(used), set())

    def test_video_timing_is_contiguous_in_microseconds(self):
        info, meta, _ = build()
        video = info["tracks"][0]["segments"]
        self.assertEqual(len(video), 3)
        at = 0
        for seg in video:
            self.assertEqual(seg["target_timerange"]["start"], at)
            self.assertEqual(seg["source_timerange"]["duration"], seg["target_timerange"]["duration"])
            at += seg["target_timerange"]["duration"]
        self.assertEqual(capcut.frame_us(413, 30), 13766666)  # 캡컷이 직접 쓴 값과 같은 버림
        self.assertEqual(video[1]["target_timerange"], {"duration": 13766666 - 4000000, "start": 4000000})
        self.assertEqual(info["duration"], capcut.frame_us(443, 30))
        self.assertEqual(meta["tm_duration"], info["duration"])
        self.assertEqual([s["source_timerange"]["start"] for s in video], [500000, 10000000, 9000000])
        mats = {m["id"]: m for m in info["materials"]["videos"]}
        for seg in video:
            src = seg["source_timerange"]
            self.assertLessEqual(src["start"] + src["duration"], mats[seg["material_id"]]["duration"])
        self.assertEqual(info["fps"], 30.0)
        self.assertEqual((info["canvas_config"]["width"], info["canvas_config"]["height"]), (1920, 1080))

    def test_material_length_rounds_up_to_frames(self):
        """캡컷은 소재 길이를 프레임 단위로 올려 적어요 (1029 초안: 2260.374783초 → 2260400000).
        그래서 끝 프레임이 반올림으로 늘어난 마지막 클립도 소재 안에 들어가고, 중간 클립 시작점도 밀리지 않아요."""
        src = [{"id": "a", "path": "/v/a.mov", "duration": 5.02, "w": 1920, "h": 1080, "has_audio": True},
               {"id": "b", "path": "/v/b.mov", "duration": 2260.374783, "w": 2688, "h": 1512, "has_audio": True}]
        segs = [{"sid": "a", "src_in": 0.0, "f0": 0, "f1": 151}, {"sid": "a", "src_in": 1.0, "f0": 151, "f1": 272},
                {"sid": "b", "src_in": 0.0, "f0": 272, "f1": 302}]
        info, meta, _ = capcut.build_draft("/tmp/root/x", CANVAS, src, segs, [], 1793240000.0)
        mats = {m["id"]: m for m in info["materials"]["videos"]}
        self.assertEqual([m["duration"] for m in info["materials"]["videos"]], [5033333, 5033333, 2260400000])
        video = info["tracks"][0]["segments"]
        self.assertEqual([s["source_timerange"]["start"] for s in video], [0, 1000000, 0])
        for seg in video:
            src_range = seg["source_timerange"]
            self.assertLessEqual(src_range["start"] + src_range["duration"], mats[seg["material_id"]]["duration"])
        listed = next(g for g in meta["draft_materials"] if g["type"] == 0)["value"]
        self.assertEqual([e["duration"] for e in listed if e["metetype"] == "video"], [5020000, 2260374000])

    def test_caption_lanes_transform_and_utf16_ranges(self):
        info, _, _ = build()
        texts = {m["id"]: m for m in info["materials"]["texts"]}
        lanes = [t for t in info["tracks"] if t["type"] == "text"]
        self.assertEqual([len(t["segments"]) for t in lanes], [2, 1])
        rows = sorted((seg["render_index"], k, seg) for k, t in enumerate(lanes) for seg in t["segments"])
        self.assertEqual([r[0] for r in rows], [14000, 14001, 14002])
        expect = [("안녕하세요", 5, {"x": 0.0, "y": -0.76}, 0), ("좋아요👍", 5, {"x": -0.5, "y": -0.6}, 1),
                  ("둘째 줄\n이어서", 8, {"x": 0.5, "y": 0.6}, 0)]
        for (_, lane, seg), (text, units, pos, want_lane) in zip(rows, expect):
            content = json.loads(texts[seg["material_id"]]["content"])
            self.assertEqual(list(content), ["styles", "text"])
            self.assertEqual(content["text"], text)
            self.assertEqual(content["styles"][0]["range"], [0, units])
            self.assertEqual(seg["clip"]["transform"], pos)
            self.assertEqual((lane, seg["track_render_index"]), (want_lane, want_lane + 1))
        self.assertEqual(rows[0][2]["target_timerange"], {"duration": capcut.frame_us(60, 30) - capcut.frame_us(10, 30),
                                                          "start": capcut.frame_us(10, 30)})
        groups = {m["group_id"] for m in texts.values()}
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups.pop().startswith("import_"))
        self.assertTrue(all(m["type"] == "subtitle" for m in texts.values()))

    def test_transform_and_utf16_helpers(self):
        self.assertEqual(capcut.transform(0, 0), {"x": -1.0, "y": 1.0})
        self.assertEqual(capcut.transform(100, 100), {"x": 1.0, "y": -1.0})
        self.assertEqual(capcut.transform(50, 50), {"x": 0.0, "y": 0.0})
        self.assertEqual([capcut.utf16_len(t) for t in ("첫 번째 자막입니다", "👍", "a😀b", "가\n나")], [10, 2, 4, 3])

    def test_media_registered_and_linked(self):
        info, meta, store = build()
        listed = next(g for g in meta["draft_materials"] if g["type"] == 0)["value"]
        by_id = {m["id"]: m for m in listed}
        for mat in info["materials"]["videos"]:
            entry = by_id[mat["local_material_id"]]
            self.assertEqual(entry["file_Path"], mat["path"])
            self.assertEqual((entry["metetype"], entry["width"], entry["height"]), ("video", mat["width"], mat["height"]))
            self.assertEqual(entry["duration"], mat["duration"] // 1000 * 1000)
        self.assertEqual(len(listed), 3)   # 캡컷이 늘 넣는 빈 항목 + 파일 둘
        self.assertEqual([m["has_audio"] for m in info["materials"]["videos"]], [True, True, False])
        self.assertEqual(len({m["local_material_id"] for m in info["materials"]["videos"]}), 2)
        self.assertTrue(all(not g["value"] for g in meta["draft_materials"] if g["type"] != 0))
        children = next(g for g in store["draft_virtual_store"] if g["type"] == 1)["value"]
        self.assertEqual([c["child_id"] for c in children], [m["id"] for m in listed])
        self.assertEqual((meta["draft_name"], meta["draft_fold_path"], meta["draft_root_path"]),
                         ("테스트", "/tmp/root/테스트", "/tmp/root"))
        self.assertEqual(meta["tm_draft_create"], 1793240000250000)

    def test_seed_markers_kept_and_device_ids_blank(self):
        info, _, _ = build()
        base = seed()
        for key in ("version", "new_version", "draft_type", "render_index_track_mode_on"):
            self.assertEqual(info[key], base[key])
        for key in ("platform", "last_modified_platform"):
            for field in ("app_id", "app_source", "app_version", "os", "os_version"):
                self.assertEqual(info[key][field], base[key][field])
            self.assertEqual([info[key][f] for f in ("device_id", "hard_disk_id", "mac_address")], ["", "", ""])
        self.assertNotEqual(info["id"], base["id"])

    def test_items_keep_every_seed_key_with_same_type(self):
        info, _, _ = build()
        base = seed()
        video_seed = next(t for t in base["tracks"] if t["type"] == "video")["segments"][0]
        text_seed = next(t for t in base["tracks"] if t["type"] == "text")["segments"][0]
        pairs = [(info["tracks"][0]["segments"][1], video_seed), (info["tracks"][1]["segments"][0], text_seed),
                 (info["materials"]["videos"][0], base["materials"]["videos"][0]),
                 (info["materials"]["texts"][0], base["materials"]["texts"][0]),
                 (info["tracks"][1], next(t for t in base["tracks"] if t["type"] == "text"))]
        pairs += [(info["materials"][k][0], base["materials"][k][0]) for k in (*capcut.VIDEO_EXTRAS, "material_animations")]
        for ours, theirs in pairs:
            self.assert_shape(ours, theirs, theirs.get("type", "item"))
        self.assertEqual(set(info), set(base))
        self.assertEqual(set(info["materials"]), set(base["materials"]))

    def assert_shape(self, ours, theirs, path):
        for key, value in theirs.items():
            self.assertIn(key, ours, f"{path}.{key}")
            self.assertIs(type(ours[key]), type(value), f"{path}.{key}")
            if isinstance(value, dict):
                self.assert_shape(ours[key], value, f"{path}.{key}")


class CaptionPieces(unittest.TestCase):
    """잘린 구간을 넘는 자막은 조각으로 나누되 짧은 넘침은 붙여요."""
    SEGS = [{"a": 0.0, "b": 2.0, "f0": 0, "f1": 60}, {"a": 5.0, "b": 8.0, "f0": 60, "f1": 150},
            {"a": 8.0, "b": 9.0, "f0": 150, "f1": 180}]  # 8.0 은 분할 표시: 원본에서 이어져요

    def test_parts_split_only_at_removed_time(self):
        self.assertEqual(render.frame_parts(1.0, 6.0, self.SEGS, 30), [(30, 60), (60, 90)])
        self.assertEqual(render.frame_parts(6.0, 8.5, self.SEGS, 30), [(90, 165)])
        self.assertEqual(render.frame_parts(2.5, 4.5, self.SEGS, 30), [])
        self.assertEqual(render.frame_parts(1.0, 5.1, self.SEGS, 30), [(30, 60), (60, 63)])

    def test_short_spill_joins_neighbour(self):
        self.assertEqual(capcut._join_short([(30, 60), (60, 63)], 15), [(30, 63)])
        self.assertEqual(capcut._join_short([(30, 60), (60, 90)], 15), [(30, 60), (60, 90)])
        self.assertEqual(capcut._join_short([(55, 60), (60, 90), (90, 120)], 15), [(55, 90), (90, 120)])


class Safety(unittest.TestCase):
    """캡컷이 켜져 있거나 열린 프로젝트가 있으면 쓰지 않고, 목록 파일의 다른 항목은 그대로 둬요."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="editvdo-capcut-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        guard = patch.object(capcut, "drafts_root", side_effect=AssertionError("실제 캡컷 폴더를 쓰면 안 돼요"))
        guard.start()
        self.addCleanup(guard.stop)

    def snapshot(self):
        return sorted(str(x.relative_to(self.root)) for x in self.root.rglob("*"))

    def test_refuses_while_capcut_runs(self):
        before = self.snapshot()
        with patch.object(capcut, "capcut_running", return_value=True):
            with self.assertRaisesRegex(ValueError, capcut.CLOSE_FIRST):
                capcut.export_draft(sample_project(), "x", root=self.root, target="capcut")
        self.assertEqual(self.snapshot(), before)
        with self.assertRaisesRegex(ValueError, "folder·capcut"):
            capcut.export_draft(sample_project(), "x", root=self.root, target="cloud")
        self.assertEqual(self.snapshot(), before)

    def test_stale_lock_does_not_block(self):
        (self.root / "지난 프로젝트").mkdir()
        (self.root / "지난 프로젝트" / ".locked").touch()   # 캡컷이 꺼져 있으면 남은 .locked 는 무시
        with patch.object(capcut, "capcut_running", return_value=False):
            folder = capcut.export_draft(sample_project(), "x", root=self.root, target="capcut")
        self.assertTrue((folder / "draft_info.json").exists())
        self.assertTrue((self.root / "지난 프로젝트" / ".locked").exists())

    def test_register_keeps_other_entries(self):
        example = json.loads((capcut.SEED / "root_meta_info.example.json").read_text(encoding="utf-8"))
        others = [dict(example["root_entry_example"], draft_name=n, draft_id=n.upper()) for n in ("1029", "1029 (1)")]
        path = self.root / "root_meta_info.json"
        path.write_text(json.dumps({"all_draft_store": others, "draft_ids": 2, "root_path": str(self.root)}), encoding="utf-8")
        entry = capcut.root_entry({"draft_fold_path": f"{self.root}/새 초안", "draft_id": "NEW", "draft_name": "새 초안",
                                   "draft_root_path": str(self.root), "tm_draft_create": 1, "tm_draft_modified": 2,
                                   "tm_duration": 3}, 99)
        self.assertTrue(capcut.register(self.root, entry))
        store = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(store["all_draft_store"], [entry, *others])
        self.assertEqual((store["draft_ids"], store["root_path"]), (2, str(self.root)))
        self.assertEqual(entry["draft_json_file"], f"{self.root}/새 초안/draft_info.json")
        self.assertEqual(set(entry), set(example["root_entry_example"]))
        self.assertEqual([x.name for x in self.root.iterdir()], ["root_meta_info.json"])
        path.write_text("{broken", encoding="utf-8")
        self.assertFalse(capcut.register(self.root, entry))
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_drafts_root_follows_capcut_setting(self):
        setting = self.root / "globalSetting"
        setting.write_text(f"[General]\ncurrentCachePath=/x\ncurrentCustomDraftPath={self.root}\n", encoding="utf-8")
        with patch.object(capcut, "SETTING", setting), patch.dict(os.environ):
            os.environ.pop("EDITVDO_CAPCUT_ROOT", None)
            self.assertEqual(DRAFTS_ROOT(), self.root)
            setting.write_text("currentCustomDraftPath=/없는/폴더\n", encoding="utf-8")
            self.assertEqual(DRAFTS_ROOT(), capcut.DEFAULT_ROOT)
            os.environ["EDITVDO_CAPCUT_ROOT"] = str(self.root / "다른 곳")  # 환경 변수가 먼저예요
            self.assertEqual(DRAFTS_ROOT(), self.root / "다른 곳")

    def test_place_media_clone_then_link_then_copy(self):
        src = self.root / "src.bin"
        src.write_bytes(b"editvdo" * 1000)
        self.assertEqual(capcut.place_media(src, self.root / "a.bin"), "clone")

        class NoClone:
            @staticmethod
            def clonefile(*args):
                return -1
        with patch.object(capcut, "_LIBC", NoClone):
            self.assertEqual(capcut.place_media(src, self.root / "b.bin"), "link")
            self.assertEqual(os.stat(self.root / "b.bin").st_ino, os.stat(src).st_ino)
            with patch.object(capcut.os, "link", side_effect=OSError("cross-device")):
                self.assertEqual(capcut.place_media(src, self.root / "c.bin"), "copy")
        for name in ("a.bin", "b.bin", "c.bin"):
            self.assertEqual((self.root / name).read_bytes(), src.read_bytes())
        self.assertNotEqual(os.stat(self.root / "a.bin").st_ino, os.stat(src).st_ino)


class Export(unittest.TestCase):
    """test_vlog.mp4 로 실제 폴더를 만들어 봐요 (임시 루트)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="editvdo-capcut-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        guard = patch.object(capcut, "drafts_root", side_effect=AssertionError("실제 캡컷 폴더를 쓰면 안 돼요"))
        guard.start()
        self.addCleanup(guard.stop)
        running = patch.object(capcut, "capcut_running", return_value=False)
        running.start()
        self.addCleanup(running.stop)

    def test_export_writes_complete_draft(self):
        p = sample_project()
        (self.root / "캡컷 테스트").mkdir()   # 같은 이름이 있으면 "이름 2"
        folder = capcut.export_draft(p, "캡컷 테스트", root=self.root, target="capcut")
        self.assertEqual(folder, self.root / "캡컷 테스트 2")
        names = {"draft_info.json", "template-2.tmp", "draft_meta_info.json", "draft_virtual_store.json",
                 "draft_agency_config.json", "timeline_layout.json", "media"}
        self.assertEqual({x.name for x in folder.iterdir()}, names)
        self.assertEqual((folder / "template-2.tmp").read_bytes(), (folder / "draft_info.json").read_bytes())
        info = json.loads((folder / "draft_info.json").read_text(encoding="utf-8"))
        meta = json.loads((folder / "draft_meta_info.json").read_text(encoding="utf-8"))
        layout = json.loads((folder / "timeline_layout.json").read_text(encoding="utf-8"))
        self.assertEqual(layout["dockItems"][0]["timelineIds"], [info["id"]])
        media = list((folder / "media").iterdir())
        self.assertEqual([m.name for m in media], ["test_vlog.mp4"])   # 같은 파일 두 소스 → 한 번만
        self.assertEqual(media[0].stat().st_size, SAMPLE.stat().st_size)
        self.assertEqual({m["path"] for m in info["materials"]["videos"]},
                         {capcut.DRAFT_PLACEHOLDER + "/media/test_vlog.mp4"})   # 폴더째 옮겨도 열리는 상대 경로
        listed = [m for g in meta["draft_materials"] if g["type"] == 0 for m in g["value"] if m["metetype"] == "video"]
        self.assertEqual({m["file_Path"] for m in listed}, {"./media/test_vlog.mp4"})
        self.assertEqual([m["duration"] for m in info["materials"]["videos"]], [34033333] * 4)   # 클립마다 하나
        video = info["tracks"][0]["segments"]
        # 남는 구간 [0,2] [5,7] (s1) [D+1, D+2] [D+2, D+3] (s2, 분할 표시로 둘)
        self.assertEqual([(s["source_timerange"]["start"], s["target_timerange"]["duration"]) for s in video],
                         [(0, 2000000), (5000000, 2000000), (1000000, 1000000), (2000000, 1000000)])
        self.assertEqual(info["duration"], 6000000)
        texts = [s for t in info["tracks"] if t["type"] == "text" for s in t["segments"]]
        content = {m["id"]: json.loads(m["content"])["text"] for m in info["materials"]["texts"]}
        rows = sorted((s["target_timerange"]["start"], s["target_timerange"]["duration"], content[s["material_id"]],
                       s["clip"]["transform"]["x"]) for s in texts)
        self.assertEqual(rows, [(1000000, 1000000, "잘린 곳을 넘는 자막", -0.4), (1500000, 400000, "겹치는 자막😀", 0.4),
                                (2000000, 1000000, "잘린 곳을 넘는 자막", -0.4)])
        self.assertEqual(len([t for t in info["tracks"] if t["type"] == "text"]), 2)
        store = json.loads((self.root / "root_meta_info.json").read_text(encoding="utf-8"))
        self.assertEqual([e["draft_fold_path"] for e in store["all_draft_store"]], [str(folder)])
        self.assertEqual(store["all_draft_store"][0]["draft_id"], meta["draft_id"])
        self.assertEqual(SAMPLE.stat().st_size, 13560167)   # 원본은 그대로

    def test_export_without_media_or_subtitles(self):
        folder = capcut.export_draft(sample_project(), "", root=self.root, include_subs=False, copy_media=False,
                                     target="capcut")
        self.assertEqual(folder.name, "캡컷 테스트")
        info = json.loads((folder / "draft_info.json").read_text(encoding="utf-8"))
        self.assertEqual([t["type"] for t in info["tracks"]], ["video"])
        self.assertEqual({m["path"] for m in info["materials"]["videos"]}, {str(SAMPLE.resolve())})
        self.assertFalse((folder / "media").exists())

    def test_failed_export_leaves_nothing(self):
        p = sample_project()
        with patch.object(capcut, "build_draft", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                capcut.export_draft(p, "실패", root=self.root, target="capcut")
            with tempfile.TemporaryDirectory() as tmp, patch.object(project, "PROJECTS", Path(tmp)):
                with self.assertRaises(RuntimeError):
                    capcut.export_draft(p, "실패")
                self.assertEqual(list((Path(tmp) / p["name"] / "exports").iterdir()), [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_folder_target_for_another_mac(self):
        """기본(folder): 프로젝트 exports/capcut-<시각>/<이름>/ 에 영상까지 담아요. 캡컷이 켜져 있어도 되고
        캡컷 초안 폴더·목록(root_meta_info.json)은 건드리지 않아요. 대비책도 같은 폴더에."""
        with tempfile.TemporaryDirectory() as tmp, patch.object(project, "PROJECTS", Path(tmp)), \
                patch.object(capcut, "capcut_running", return_value=True):
            folder = capcut.export_draft(sample_project(), "다른 맥", copy_media=False, fallback=True)
            root = folder.parent
            self.assertEqual((folder.name, root.parent), ("다른 맥", Path(tmp) / "_t_capcut_synth" / "exports"))
            self.assertRegex(root.name, r"^capcut-\d{8}-\d{6}$")
            self.assertEqual(sorted(x.name for x in root.iterdir()), ["captions.srt", "clips", "다른 맥"])
            media = list((folder / "media").iterdir())   # copy_media=False 여도 늘 담아요
            self.assertEqual([m.name for m in media], ["test_vlog.mp4"])
            self.assertEqual(media[0].stat().st_size, SAMPLE.stat().st_size)
            info = json.loads((folder / "draft_info.json").read_text(encoding="utf-8"))
            self.assertEqual({m["path"] for m in info["materials"]["videos"]},
                             {capcut.DRAFT_PLACEHOLDER + "/media/test_vlog.mp4"})
            self.assertFalse(list(Path(tmp).rglob("root_meta_info.json")))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_fallback_clips_and_srt(self):
        out = Path(tempfile.mkdtemp(prefix="editvdo-clips-"))
        self.addCleanup(shutil.rmtree, out, True)
        capcut.export_clips(sample_project(), out)
        clips = sorted((out / "clips").iterdir())
        self.assertEqual([c.name for c in clips], ["001.mp4", "002.mp4", "003.mp4", "004.mp4"])
        frames = [int(subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0", "-show_entries",
                                      "stream=nb_read_packets", "-of", "csv=p=0", str(c)], capture_output=True, text=True,
                                     check=True).stdout) for c in clips]
        self.assertEqual(frames, [60, 60, 30, 30])
        srt = (out / "captions.srt").read_text(encoding="utf-8")
        self.assertIn("00:00:01,000 --> 00:00:03,000\n잘린 곳을 넘는 자막", srt)
        self.assertIn("00:00:01,500 --> 00:00:01,900\n겹치는 자막😀", srt)


@unittest.skipUnless(hasattr(project, "create"), "project.create 가 아직 없어요")
class ProjectIntegration(unittest.TestCase):
    """진짜 프로젝트(project.create → transact/apply_ops)로 만든 편집을 임시 루트에 내보내요."""

    def test_real_project_export(self):
        name = project.create("캡컷 통합 테스트", [str(SAMPLE)], name=f"_t_capcut_{os.getpid()}")
        self.addCleanup(shutil.rmtree, project.project_dir(name), True)
        root = Path(tempfile.mkdtemp(prefix="editvdo-capcut-"))
        self.addCleanup(shutil.rmtree, root, True)
        ops = [{"op": "put", "coll": "cuts", "item": {"id": "c1", "start": 3.0, "end": 8.0, "type": "manual", "enabled": True}},
               {"op": "put", "coll": "cuts", "item": {"id": "c2", "start": 20.0, "end": 30.0, "type": "manual", "enabled": True}},
               {"op": "put", "coll": "subtitles", "item": {"id": "t1", "start": 1.0, "end": 2.5, "text": "첫 줄",
                                                            "speech": [1.0, 2.2], "locked": True}},
               {"op": "put", "coll": "subtitles", "item": {"id": "t2", "start": 21.0, "end": 25.0, "text": "다 잘린 줄",
                                                            "speech": [21.0, 24.0], "locked": True}}]
        project.transact(name, lambda p: project.apply_ops(p, ops, "cli"))
        p = project.load(name)
        with patch.object(capcut, "capcut_running", return_value=False):
            folder = capcut.export_draft(p, "통합", root=root)
        info = json.loads((folder / "draft_info.json").read_text(encoding="utf-8"))
        keeps = project.keep_segments(p)
        self.assertEqual(len(info["tracks"][0]["segments"]), len(keeps))
        self.assertAlmostEqual(info["duration"] / 1e6, project.edited_duration(p), delta=1 / 30)
        texts = [json.loads(m["content"])["text"] for m in info["materials"]["texts"]]
        self.assertEqual(texts, ["첫 줄"])


if __name__ == "__main__":
    unittest.main()

"""편집본을 캡컷(CapCut 9.6) 프로젝트로 내보내요.
설치된 캡컷이 직접 쓴 초안(capcut_seed/)을 복제해서 id·시간·경로·이름·글자만 바꿔요.
원본 영상과 다른 캡컷 프로젝트는 건드리지 않아요."""
import copy
import ctypes
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from contextlib import suppress
from pathlib import Path

from . import project, render

SEED = Path(__file__).resolve().parent / "capcut_seed"
CAPCUT_DATA = Path.home() / "Movies/CapCut/User Data"
DEFAULT_ROOT = CAPCUT_DATA / "Projects/com.lveditor.draft"
SETTING = CAPCUT_DATA / "Config/globalSetting"
VIDEO_EXTRAS = ("speeds", "placeholder_infos", "canvases", "sound_channel_mappings", "material_colors", "vocal_separations")
TEXT_INDEX = 14000  # 캡컷이 자막 조각에 매기는 render_index 시작값
MIN_PIECE = 0.5     # 잘린 구간 때문에 나뉜 자막 조각이 이보다 짧으면(초) 옆 조각에 붙여요
CLOSE_FIRST = "캡컷을 먼저 닫아 주세요"
TARGETS = ("folder", "capcut")  # 내보낼 곳: 프로젝트 exports/ 폴더(다른 맥으로 옮기기) | 이 맥 캡컷
DRAFT_PLACEHOLDER = "##_draftpath_placeholder_0E685133-18CE-45ED-8CB8-2904A212EC80_##"  # 캡컷 9.6 이 초안 폴더를 가리킬 때 쓰는 고정 표식
_LIBC = ctypes.CDLL(None, use_errno=True)


def drafts_root():
    """환경 변수 EDITVDO_CAPCUT_ROOT, 아니면 캡컷 설정(globalSetting 의 currentCustomDraftPath)에 있는 초안 폴더,
    둘 다 없으면 기본 위치예요."""
    if os.environ.get("EDITVDO_CAPCUT_ROOT"):
        return Path(os.environ["EDITVDO_CAPCUT_ROOT"]).expanduser()
    try:
        for line in SETTING.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "currentCustomDraftPath" and value.strip() and Path(value.strip()).is_dir():
                return Path(value.strip())
    except OSError:
        pass
    return DEFAULT_ROOT


def capcut_running():
    return subprocess.run(["pgrep", "-x", "CapCut"], capture_output=True).returncode == 0


def _seed(name):
    return json.loads((SEED / name).read_text(encoding="utf-8"))


def _dump(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))  # 캡컷처럼 한 줄로


def _uid():
    return str(uuid.uuid4()).upper()


def _clone(item, **changes):
    out = copy.deepcopy(item)
    out.update(changes)
    return out


def frame_us(frame, fps):
    """편집본 프레임 번호 → µs (캡컷처럼 버림: 30fps 413프레임 = 13766666)."""
    return int(frame * 1_000_000 // fps)


def utf16_len(text):
    """캡컷 글자 범위(range)는 UTF-16 코드 단위로 세요 (이모지 하나 = 2)."""
    return len(text.encode("utf-16-le")) // 2


def transform(x, y):
    """화면 위치(%, 왼쪽 위 기준, 글 상자 가운데) → 캡컷 좌표(가운데 0, 오른쪽·위가 +1)."""
    return {"x": round(x / 50 - 1, 6), "y": round(1 - y / 50, 6)}


def build_draft(folder, canvas, sources, segments, captions, now):
    """캡컷 초안 JSON 세 개(draft_info, draft_meta_info, draft_virtual_store)를 만들어요. 파일은 쓰지 않아요.
    sources:  [{id, path, name?, duration(초), w, h, has_audio}] — path 는 캡컷이 읽을 파일 경로
    segments: [{sid, src_in(초), f0, f1}] 편집 순서, f0~f1 은 편집본 프레임 (0부터 빈틈없이)
    captions: [{text, f0, f1, x, y}] 편집본 프레임·화면 위치(%) — 겹치면 자막 트랙을 더 만들어요"""
    folder, fps = Path(folder), canvas["fps"]
    now_s, now_us = int(now), int(now * 1_000_000)
    seed = _seed("draft_info.json")
    tpl = seed["materials"]
    vtrack = next(t for t in seed["tracks"] if t["type"] == "video")
    ttrack = next(t for t in seed["tracks"] if t["type"] == "text")
    info = copy.deepcopy(seed)
    mats = info["materials"]
    for key, value in mats.items():
        if isinstance(value, list):
            mats[key] = []
    for key in ("platform", "last_modified_platform"):
        info[key].update(device_id="", hard_disk_id="", mac_address="")

    meta = _seed("draft_meta_info.json")
    groups = {g["type"]: g for g in meta["draft_materials"]}
    vmeta = next(m for m in groups[0]["value"] if m["metetype"] == "video")
    listed = [_clone(m, create_time=now_s, import_time=now_s, import_time_ms=now_us)
              for m in groups[0]["value"] if m["metetype"] != "video"]  # 캡컷이 늘 넣는 빈 항목
    videos = {}
    for s in sources:
        local, raw = str(uuid.uuid4()), round(s["duration"] * 1_000_000)
        dur = frame_us(math.ceil(round(s["duration"] * fps, 6)), fps)  # 캡컷처럼 소재 길이는 프레임 단위로 올림
        name = s.get("name") or Path(s["path"]).name
        path = file_path = str(s["path"])
        if Path(path).is_relative_to(folder):  # 폴더 안 영상은 캡컷이 쓰는 상대 경로로 → 폴더째 다른 맥으로 옮겨도 열려요
            rel = Path(path).relative_to(folder).as_posix()
            path, file_path = f"{DRAFT_PLACEHOLDER}/{rel}", f"./{rel}"
        videos[s["id"]] = _clone(tpl["videos"][0], path=path, material_name=name, duration=dur,
                                 width=int(s["w"]), height=int(s["h"]), has_audio=bool(s["has_audio"]),
                                 local_material_id=local, unique_id="")
        whole = {"duration": raw // 1000 * 1000, "start": 0}  # 캡컷은 여기 길이를 ms 단위로 적어요
        listed.append(_clone(vmeta, id=local, file_Path=file_path, extra_info=name, duration=whole["duration"],
                             width=int(s["w"]), height=int(s["h"]), create_time=now_s, import_time=now_s,
                             import_time_ms=now_us, roughcut_time_range=whole))
    for g in meta["draft_materials"]:
        g["value"] = listed if g["type"] == 0 else []

    track = _clone(vtrack, id=_uid(), segments=[])
    end = 0
    for seg in segments:
        if seg["f1"] <= seg["f0"]:
            continue
        mat = _clone(videos[seg["sid"]], id=_uid())  # 클립마다 소재 하나: 자르기·안정화는 소재에 저장돼요
        mats["videos"].append(mat)
        t0, t1 = frame_us(seg["f0"], fps), frame_us(seg["f1"], fps)
        start = max(0, min(frame_us(round(seg["src_in"] * fps), fps), mat["duration"] - (t1 - t0)))
        refs = []
        for key in VIDEO_EXTRAS:
            extra = _clone(tpl[key][0], id=_uid())
            mats[key].append(extra)
            refs.append(extra["id"])
        track["segments"].append(_clone(vtrack["segments"][0], id=_uid(), material_id=mat["id"], extra_material_refs=refs,
                                        source_timerange={"duration": t1 - t0, "start": start},
                                        target_timerange={"duration": t1 - t0, "start": t0},
                                        volume=1.0, last_nonzero_volume=1.0))
        end = t1
    tracks, lane_ends = [track], []
    group = f"import_{now_us // 1000}"
    for n, cap in enumerate(sorted((c for c in captions if c["f1"] > c["f0"]), key=lambda c: (c["f0"], c["f1"]))):
        lane = next((k for k, last in enumerate(lane_ends) if last <= cap["f0"]), len(lane_ends))
        if lane == len(lane_ends):
            lane_ends.append(0)
            tracks.append(_clone(ttrack, id=_uid(), segments=[]))
        lane_ends[lane] = cap["f1"]
        content = json.loads(tpl["texts"][0]["content"])
        content["text"] = cap["text"]
        for style in content["styles"]:
            style["range"] = [0, utf16_len(cap["text"])]
        text = _clone(tpl["texts"][0], id=_uid(), content=_dump(content), group_id=group)
        anim = _clone(tpl["material_animations"][0], id=_uid())
        mats["texts"].append(text)
        mats["material_animations"].append(anim)
        t0, t1 = frame_us(cap["f0"], fps), frame_us(cap["f1"], fps)
        seg = _clone(ttrack["segments"][0], id=_uid(), material_id=text["id"], extra_material_refs=[anim["id"]],
                     target_timerange={"duration": t1 - t0, "start": t0},
                     render_index=TEXT_INDEX + n, track_render_index=lane + 1)
        seg["clip"]["transform"] = transform(cap["x"], cap["y"])
        tracks[lane + 1]["segments"].append(seg)
    info.update(id=_uid(), duration=end, fps=float(fps), tracks=tracks,
                canvas_config={**seed["canvas_config"], "width": int(canvas["w"]), "height": int(canvas["h"])})

    meta.update(draft_id=_uid(), draft_name=folder.name, draft_fold_path=str(folder), draft_root_path=str(folder.parent),
                tm_draft_create=now_us, tm_draft_modified=now_us, tm_duration=end)
    store = _seed("draft_virtual_store.json")
    for g in store["draft_virtual_store"]:
        if g["type"] == 1:
            g["value"] = [{"child_id": m["id"], "parent_id": ""} for m in listed]
    return info, meta, store


def root_entry(meta, size):
    """root_meta_info.json 의 all_draft_store 항목 (캡컷이 쓴 항목 모양 그대로)."""
    folder = meta["draft_fold_path"]
    return _clone(_seed("root_meta_info.example.json")["root_entry_example"],
                  draft_cover=f"{folder}/draft_cover.jpg", draft_fold_path=folder, draft_id=meta["draft_id"],
                  draft_json_file=f"{folder}/draft_info.json", draft_name=meta["draft_name"],
                  draft_root_path=meta["draft_root_path"], draft_timeline_materials_size=size,
                  tm_draft_create=meta["tm_draft_create"], tm_draft_modified=meta["tm_draft_modified"],
                  tm_duration=meta["tm_duration"])


def register(root, entry):
    """캡컷 목록(root_meta_info.json)에 새 초안을 맨 앞에 더해요. 다른 항목은 그대로 두고 원자적으로 바꿔 써요.
    읽을 수 없는 파일이면 손대지 않아요 (캡컷이 폴더를 직접 찾아요)."""
    path = Path(root) / "root_meta_info.json"
    if path.exists():
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return False
    else:
        store = {"all_draft_store": [], "draft_ids": 1, "root_path": str(root)}
    store["all_draft_store"] = [entry, *store.get("all_draft_store", [])]
    tmp = path.with_name(path.name + ".editvdo-tmp")
    tmp.write_text(_dump(store), encoding="utf-8")
    os.replace(tmp, path)
    return True


def place_media(src, dst):
    """원본을 캡컷 폴더 안에 둬요 (캡컷은 샌드박스라서 자기 폴더만 늘 읽을 수 있어요).
    APFS 복제(cp -c 와 같은 clonefile) → 하드 링크 → 복사 순서로 시도하고 쓴 방법을 돌려줘요. 원본은 그대로예요."""
    if _LIBC.clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0:
        return "clone"
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        shutil.copyfile(src, dst)
        return "copy"


def media_duration(path):
    """파일의 실제 길이(초)를 ffprobe 로 읽어요."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return float(out.strip())


def _free(folder, stem, suffix=""):
    """겹치지 않는 이름: 이름, 이름 2, 이름 3 …"""
    path, n = folder / f"{stem}{suffix}", 1
    while path.exists():
        n += 1
        path = folder / f"{stem} {n}{suffix}"
    return path


def _join_short(parts, min_frames):
    """잘린 구간 때문에 나뉜 자막 조각 가운데 짧은 것(말 끝 여유가 다음 클립으로 넘친 부분 등)은 옆 조각과 합쳐요."""
    out = []
    for f0, f1 in parts:
        if out and (f1 - f0 < min_frames or out[-1][1] - out[-1][0] < min_frames):
            out[-1] = (out[-1][0], f1)
        else:
            out.append((f0, f1))
    return out


def export_clips(p, out_dir, on_progress=None):
    """대비책: 남는 구간마다 클립(clips/001.mp4 …, 프레임 정확히 다시 인코딩)과 편집본 시간 자막(captions.srt)을 만들어요.
    캡컷에서 클립을 차례로 놓고 SRT를 불러오면 같은 편집이 돼요."""
    out_dir = Path(out_dir)
    segs = [s for s in render.plan(p, at_splits=True) if s["f1"] > s["f0"]]
    if not segs:
        raise ValueError("남은 영상이 없어요. 컷을 하나 이상 살려 주세요")
    render.require_files(segs)
    (out_dir / "clips").mkdir(parents=True, exist_ok=True)
    outs = [out_dir / "clips" / f"{i:03d}.mp4" for i in range(1, len(segs) + 1)]
    render.encode_all(segs, outs, p["canvas"], audio="aac", on_progress=on_progress)
    render.export_srt(p, out_dir / "captions.srt", segs)
    return out_dir


def export_draft(p, name, root=None, include_subs=True, copy_media=True, fallback=False, on_progress=None,
                 target="folder"):
    """편집본을 캡컷 프로젝트 폴더(root 안 <이름>/)로 만들어요. 만든 폴더 경로를 돌려줘요.
    남는 구간(분할 표시 포함)마다 클립 하나라서 캡컷에서 전환 효과를 넣을 수 있어요.
    target="folder": root 기본값은 프로젝트 exports/capcut-<시각>/. 영상을 늘 폴더 안에 담아서, 폴더째 다른 맥의
      캡컷 초안 폴더에 넣으면 열려요. 캡컷이 켜져 있어도 되고 캡컷 목록(root_meta_info.json)은 건드리지 않아요.
    target="capcut": root 기본값은 이 맥 캡컷 초안 폴더(drafts_root)이고 캡컷 목록에 올려요. 캡컷이 켜져 있으면
      아무것도 쓰지 않고 멈춰요(꺼져 있을 때 남은 .locked 는 지난번 비정상 종료의 흔적이라 무시해요).
    fallback=True 면 클립+SRT 대비책도 만들어요 (folder 는 같은 root 에, capcut 은 프로젝트 exports/capcut-<시각>/ 에)."""
    if target not in TARGETS:
        raise ValueError("내보낼 곳은 folder·capcut 중 하나예요")
    here = target == "capcut"
    if here:
        root = Path(root) if root else drafts_root()
        if capcut_running():
            raise ValueError(CLOSE_FIRST)
        if not root.is_dir():
            raise ValueError("캡컷 프로젝트 폴더가 없어요. 캡컷을 한 번 실행해 주세요")
    else:
        copy_media = True  # 다른 맥에서도 열리게 영상은 늘 폴더 안에 (APFS 복제라 공간을 거의 안 써요)
    fps = p["canvas"]["fps"]
    segs = [s for s in render.plan(p, at_splits=True) if s["f1"] > s["f0"]]
    if not segs:
        raise ValueError("남은 영상이 없어요. 컷을 하나 이상 살려 주세요")
    render.require_files(segs)
    srcs = {s["id"]: s for s in p["sources"]}
    title = re.sub(r"[\x00-\x1f/:\\]+", " ", str(name or p.get("title") or p["name"])).strip(" .")[:80].strip()
    made = None
    if root is None:
        root = made = project.project_dir(p["name"]) / "exports" / f"capcut-{time.strftime('%Y%m%d-%H%M%S')}"
        root.mkdir(parents=True, exist_ok=True)
    root = Path(root)
    folder = _free(root, title or "EditVDO")
    folder.mkdir()
    try:
        placed, sources, size = {}, [], 0
        for sid in dict.fromkeys(s["sid"] for s in segs):
            src = Path(srcs[sid]["path"]).resolve()
            if src not in placed:
                size += src.stat().st_size
                placed[src] = src
                if copy_media:
                    (folder / "media").mkdir(exist_ok=True)
                    placed[src] = _free(folder / "media", src.stem, src.suffix)
                    place_media(src, placed[src])
            sources.append({"id": sid, "path": str(placed[src]), "name": srcs[sid].get("name") or src.name,
                            "duration": media_duration(src), "w": srcs[sid]["w"], "h": srcs[sid]["h"],
                            "has_audio": srcs[sid].get("has_audio", True)})
        captions = []
        if include_subs:
            for sub in render.edited_subtitles(p, segs):
                captions += [{"text": sub["text"], "f0": f0, "f1": f1, "x": sub["x"], "y": sub["y"]}
                             for f0, f1 in _join_short(sub["parts"], round(MIN_PIECE * fps))]
        info, meta, store = build_draft(folder, p["canvas"], sources, segs, captions, time.time())
        meta["draft_timeline_materials_size_"] = size
        layout = _seed("timeline_layout.json")
        for dock in layout["dockItems"]:
            dock.update(timelineIds=[info["id"]], timelineNames=[info["id"]])
        for file, data in (("draft_meta_info.json", meta), ("draft_virtual_store.json", store),
                           ("draft_agency_config.json", _seed("draft_agency_config.json")),
                           ("timeline_layout.json", layout), ("template-2.tmp", info), ("draft_info.json", info)):
            (folder / file).write_text(_dump(data), encoding="utf-8")
        if here:
            register(root, root_entry(meta, size))
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        if made:
            with suppress(OSError):
                made.rmdir()  # 이번에 만든 exports/capcut-<시각>/ 가 비었으면 지워요
        raise
    if fallback:  # folder 는 같은 폴더에, capcut 은 프로젝트 exports/ 에
        out = project.project_dir(p["name"]) / "exports" / f"capcut-{time.strftime('%Y%m%d-%H%M%S')}" if here else root
        export_clips(p, out, on_progress)
    return folder

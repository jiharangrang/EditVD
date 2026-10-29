#!/usr/bin/env python3
"""EditVDO 명령줄 — Claude 가 UI 와 같은 프로젝트를 고쳐요 (열려 있는 UI 는 1초 안에 새로 불러와요).
시간은 모두 원본 시간축(소스들을 순서대로 이은 축)이고, 줄마다 편집본 시간도 같이 보여 줘요.
시간 형식: SS · M:SS(.s) · H:MM:SS(.s). 바꾸는 명령은 모두 project.transact(by="cli") 로 저장해요."""
import argparse
import bisect
import json
import math
import sys

from editvdo import jobs, project as P

STATUS = {"queued": "대기", "analyzing": "분석 중", "error": "오류"}
GAP = 0.5  # 자막 없는 말을 줄로 묶을 때 이만큼(초) 쉬면 다음 줄
NO_POS = object()


# ---------- 시간·글 ----------

def fmt(t):
    """초 → MM:SS.ss (한 시간이 넘으면 H:MM:SS.ss)."""
    cs = int(round(max(float(t), 0.0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}" if h else f"{m:02d}:{s:02d}.{cs:02d}"


def tm(text):
    """SS · M:SS(.s) · H:MM:SS(.s) → 초."""
    parts = str(text).strip().split(":")
    try:
        if len(parts) > 3 or not all(parts):
            raise ValueError
        nums = [float(x) for x in parts]
        if not all(math.isfinite(x) and x >= 0 for x in nums) or any(x >= 60 for x in nums[1:]):
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError(f"시간 형식을 확인해 주세요: {text} (SS · M:SS · H:MM:SS)") from None
    t = 0.0
    for x in nums:
        t = t * 60 + x
    return t


def pos(text):
    """'x,y' (화면 %, 글 상자 가운데) · none → 기본 위치."""
    if str(text).strip().lower() in ("none", "null", "-"):
        return None
    try:
        x, y = (float(v) for v in str(text).split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("위치는 x,y (화면 %, 0~100) 로 적어 주세요") from None
    return {"x": x, "y": y}


def one_line(text, limit=None):
    s = " / ".join(x.strip() for x in str(text).split("\n") if x.strip())
    return s if limit is None or len(s) <= limit else s[:limit - 1] + "…"


def load(name):
    try:
        return P.load(name)
    except FileNotFoundError:
        if (P.project_dir(name) / "meta.json").exists():
            raise ValueError(f"v1 프로젝트예요. 먼저 옮겨 주세요: ev.py migrate {name}") from None
        raise


def words_of(p):
    """원본 시간축 음성 인식 단어 (분석 전이면 빈 목록)."""
    from editvdo import analyze
    return sorted(analyze.words(p), key=lambda w: w["start"])


def inside(words, a, b):
    """가운데가 [a, b] 안에 드는 단어."""
    i = bisect.bisect_left([w["start"] for w in words], a - 2.0)
    return [w for w in words[i:] if w["start"] <= b and a <= (w["start"] + w["end"]) / 2 <= b]


def said(words, limit=40):
    return one_line(" ".join(w["word"].strip() for w in words), limit)


def hidden(p, a, b):
    """[a, b] 가 통째로 잘린 구간 안인지."""
    return any(s <= a + 1e-6 and b <= e + 1e-6 for s, e in P.cut_intervals(p))


def times(p, a, b=None):
    """'원본 │ 편집본' 두 칸 (통째로 잘린 줄은 편집본 칸에 '잘림')."""
    if b is not None and hidden(p, a, b):
        return f"{fmt(a):>9} │ 잘림     "  # 한글 두 글자 = 네 칸
    return f"{fmt(a):>9} │ {fmt(P.to_edited(p, a)):<9}"


def head(p):
    """프로젝트 한 줄 요약 + 소스 줄."""
    cuts = p["cuts"]
    line = (f"{p['title']} ({p['name']}) · rev {p['rev']} · 원본 {fmt(P.total(p))} → 편집본 {fmt(P.edited_duration(p))}"
            f" · 컷 {len(cuts)} (자름 {sum(c['enabled'] for c in cuts)}, 미확인 {sum(not c['reviewed'] for c in cuts)})"
            f" · 자막 {len(p['subtitles'])}줄")
    srcs = []
    for s in p["sources"]:
        x = f"{s['id']} {s['name']} {fmt(s['offset'])}–{fmt(s['offset'] + s['duration'])}"
        if s["status"] == "analyzing":
            x += f" [분석 중 {int(s['progress'] * 100)}% {s['step']}]".replace(" ]", "]")
        elif s["status"] != "ready":
            x += f" [{STATUS.get(s['status'], s['status'])}{': ' + s['error'] if s['error'] else ''}]"
        srcs.append(x)
    return line + "\n소스: " + (" | ".join(srcs) or "없음")


def cut_row(p, c, words=None):
    """✂ [id 종류 on/off ✓확인] 원본 시작–끝 (길이) 이유 ‹잘리는 말›"""
    s = (f"✂ [{c['id']} {c['type']} {'on' if c['enabled'] else 'off'}{' ✓' if c['reviewed'] else ''}] "
         f"{fmt(c['start'])}–{fmt(c['end'])} ({c['end'] - c['start']:.2f}s)")
    if c.get("reason"):
        s += f" {c['reason']}"
    if words is not None and (w := inside(words, c["start"], c["end"])):
        s += f" ‹{said(w)}›"
    return s


def sub_row(p, t):
    """id 원본 시작–끝 화자 @위치 잠금 확인 「글」"""
    flags = [t["speaker"] or "-"]
    if t.get("pos"):
        flags.append(f"@{t['pos']['x']:g},{t['pos']['y']:g}")
    if t.get("locked"):
        flags.append("잠금")
    if t.get("check"):
        flags.append("확인")
    return f"{t['id']} {fmt(t['start'])}–{fmt(t['end'])} {' '.join(flags)} 「{one_line(t['text'])}」"


def word_list(words):
    return " · ".join(f"{w['word'].strip()} {w['start']:.2f}-{w['end']:.2f}" for w in words)


def show(frac=None, step=None, sid=None, _last={}):
    """진행 표시: 단계가 바뀌거나 20% 넘을 때마다 한 줄."""
    pct = int((frac or 0) * 100)
    key = (sid, step)
    if key != _last.get("key") or pct >= _last.get("pct", -100) + 20 or (pct == 100 and _last.get("pct") != 100):
        _last.update(key=key, pct=pct)
        print(f"  {pct:3d}% {sid or ''} {step or ''}".rstrip(), flush=True)


def save(name, ops):
    """연산을 저장하고 (p, changes) 를 돌려줘요."""
    return P.transact(name, lambda p: P.apply_ops(p, ops, "cli"), by="cli")


def need(p, coll, ids, label):
    have = {x["id"] for x in p[coll]}
    if missing := [i for i in ids if i not in have]:
        end = "이에요" if (ord(label[-1]) - 0xAC00) % 28 else "예요"  # 받침이 있으면 '이에요'
        raise ValueError(f"없는 {label}{end}: {', '.join(missing)}")


# ---------- 프로젝트·소스 ----------

def cmd_new(a):
    name = P.create(a.title, a.videos, name=a.name)
    p = P.load(name)
    print(f"프로젝트 {name} 을 만들었어요. 분석해요 (영상 {len(p['sources'])}개)", flush=True)
    jobs.run_analysis(name, [s["id"] for s in p["sources"]], transcribe=not a.no_transcribe, on_progress=show)
    print(head(P.load(name)))


def cmd_add(a):
    load(a.project)
    with jobs.analysis_lock(a.project):  # 더하고 분석하는 사이에 UI 서버가 '멈춘 분석'으로 보지 않게
        p, sids = P.transact(a.project, lambda p: P.add_sources(p, a.videos), by="cli")
        print(f"영상 {len(sids)}개를 더했어요 ({' '.join(sids)}). 분석해요", flush=True)
        jobs.run_analysis(a.project, sids, on_progress=show, lock=False)
    print(head(P.load(a.project)))


def _preview(name):
    if jobs.rebuild_preview(name, wait=False) is None:
        print("분석 중이라 미리보기는 분석이 끝날 때 다시 만들어져요")


def cmd_order(a):
    load(a.project)
    P.transact(a.project, lambda p: P.reorder_sources(p, a.sids), by="cli", snapshot="영상 순서")
    _preview(a.project)
    print(head(P.load(a.project)))


def cmd_remove(a):
    load(a.project)
    P.transact(a.project, lambda p: P.remove_source(p, a.sid), by="cli", snapshot="영상 빼기")
    _preview(a.project)
    print(head(P.load(a.project)))


def cmd_analyze(a):
    p = load(a.project)
    sids = [a.sid] if a.sid else [s["id"] for s in p["sources"]]
    need(p, "sources", sids, "영상")
    if a.clean:
        save(a.project, [{"op": "set", "key": "settings.asr", "value": {"clean": True}}])
    jobs.run_analysis(a.project, sids, transcribe=a.transcribe, on_progress=show)
    print(head(P.load(a.project)))


def cmd_migrate(a):
    done = P.migrate_v1(a.project)
    p = P.load(a.project)
    print(("v1 프로젝트를 옮겼어요 (v1 파일은 그대로)" if done else "이미 v2 프로젝트예요") + "\n" + head(p))
    d = P.project_dir(a.project)
    if todo := [s["id"] for s in p["sources"] if not (d / "sources" / s["id"] / "thumbs.jpg").exists()]:
        print("썸네일을 만들어요", flush=True)
        jobs.make_thumbs(a.project, todo, show)


# ---------- 읽기 ----------

def cmd_text(a):
    p = load(a.project)
    words = words_of(p)
    lo = a.start if a.start is not None else 0.0
    hi = a.end if a.end is not None else math.inf
    print(head(p))
    rows = [(c["start"], 0, f"{times(p, c['start'])}  {cut_row(p, c, words)}")
            for c in p["cuts"] if c["end"] >= lo and c["start"] <= hi]
    for t in p["subtitles"]:
        if t["end"] >= lo and t["start"] <= hi:
            row = f"{times(p, t['start'], t['end'])}  {sub_row(p, t)}"
            if a.words and (w := inside(words, t["start"], t["end"])):
                row += "\n" + " " * 23 + "└ " + word_list(w)
            rows.append((t["start"], 1, row))
    cover = sorted((t["start"] - 0.05, t["end"] + 0.05) for t in p["subtitles"])
    starts, ends = [x for x, _ in cover], list(_running_max(e for _, e in cover))
    loose, line = [], []
    for w in words:  # 자막이 덮지 않는 말은 쉼마다 줄로 묶어서 보여 줘요
        m = (w["start"] + w["end"]) / 2
        i = bisect.bisect_right(starts, m) - 1
        if i >= 0 and ends[i] >= m:
            continue
        if line and (w["start"] - line[-1]["end"] > GAP or w.get("sid") != line[-1].get("sid")):
            loose.append(line)
            line = []
        line.append(w)
    loose += [line] if line else []
    for ws in loose:
        a0, b0 = ws[0]["start"], ws[-1]["end"]
        if b0 >= lo and a0 <= hi:
            row = f"{times(p, a0, b0)}  · {said(ws, 200)} (자막 없음)"
            if a.words:
                row += "\n" + " " * 23 + "└ " + word_list(ws)
            rows.append((a0, 2, row))
    print("     원본 │ 편집본")
    for _, _, row in sorted(rows, key=lambda r: (r[0], r[1])):
        print(row)
    if not words:
        print("(음성 인식 단어가 없어요)")


def _running_max(xs):
    m = -math.inf
    for x in xs:
        m = max(m, x)
        yield m


def cmd_cuts(a):
    p = load(a.project)
    words = words_of(p)
    cuts = p["cuts"]
    on, todo = sum(c["enabled"] for c in cuts), sum(not c["reviewed"] for c in cuts)
    print(f"컷 {len(cuts)}개 · 자름 {on} · 살림 {len(cuts) - on} · 미확인 {todo} · 편집본 {fmt(P.edited_duration(p))}")
    for c in cuts:
        if not a.todo or not c["reviewed"]:
            print(f"{times(p, c['start'])}  {cut_row(p, c, words)}")


def cmd_subs(a):
    p = load(a.project)
    subs = p["subtitles"]
    print(f"자막 {len(subs)}줄 · 확인 {sum(t['check'] for t in subs)} · 잠금 {sum(t['locked'] for t in subs)}")
    for s in p["speakers"]:
        print(f"화자 {s['id']} {s['name']} {s['color']} @{s['pos']['x']:g},{s['pos']['y']:g}" if s.get("pos")
              else f"화자 {s['id']} {s['name']} {s['color']}")
    for t in subs:
        if not a.check or t["check"]:
            print(f"{times(p, t['start'], t['end'])}  {sub_row(p, t)}")


def cmd_comments(a):
    p = load(a.project)
    print(f"메모 {len(p['comments'])}개 · 할 일 {sum(not m['done'] for m in p['comments'])}")
    for m in p["comments"]:
        row = f"{times(p, m['t'])}  {m['id']} {'완료' if m['done'] else '할 일'} 「{one_line(m['text'])}」"
        print(row + (f" → 답: {one_line(m['reply'])}" if m["reply"] else ""))
    if p["notes"]:
        print(f"Claude 메모: {one_line(p['notes'])}")


def cmd_at(a):
    p = load(a.project)
    if a.time > P.edited_duration(p) + 1e-6:
        raise ValueError(f"편집본은 {fmt(P.edited_duration(p))} 까지예요")
    t = P.to_original(p, a.time)
    s, local = P.source_at(p, t)
    print(f"편집본 {fmt(a.time)} = 원본 {fmt(t)} ({s['id']} {s['name']} 안 {fmt(local)})")
    a.start, a.end, a.words = max(0.0, t - 6), t + 6, False
    cmd_text(a)


# ---------- 컷 ----------

def fine_silences(p):
    """소스별 silences.json 의 fine(0.15초 이상 쉼)을 원본 시간축으로."""
    d, out = P.project_dir(p["name"]), []
    for s in p["sources"]:
        try:
            data = json.loads((d / "sources" / s["id"] / "silences.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out += [(a + s["offset"], b + s["offset"]) for a, b in data.get("fine", [])]
    return sorted(out)


def snap(t, fine, side, pad_after, pad_before, reach=0.35):
    """경계 t 를 ±reach 안의 가장 가까운 쉼 안으로 옮겨요. 컷 시작은 앞말 뒤 여유, 컷 끝은 뒷말 앞 여유를 남겨요."""
    best = None
    for s, e in fine:
        if s - reach <= t <= e + reach:
            dist = 0 if s <= t <= e else min(abs(t - s), abs(t - e))
            if best is None or dist < best[0]:
                best = (dist, s, e)
    if best is None:
        return t
    _, s, e = best
    lo, hi = (s + pad_after, e) if side == "start" else (s, e - pad_before)
    return (s + e) / 2 if lo > hi else min(max(t, lo), hi)


def cmd_cut(a):
    p = load(a.project)
    fine, st = fine_silences(p), p["settings"]["cut"]
    s1 = snap(a.start, fine, "start", st["pad_after"], st["pad_before"])
    e1 = snap(a.end, fine, "end", st["pad_after"], st["pad_before"])
    if e1 - s1 < 0.05:
        s1, e1 = a.start, a.end
    item = {"start": round(s1, 3), "end": round(e1, 3), "type": a.type, "reason": a.reason,
            "enabled": not a.off, "reviewed": False}
    p, ch = save(a.project, [{"op": "put", "coll": "cuts", "item": item}])
    c = next(x["item"] for x in ch if x.get("coll") == "cuts")
    print(f"추가 {times(p, c['start'])}  {cut_row(p, c)}")
    if (round(s1, 2), round(e1, 2)) != (round(a.start, 2), round(a.end, 2)):
        print(f"  경계를 쉼에 맞췄어요: {fmt(a.start)}–{fmt(a.end)} → {fmt(c['start'])}–{fmt(c['end'])}")
    print(f"편집본 {fmt(P.edited_duration(p))}")


def cmd_toggle(a):
    on = a.cmd == "on"

    def fn(p):
        need(p, "cuts", a.ids, "컷")
        return P.apply_ops(p, [{"op": "put", "coll": "cuts", "item": {"id": i, "enabled": on, "reviewed": False}}
                               for i in a.ids], "cli")
    p, ch = P.transact(a.project, fn, by="cli")
    for x in ch:
        if x.get("coll") == "cuts":
            print(f"{times(p, x['item']['start'])}  {cut_row(p, x['item'])}")
    print(f"편집본 {fmt(P.edited_duration(p))} · 사용자에게 미확인으로 보여요")


def cmd_drop(a):
    def fn(p):
        need(p, "cuts", a.ids, "컷")
        return P.apply_ops(p, [{"op": "del", "coll": "cuts", "id": i} for i in a.ids], "cli")
    p, _ = P.transact(a.project, fn, by="cli")
    print(f"컷 {len(a.ids)}개를 지웠어요 · 편집본 {fmt(P.edited_duration(p))}")


def cmd_detect(a):
    settings = {k: v for k, v in (("min_silence", a.min_silence), ("pad_before", a.pad_before),
                                  ("pad_after", a.pad_after), ("long_silence", a.long), ("fillers", a.fillers))
                if v is not None}
    p, ch = jobs.detect(a.project, "cli", settings or None)
    cuts = p["cuts"]
    changed = sum(1 for x in ch if x.get("coll") == "cuts" and not x.get("deleted"))
    gone = sum(1 for x in ch if x.get("coll") == "cuts" and x.get("deleted"))
    print(f"자동 컷을 다시 찾았어요 (이전 상태는 history/): 컷 {len(cuts)}개 · 자름 {sum(c['enabled'] for c in cuts)}"
          f" · 미확인 {sum(not c['reviewed'] for c in cuts)} · 새로/바뀜 {changed} · 없어짐 {gone}"
          f" · 편집본 {fmt(P.edited_duration(p))}")
    print("설정: " + " ".join(f"{k}={v}" for k, v in p["settings"]["cut"].items()))


# ---------- 자막 ----------

def cmd_sub(a):
    item = {"id": a.id}
    if a.text is not None:
        item["text"] = a.text.replace("\\n", "\n")
    if a.start is not None:
        item["start"] = round(a.start, 3)
    if a.end is not None:
        item["end"] = round(a.end, 3)
    if a.speaker is not None:
        item["speaker"] = None if a.speaker.lower() in ("none", "-") else a.speaker.upper()
    if a.pos is not NO_POS:
        item["pos"] = a.pos
    if a.unlock:
        item["locked"] = False
    if a.check is not None:
        item["check"] = a.check

    def fn(p):
        need(p, "subtitles", [a.id], "자막")
        return P.apply_ops(p, [{"op": "put", "coll": "subtitles", "item": item}], "cli") if len(item) > 1 else []
    p, ch = P.transact(a.project, fn, by="cli")
    t = next(x for x in p["subtitles"] if x["id"] == a.id)
    print(f"{times(p, t['start'], t['end'])}  {sub_row(p, t)}")
    if others := sum(1 for x in ch if x.get("coll") == "subtitles" and x["item"]["id"] != a.id):
        print(f"  다른 자막 {others}줄 타이밍도 다시 맞췄어요")


def cmd_subgen(a):
    p = load(a.project)
    if missing := jobs.missing_words(p):
        print(f"음성 인식부터 해요: {' '.join(missing)}", flush=True)
        jobs.run_analysis(a.project, missing, on_progress=show, words_only=True)
    p, _ = jobs.build_subs(a.project, "cli")
    print(f"자막 {len(p['subtitles'])}줄을 만들었어요 (이전 자막은 history/)")


def cmd_import(a):
    load(a.project)
    p, _, info = jobs.import_subs(a.project, jobs.read_text(a.file), "cli", a.base, True)
    info = info or {}
    print(f"자막 {len(p['subtitles'])}줄 · 불러온 줄 {info.get('count', '?')} · 시간 기준 {info.get('base', '?')}"
          f" · 말소리에 못 맞춘 줄 {info.get('unmatched', '?')} (확인 표시) · 이전 자막은 history/")


def cmd_timing(a):
    vals = {k: v for k, v in (("lead", a.lead), ("hold", a.hold), ("min", a.min), ("bridge", a.bridge),
                              ("max_chars", a.max_chars)) if v is not None}
    if vals:
        p, ch = save(a.project, [{"op": "set", "key": "settings.subs", "value": vals}])
        print(f"자막 {sum(1 for x in ch if x.get('coll') == 'subtitles')}줄 타이밍을 다시 맞췄어요")
        if "max_chars" in vals:
            print("한 줄 글자 수는 자막을 새로 나눌 때 써요 (ev.py subgen)")
    else:
        p = load(a.project)
    print("자막 타이밍: " + " ".join(f"{k}={v}" for k, v in p["settings"]["subs"].items()))


def cmd_speaker(a):
    item = {"id": a.id.upper()}
    if a.name is not None:
        item["name"] = a.name
    if a.pos is not NO_POS:
        item["pos"] = a.pos
    if a.color is not None:
        item["color"] = a.color
    p, ch = save(a.project, [{"op": "put", "coll": "speakers", "item": item}])
    s = next(x for x in p["speakers"] if x["id"] == item["id"])
    where = f" @{s['pos']['x']:g},{s['pos']['y']:g}" if s.get("pos") else ""
    print(f"화자 {s['id']} {s['name']} {s['color']}{where} · 자막 {sum(t['speaker'] == s['id'] for t in p['subtitles'])}줄")


# ---------- 메모·내보내기 ----------

def cmd_reply(a):
    item = {"id": a.id, "reply": a.text}
    if a.done:
        item["done"] = True

    def fn(p):
        need(p, "comments", [a.id], "메모")
        return P.apply_ops(p, [{"op": "put", "coll": "comments", "item": item}], "cli")
    p, _ = P.transact(a.project, fn, by="cli")
    m = next(x for x in p["comments"] if x["id"] == a.id)
    print(f"{times(p, m['t'])}  {m['id']} {'완료' if m['done'] else '할 일'} 「{one_line(m['text'])}」 → 답: {one_line(m['reply'])}")


def cmd_note(a):
    load(a.project)
    save(a.project, [{"op": "set", "key": "notes", "value": a.text}])
    print("메모를 남겼어요 (UI 메모 탭 위쪽에 보여요)")


def cmd_export(a):
    load(a.project)
    res = jobs.run_export(a.project, a.kind, title=a.name, burn=not a.no_burn, include_subs=not a.no_subs,
                          copy_media=not a.no_media, fallback=a.fallback, on_progress=show,
                          target="capcut" if a.here else "folder")
    print(f"내보냈어요: {res['path']}")


def cmd_serve(a):
    from editvdo import server
    try:
        server.serve(a.port)
    except OSError as e:
        raise ValueError(f"포트 {a.port} 를 열 수 없어요 ({e.strerror}). --port 8766 처럼 다른 포트를 써 보세요") from None


# ---------- 명령 ----------

def parser():
    ap = argparse.ArgumentParser(prog="ev.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="명령")

    def cmd(name, fn, help_, project=True):
        s = sub.add_parser(name, help=help_, description=help_)
        if project:
            s.add_argument("project", help="프로젝트 이름 (projects/<이름>)")
        s.set_defaults(fn=fn)
        return s

    s = cmd("new", cmd_new, "새 프로젝트를 만들고 분석해요 (끝날 때까지 기다려요)", project=False)
    s.add_argument("title")
    s.add_argument("videos", nargs="+")
    s.add_argument("--name", help="폴더 이름 (영문·숫자·한글·_·-)")
    s.add_argument("--no-transcribe", action="store_true", help="음성 인식은 건너뛰어요")
    s = cmd("add", cmd_add, "영상을 뒤에 더하고 분석해요")
    s.add_argument("videos", nargs="+")
    s = cmd("order", cmd_order, "영상 순서를 바꿔요 (컷·자막·메모가 같이 움직여요)")
    s.add_argument("sids", nargs="+")
    s = cmd("remove", cmd_remove, "영상 하나를 빼요")
    s.add_argument("sid")
    s = cmd("analyze", cmd_analyze, "다시 분석해요 (기본: 음성 인식은 그대로 두고)")
    s.add_argument("--sid")
    s.add_argument("--transcribe", action="store_true", help="음성 인식도 다시 해요")
    s.add_argument("--clean", action="store_true", help="시끄러운 곳: 목소리만 분리해서 분석 (demucs)")
    s = cmd("text", cmd_text, "대본: 말·자막·컷을 시간순으로 (원본 │ 편집본)")
    s.add_argument("--words", action="store_true", help="단어별 시간도 보여요")
    s.add_argument("--from", dest="start", type=tm)
    s.add_argument("--to", dest="end", type=tm)
    s = cmd("cuts", cmd_cuts, "컷 목록")
    s.add_argument("--todo", action="store_true", help="미확인 컷만")
    s = cmd("cut", cmd_cut, "컷을 더해요 (경계는 ±0.35초 안의 쉼에 맞춰요)")
    s.add_argument("start", type=tm)
    s.add_argument("end", type=tm)
    s.add_argument("reason", help="이유 (20자 안팎)")
    s.add_argument("--type", default="ng", choices=("ng", "retake", "manual"))
    s.add_argument("--off", action="store_true", help="자르지 않고 제안만 (애매할 때)")
    for name, help_ in (("on", "컷을 켜요 (자름, 사용자에게 미확인으로 보여요)"), ("off", "컷을 꺼요 (살림, 미확인으로 보여요)")):
        s = cmd(name, cmd_toggle, help_)
        s.add_argument("ids", nargs="+")
    s = cmd("drop", cmd_drop, "컷을 지워요")
    s.add_argument("ids", nargs="+")
    s = cmd("detect", cmd_detect, "자동 컷을 다시 찾아요 (확인한 결정은 이어받아요)")
    s.add_argument("--min-silence", type=float)
    s.add_argument("--pad-before", type=float)
    s.add_argument("--pad-after", type=float)
    s.add_argument("--long", type=float, help="이 길이(초) 이상 쉼은 long")
    s.add_argument("--fillers", action=argparse.BooleanOptionalAction, help="필러(음·어) 컷을 켤지")
    s = cmd("subs", cmd_subs, "자막 목록")
    s.add_argument("--check", action="store_true", help="확인 표시된 줄만")
    s = cmd("sub", cmd_sub, "자막 한 줄 고치기 (시간을 주면 그 줄은 잠겨요)")
    s.add_argument("id")
    s.add_argument("--text", help="새 글 (줄바꿈은 \\n)")
    s.add_argument("--start", type=tm)
    s.add_argument("--end", type=tm)
    s.add_argument("--speaker", help="A~Z, none")
    s.add_argument("--pos", type=pos, default=NO_POS, help="x,y (화면 %%) 또는 none")
    s.add_argument("--unlock", action="store_true", help="자동 타이밍으로 되돌려요")
    s.add_argument("--check", action=argparse.BooleanOptionalAction, help="확인 표시 (주황 점)")
    cmd("subgen", cmd_subgen, "음성 인식으로 자막을 새로 만들어요")
    s = cmd("import", cmd_import, "대본(SRT·VTT·시간+글·클로바노트·글만)을 자막으로")
    s.add_argument("file")
    s.add_argument("--base", default="auto", choices=("auto", "orig", "edit"), help="대본 시간이 원본/편집본 기준인지")
    s = cmd("timing", cmd_timing, "자막 타이밍 설정 (주면 바꾸고 다시 맞춰요)")
    s.add_argument("--lead", type=float, help="말 앞 여유(초)")
    s.add_argument("--hold", type=float, help="말 끝난 뒤 더 보이기(초)")
    s.add_argument("--min", type=float, help="최소 길이(초)")
    s.add_argument("--bridge", type=float, help="이보다 짧은 틈은 이어 붙이기(초)")
    s.add_argument("--max-chars", type=int, help="한 줄 글자 수 (subgen 때 씀)")
    s = cmd("speaker", cmd_speaker, "화자 이름·위치·색")
    s.add_argument("id", help="A~Z")
    s.add_argument("--name")
    s.add_argument("--pos", type=pos, default=NO_POS, help="x,y (화면 %%)")
    s.add_argument("--color", help="#RRGGBB")
    cmd("comments", cmd_comments, "사용자 메모(코멘트) 목록")
    s = cmd("reply", cmd_reply, "메모에 답하기")
    s.add_argument("id")
    s.add_argument("text")
    s.add_argument("--done", action="store_true", help="완료로 표시")
    s = cmd("note", cmd_note, "Claude 메모 (UI 메모 탭 위쪽)")
    s.add_argument("text")
    s = cmd("at", cmd_at, "편집본 시간 → 원본 시간 (+ 앞뒤 6초 대본)")
    s.add_argument("time", type=tm)
    s = cmd("export", cmd_export, "내보내기 (캡컷 프로젝트 · MP4 · SRT)")
    s.add_argument("kind", choices=("capcut", "mp4", "srt"))
    s.add_argument("--name", help="캡컷 프로젝트 이름")
    s.add_argument("--here", action="store_true",
                   help="이 맥 캡컷에 바로 넣어요 (캡컷을 닫고). 없으면 exports/ 에 다른 맥으로 옮길 폴더를 만들어요")
    s.add_argument("--no-media", action="store_true", help="--here 일 때 캡컷 폴더에 원본을 담지 않아요")
    s.add_argument("--no-subs", action="store_true", help="캡컷에 자막을 넣지 않아요")
    s.add_argument("--no-burn", action="store_true", help="MP4 에 자막을 입히지 않고 자막 트랙으로")
    s.add_argument("--fallback", action="store_true", help="캡컷용 클립·SRT 대비책도 exports/ 에")
    cmd("migrate", cmd_migrate, "v1 프로젝트를 v2 로 옮겨요")
    s = cmd("serve", cmd_serve, "편집 화면 서버", project=False)
    s.add_argument("--port", type=int, default=8765)
    return ap


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    a = parser().parse_args(argv)
    try:
        if getattr(a, "project", None) and a.fn is not cmd_migrate:
            load(a.project)  # 없는 프로젝트·v1 프로젝트는 여기서 알려요
        a.fn(a)
    except (ValueError, OSError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("중단했어요", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

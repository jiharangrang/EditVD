"""자막: 단어로 줄 만들기, 자동 타이밍(retime), 대본 읽기(SRT·VTT·시간표·클로바노트), 대본↔음성 맞추기, 나누기·합치기.
시간은 모두 원본 시간축이에요. 자동 타이밍만 편집본 시간축에서 계산해요."""
import bisect
import copy
import html
import itertools
import math
import re
import statistics
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher

from . import project

CHAR_SEC = 0.17        # 한국어 말 속도: 글자당 약 0.17초
PAUSE = 0.5            # 이보다 긴 쉼에서 줄을 나눠요
MAX_LINE_SEC = 4.5     # 한 줄이 이보다 길어지면 나눠요
NO_WORDS_CAP = 6.0     # 인식 단어가 없을 때 끝 시간이 없는 줄의 최대 길이
EPS = 0.0005
SENT_END = re.compile(r"[.?!。？！…][\"'’”)\]]*$")
TRAIL = re.compile(r"[\s.,…。、，]+$")
TIME = r"(?:\d{1,2}:)?\d{1,3}:\d{2}(?:[.,]\d{1,3})?"
CUE = re.compile(rf"^\s*({TIME})\s*-->\s*({TIME})(?:\s+\S+:\S+)*\s*$")
LINE = re.compile(rf"""^(?P<lead>\s*(?:[-*•·]\s*)?)[\[(]?\s*(?P<t0>{TIME})(?![\d:])\s*
    (?:(?:~|〜|–|—|-->|->|-)\s*(?P<t1>{TIME})(?![\d:])\s*)?[\])]?(?:\s*[|:–—-]\s+|\s+|$)(?P<rest>.*)$""", re.X)
HEAD = re.compile(rf"^\s*(?P<name>[^\W\d_][\w .·-]{{0,24}}?)\s+(?P<t>{TIME})\s*$")
EXPLICIT = re.compile(r"(?:화자|참석자|발화자|speaker|spk)\s*\d{1,2}", re.I)
PREFIX = re.compile(r"^\s*(?:\[(?P<b>[^\]\n]{1,20})\]\s*(?P<bc>:)?|(?P<n>[^\s:\[\]()]{1,12}(?:\s\d{1,2})?)\s*:)\s*(?=\S)")
VOICE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]+)>")
TAGS = re.compile(r"<[^>]*>|\{\\[^}]*\}")


def _settings(p):
    return {**project.DEFAULT_SETTINGS["subs"], **((p.get("settings") or {}).get("subs") or {})}


def _keys(s):
    """맞추기용 글자: 띄어쓰기·문장부호를 빼고 소문자로."""
    return [c for c in unicodedata.normalize("NFC", s).lower() if c.isalnum()]


def _len(s):
    return len(re.sub(r"\s", "", s))


def _clean(text):
    return TRAIL.sub("", text.strip())


def _fresh(p, used=None):
    used = used if used is not None else {t["id"] for t in p["subtitles"]}
    while (iid := project.new_id("t")) in used:
        pass
    used.add(iid)
    return iid


def _sid_of(p, x):
    return project.source_at(p, (x["start"] + x["end"]) / 2)[0]["id"] if p["sources"] else None


# ---------- 줄 나누기 ----------

def _group(toks, max_chars, cuts=()):
    """단어 목록을 자막 줄로 묶어요: 쉼(0.5초)·소스/발화/화자 경계·강제 줄바꿈·1초 넘는 컷·문장 끝·
    글자 수(띄어쓰기 뺀 max_chars, 2/3 넘으면 쉼표에서 먼저)·한 줄 4.5초."""
    ends = [b for _, b in cuts]
    lines, cur, n = [], [], 0
    for i, w in enumerate(toks):
        cur.append(w)
        n += _len(w["word"])
        nx = toks[i + 1] if i + 1 < len(toks) else None
        if nx is None or _brk(cur, n, w, nx, max_chars, cuts, ends):
            lines.append(cur)
            cur, n = [], 0
    return lines


def _brk(cur, n, w, nx, max_chars, cuts, ends):
    if w.get("nl") or nx["start"] - w["end"] > PAUSE:
        return True
    if any(nx.get(k) != w.get(k) for k in ("sid", "u", "spk")):
        return True
    if SENT_END.search(w["word"]):
        return True
    if n >= max_chars * 2 / 3 and w["word"].endswith((",", "，")):
        return True
    if n + _len(nx["word"]) > max_chars and n * 2 >= max_chars:
        return True
    if nx["end"] - cur[0]["start"] > MAX_LINE_SEC:
        return True
    a, b = (w["start"] + w["end"]) / 2, (nx["start"] + nx["end"]) / 2
    i = bisect.bisect_right(ends, a)
    return i < len(cuts) and cuts[i][0] < b


def _sub(p, text, a, b, rev, used, by="auto", speaker=None, check=False):
    """새 자막 한 줄 (start/end 는 임시로 말소리 구간, retime 이 정해요)."""
    tot = project.total(p)
    a = round(min(max(a, 0.0), tot), 3)
    b = round(min(max(b, a), tot), 3)
    s, e = a, round(min(max(b, a + 0.1), tot), 3)
    if e - s < 0.001:
        s = round(max(0.0, e - 0.1), 3)
    return {"id": _fresh(p, used), "start": s, "end": e, "text": text[:2000], "speech": [a, b], "speaker": speaker,
            "pos": None, "locked": False, "check": bool(check), "by": by, "rev": rev}


def build_lines(words, p, sids=None):
    """단어(원본 시간, sid 포함) → 자막 줄 목록 (아직 p 에 넣지 않아요). speech = [첫 단어 시작, 마지막 단어 끝]."""
    want = None if sids is None else set(sids)
    ws = sorted((w for w in words if (want is None or w.get("sid") in want) and str(w.get("word", "")).strip()),
                key=lambda w: (w["start"], w["end"]))
    cuts = [(a, b) for a, b in project.cut_intervals(p) if b - a > 1.0]
    rev, used, out = p.get("rev", 0) + 1, {t["id"] for t in p["subtitles"]}, []
    for line in _group(ws, int(_settings(p)["max_chars"]), cuts):
        text = _clean(" ".join(str(w["word"]).strip() for w in line))
        if any(c.isalnum() for c in text):
            out.append(_sub(p, text, line[0]["start"], max(w["end"] for w in line), rev, used,
                            check=any(w.get("guess") for w in line)))
    return out


def generate(p, sids=None):
    """인식 단어로 자막을 새로 만들어요: sids 소스(기본 전체)의 자막을 지우고 새 줄을 넣은 뒤 타이밍을 맞춰요.
    새 자막 목록을 돌려줘요."""
    from . import analyze
    want = None if sids is None else set(sids)
    keep = [] if want is None else [t for t in p["subtitles"] if _sid_of(p, t) not in want]
    p["subtitles"] = keep
    new = build_lines(analyze.words(p), p, sids)
    p["subtitles"] = sorted(keep + new, key=lambda t: (t["start"], t["end"]))
    retime(p)
    return new


# ---------- 자동 타이밍 ----------

class _Axis:
    """원본↔편집본 변환 (project.to_edited / to_original 과 같은 규칙, 한 번만 계산)."""

    def __init__(self, p):
        self.segs = project.keep_segments(p)
        self.starts = [a for a, _ in self.segs]
        self.cum = list(itertools.accumulate((b - a for a, b in self.segs), initial=0.0))
        self.D = self.cum[-1]
        self.cuts = project.cut_intervals(p)
        self.cut_starts = [a for a, _ in self.cuts]

    def E(self, t):
        i = bisect.bisect_right(self.starts, t) - 1
        if i < 0:
            return 0.0
        a, b = self.segs[i]
        return self.cum[i] + (min(t, b) - a)

    def O(self, e):
        if not self.segs:
            return 0.0
        i = min(max(bisect.bisect_right(self.cum, e) - 1, 0), len(self.segs) - 1)
        a, b = self.segs[i]
        return min(a + max(e - self.cum[i], 0.0), b)

    def seg_start(self, t):
        """t 를 담은 남는 조각의 편집본 시작 (t 가 잘린 곳이면 E(t))."""
        i = bisect.bisect_right(self.starts, t) - 1
        return self.cum[i] if i >= 0 and t < self.segs[i][1] else self.E(t)

    def hidden(self, a, b):
        """[a, b] 가 통째로 한 컷 안에 있는지."""
        i = bisect.bisect_right(self.cut_starts, a + EPS) - 1
        return i >= 0 and self.cuts[i][1] >= b - EPS


def retime(p):
    """잠기지 않은 자막의 start/end 를 말소리(speech)와 settings.subs 로 다시 계산해요 (편집본 시간축).
    화자별로(화자 없음도 한 무리) 말소리 시작 순서로: s = E(speech0) - lead (말이 든 조각 시작보다 앞으로는 안 감),
    e = max(E(speech1) + hold, s + min), 다음 줄 시작 ns 와 bridge 보다 가까우면 e = ns, e ≤ ns, s ≥ 앞 줄 e.
    말소리가 통째로 컷 안인 줄(숨은 줄)은 그대로 둬요. 잠긴 줄은 그대로 두되 같은 화자 줄이 겹치지 않게 막아요.
    같은 입력(speech·설정·컷·화자)이면 언제나 같은 결과예요. 바뀐 자막 id 목록을 돌려줘요."""
    st = _settings(p)
    lead, hold, mn, bridge = (float(st[k]) for k in ("lead", "hold", "min", "bridge"))
    ax = _Axis(p)
    groups = defaultdict(list)
    for t in p["subtitles"]:
        groups[t.get("speaker")].append(t)
    changed = []
    for items in groups.values():
        rows = []
        for t in items:
            if t.get("locked"):
                if not ax.hidden(t["start"], t["end"]):
                    rows.append((ax.E(t["start"]), 1, -t["end"], t["id"], t))
                continue
            a, b = t.get("speech") or (t["start"], t["end"])
            if not ax.hidden(a, b):
                rows.append((max(ax.E(a) - lead, ax.seg_start(a)), 0, -b, t["id"], t))
        rows.sort(key=lambda r: r[:4])  # 같은 시작이면 말이 긴 줄 먼저
        prev = -math.inf
        for i, (s, locked, _, _, t) in enumerate(rows):
            if locked:
                prev = max(prev, ax.E(t["end"]))
                continue
            s = max(s, prev)
            e = max(ax.E((t.get("speech") or (t["start"], t["end"]))[1]) + hold, s + mn)
            if i + 1 < len(rows) and rows[i + 1][0] > s:
                ns = rows[i + 1][0]
                if ns - e < bridge:
                    e = ns
                e = min(e, ns)
            e = min(e, ax.D)
            prev = max(prev, e)
            a, b = round(ax.O(s), 3), round(ax.O(e), 3)
            if b - a < 0.001:
                continue  # 길이가 없어지는 줄은 손대지 않아요
            if (a, b) != (t["start"], t["end"]):
                t["start"], t["end"] = a, b
                changed.append(t["id"])
    return changed


# ---------- 대본 읽기 ----------

def _sec(s):
    """M:SS, MM:SS, H:MM:SS (+ .ms / ,ms) → 초. 말이 안 되는 시간이면 None."""
    parts = s.replace(",", ".").split(":")
    try:
        sec, mins = float(parts[-1]), int(parts[-2])
        hrs = int(parts[-3]) if len(parts) == 3 else 0
    except (ValueError, IndexError):
        return None
    if sec >= 60 or (len(parts) == 3 and mins >= 60):
        return None
    return round(hrs * 3600 + mins * 60 + sec, 3)


def parse_script(text):
    """대본 글을 너그럽게 읽어요: [{text, start?, end?, speaker?}] (시간은 초).
    SRT·WebVTT(자막 블록, cue=True), 시간표 줄(`- 00:12 [화자]: 말`, `[00:12 ~ 00:15] 말`, `1:02:03.5 말`),
    클로바노트(`참석자 1 00:12` 다음 줄들이 말), 하위 줄들이 시간을 가진 구간 머리줄(`[00:06 ~ 03:00] 제목`)은 빼요.
    시간 줄 다음의 시간 없는 줄은 앞 줄에 이어 붙이고, 시간이 하나도 없으면 줄마다 시간 없는 항목이에요.
    쓸 줄이 하나도 없을 때만 ValueError."""
    if not isinstance(text, str):
        raise ValueError("대본은 글자여야 해요")
    text = unicodedata.normalize("NFC", text.lstrip("\ufeff")).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    out = _parse_cues(lines) if any(CUE.match(x) for x in lines) else _parse_lines(lines)
    out = [e for e in _speakers(out) if e["text"]]
    if not out:
        raise ValueError("대본에서 자막으로 쓸 줄을 찾지 못했어요")
    return out


def _parse_cues(lines):
    out, cur = [], None
    for x in lines:
        m = CUE.match(x)
        if m:
            if cur and cur["lines"] and re.fullmatch(r"\d+", cur["lines"][-1]):
                cur["lines"].pop()  # 빈 줄 없이 붙은 다음 번호
            t0, t1 = _sec(m[1]), _sec(m[2])
            cur = None
            if t0 is not None and t1 is not None:
                cur = {"start": t0, "lines": [], "cue": True, **({"end": t1} if t1 > t0 else {})}
                out.append(cur)
            continue
        if not x.strip():
            cur = None
        elif cur is not None:
            cur["lines"].append(x.strip())
    for e in out:
        speaker = None
        texts = []
        for x in e.pop("lines"):
            if (v := VOICE.search(x)) and speaker is None:
                speaker = v.group(1).strip()
            x = TAGS.sub("", html.unescape(x)).strip()
            if x:
                texts.append(x)
        e["text"] = "\n".join(texts)
        if speaker:
            e["speaker"] = speaker
    return out


def _parse_lines(lines):
    heads = [m for x in lines if not LINE.match(x) and (m := HEAD.match(x))]
    clova = len(heads) >= 2 or any(EXPLICIT.fullmatch(m["name"].strip()) for m in heads)
    rows, plain, cur = [], [], None
    for x in lines:
        s = x.strip()
        if not s:
            continue
        m = LINE.match(x)
        t0 = _sec(m["t0"]) if m else None
        if t0 is not None:
            t1 = _sec(m["t1"]) if m["t1"] else None
            cur = {"start": t0, "end": t1 if t1 is not None and t1 > t0 else None, "ranged": bool(m["t1"]),
                   "lines": [m["rest"].strip()] if m["rest"].strip() else [], "indent": bool(m["lead"])}
            rows.append(cur)
            continue
        h = HEAD.match(x) if clova else None
        if h and _sec(h["t"]) is not None:
            cur = {"start": _sec(h["t"]), "end": None, "ranged": False, "lines": [], "indent": False,
                   "speaker": h["name"].strip()}
            rows.append(cur)
        elif cur is not None:
            cur["lines"].append(s)
        elif not rows:
            plain.append(s)
    if not rows:
        return [{"text": s} for s in plain]
    for k, r in enumerate(rows):  # 구간 머리줄: 범위 안에 시간 줄이 둘 이상이거나, 들여 쓴 시간 줄이 바로 안에 있으면
        if not r["ranged"] or r["end"] is None:
            continue
        inside = [f for f in itertools.takewhile(lambda f: f["start"] < r["end"], rows[k + 1:])
                  if f["start"] >= r["start"] - 1]
        if len(inside) >= 2 or (inside and inside[0] is rows[k + 1] and rows[k + 1]["indent"] and not r["indent"]):
            r["header"] = True
    return [{"text": "\n".join(r["lines"]), "start": r["start"], **({"end": r["end"]} if r["end"] is not None else {}),
             **({"speaker": r["speaker"]} if r.get("speaker") else {})} for r in rows if not r.get("header")]


def _speakers(entries):
    """글 앞의 화자 표시(`화자1:`, `[화자1]`, `[남]:`, `참석자 1:`, `민수:`)를 떼어 speaker 로 옮겨요.
    `[이름]:` 과 '화자/참석자 N' 은 언제나, 쌍점 없는 `[이름]` 은 '화자 N' 꼴일 때만 (`[웃음]` 같은 효과음 표시는 그대로),
    그 밖의 `이름:` 은 파일에 두 번 이상 나올 때만 화자로 봐요."""
    found = []
    for e in entries:
        m = None if e.get("speaker") else PREFIX.match(e["text"])
        lab = None
        if m:
            lab = (m["b"] or m["n"] or "").strip()
            sure = bool(m["bc"]) or bool(EXPLICIT.fullmatch(lab))
            lab = (lab, sure, m.end()) if lab and not lab.isdigit() and (sure or m["n"]) else None
        found.append(lab)
    count = defaultdict(int)
    for f in found:
        if f:
            count[f[0]] += 1
    out = []
    for e, f in zip(entries, found):
        e = dict(e)
        if f and (f[1] or count[f[0]] >= 2):
            e["speaker"], e["text"] = f[0], e["text"][f[2]:]
        e["text"] = e["text"].strip()
        out.append(e)
    return out


# ---------- 대본 ↔ 음성 맞추기 ----------

def _need(n):
    """이 정도 글자가 맞아야 '맞은' 줄로 봐요 (v1 기준)."""
    return max(min(2, n), n // 6)


def _whisper_chars(ws):
    W, T = [], []
    for w in ws:
        ks = _keys(str(w["word"]))
        d = max(w["end"] - w["start"], 0.01)
        for j, c in enumerate(ks):
            W.append(c)
            T.append((w["start"] + d * j / len(ks), w["start"] + d * (j + 1) / len(ks)))
    return W, T


def _blocks(a, b):
    """글자 단위 SequenceMatcher 일치 블록 [(i, j, n)]. 아주 길면 앞에서부터 조각씩 맞춰요."""
    if len(a) * len(b) <= 1.5e8:
        return [(m.a, m.b, m.size) for m in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks() if m.size]
    out, cur, step = [], 0, 3000
    ratio = len(b) / max(len(a), 1)
    for i in range(0, len(a), step):
        hi = min(len(b), max(cur + int(step * ratio) + 500, int((i + 1.5 * step) * ratio) + 500))
        for m in SequenceMatcher(None, a[i:i + step], b[cur:hi], autojunk=False).get_matching_blocks():
            if m.size:
                out.append((i + m.a, cur + m.b, m.size))
        sure = [x for x in out if x[2] >= 4]
        if sure:
            cur = max(cur, sure[-1][1] + sure[-1][2])
    return out


def _ranges(texts):
    """항목별 맞추기용 글자 범위 [(i0, i1)] 와 이어 붙인 글자."""
    S, rng = [], []
    for x in texts:
        ks = _keys(x)
        rng.append((len(S), len(S) + len(ks)))
        S += ks
    return rng, S


def _owners(rng):
    return [k for k, (i0, i1) in enumerate(rng) for _ in range(i0, i1)]


def _align(texts, ws):
    """대본 항목 글자 ↔ 인식 글자. (항목별 글자 범위 [(i0, i1)], 기준점 {대본 글자 위치: (t0, t1)})."""
    rng, S = _ranges(texts)
    W, T = _whisper_chars(ws)
    owner = _owners(rng)
    anchors = {}
    for i, j, n in _blocks("".join(S), "".join(W)):
        if n == 1 and rng[owner[i]][1] - rng[owner[i]][0] > 2:
            continue  # 한 글자 우연 일치는 버려요 (아주 짧은 줄은 예외)
        for k in range(n):
            anchors[i + k] = T[j + k]
    clean, last = {}, -math.inf
    for i in sorted(anchors):  # 시간이 뒤로 가는 기준점은 버려요
        if anchors[i][0] >= last - 0.05:
            clean[i] = anchors[i]
            last = max(last, anchors[i][0])
    return rng, clean


def _char_times(rng, anchors, ok, starts, ends, tot, fill=False):
    """대본 글자마다 (t0, t1) 원본 시간. 맞은 항목은 기준점 사이를 고르게·앞뒤는 글자당 CHAR_SEC,
    안 맞은 항목은 대본 시간(있으면)이나 앞뒤 맞은 글자 사이에 차례로 놓아요.
    fill=True (인식 단어 없음): 대본 시간 줄은 끝 시간까지, 없으면 다음 줄 시작까지(최대 6초) 채워요."""
    n = rng[-1][1] if rng else 0
    T = [None] * n
    owner = _owners(rng)
    for i, v in anchors.items():
        if ok[owner[i]]:
            T[i] = v
    known = sorted(i for i in range(n) if T[i] is not None)

    def lower(i):  # i 앞 기준점의 끝
        k = bisect.bisect_left(known, i) - 1
        return T[known[k]][1] if k >= 0 else 0.0

    def upper(i):  # i 뒤 기준점의 시작
        k = bisect.bisect_right(known, i)
        return T[known[k]][0] if k < len(known) else tot

    for k, (i0, i1) in enumerate(rng):
        idx = [i for i in range(i0, i1) if T[i] is not None]
        if not ok[k] or not idx:
            continue
        for x, y in zip(idx, idx[1:]):
            if y - x > 1:
                a, b = T[x][1], max(T[y][0], T[x][1])
                step = (b - a) / (y - x - 1)
                for j in range(1, y - x):
                    T[x + j] = (a + step * (j - 1), a + step * j)
        lo, hi = lower(i0), upper(i1 - 1)
        for j in range(idx[0] - 1, i0 - 1, -1):
            t1 = T[j + 1][0]
            T[j] = (min(t1, max(lo, t1 - CHAR_SEC)), t1)
        for j in range(idx[-1] + 1, i1):
            t0 = T[j - 1][1]
            T[j] = (t0, max(t0, min(hi, t0 + CHAR_SEC)))
    k = 0
    while k < len(rng):
        if ok[k]:
            k += 1
            continue
        run = [k]
        while run[-1] + 1 < len(rng) and not ok[run[-1] + 1]:
            run.append(run[-1] + 1)
        L = T[rng[k][0] - 1][1] if rng[k][0] > 0 and T[rng[k][0] - 1] else 0.0
        e_last = rng[run[-1]][1]
        U = T[e_last][0] if e_last < n and T[e_last] else tot
        U = max(U, L)
        loose = [x for x in run if starts[x] is None]
        need = sum(rng[x][1] - rng[x][0] for x in loose) * CHAR_SEC + 0.2 * len(loose)
        scale = min(1.0, (U - L) / need) if need > 0 else 1.0
        t = L
        for x in run:
            i0, i1 = rng[x]
            if starts[x] is not None:
                want = max(1, i1 - i0) * CHAR_SEC
                a = max(starts[x], t)
                if not fill:  # 다음 맞은 말 전에 끝나게 (자리가 없으면 앞 줄 끝에 붙여요)
                    a = min(a, max(t, U - want))
                a = min(a, U)
                nxt = next((starts[y] for y in range(x + 1, len(rng)) if starts[y] is not None), None)
                cap = min(v for v in (U, ends[x], nxt, a + NO_WORDS_CAP) if v is not None and v >= a)
                if ends[x] is not None:
                    b = max(a, min(ends[x], U))
                else:
                    b = cap if fill else min(cap, a + want)
                t = max(t, b)
            else:
                a = t + 0.1 * scale
                b = a + (i1 - i0) * CHAR_SEC * scale
                t = max(t, b + 0.1 * scale)
            step = (b - a) / max(1, i1 - i0)
            for j in range(i0, i1):
                T[j] = (a + step * (j - i0), a + step * (j - i0 + 1))
        k = run[-1] + 1
    return T


def _est_start(rng, anchors, k):
    """항목 k 의 말 시작 짐작: 첫 기준점 시간에서 앞 글자 수만큼 당겨요."""
    i0, i1 = rng[k]
    first = next((i for i in range(i0, i1) if i in anchors), None)
    return None if first is None else anchors[first][0] - (first - i0) * CHAR_SEC


def _pick_base(p, entries, est, base):
    if base in ("orig", "edit"):
        return base
    pairs = [(est[k], e["start"]) for k, e in enumerate(entries) if est[k] is not None and e.get("start") is not None]
    if not pairs or not project.cut_intervals(p):
        return "orig"
    tot = project.total(p)
    eo = statistics.median(abs(a - min(t, tot)) for a, t in pairs)
    ee = statistics.median(abs(a - project.to_original(p, t)) for a, t in pairs)
    return "edit" if ee < eo else "orig"


def _tokens(text, nl=True):
    """글 → [(단어, 줄 끝인지)] (글자 없는 조각은 앞 단어에 붙여요)."""
    out = []
    for line in text.split("\n"):
        for tok in line.split():
            if out and not _keys(tok) and not out[-1][1]:
                out[-1] = (out[-1][0] + tok, False)
            else:
                out.append((tok, False))
        if out and nl:
            out[-1] = (out[-1][0], True)
    return out


def _match(rng, anchors, starts):
    """항목별 맞음 여부. 대본 시간이 있으면 시간이 크게(1분 넘게) 어긋난 일치는 잘못 맞은 것으로 봐요."""
    ok = [i1 > i0 and sum(1 for i in range(i0, i1) if i in anchors) >= _need(i1 - i0) for i0, i1 in rng]
    res = [(k, est - starts[k]) for k in range(len(rng))
           if ok[k] and starts[k] is not None and (est := _est_start(rng, anchors, k)) is not None]
    if len(res) >= 3:
        med = statistics.median(r for _, r in res)
        for k, r in res:
            if abs(r - med) > 60 + (rng[k][1] - rng[k][0]) * CHAR_SEC:
                ok[k] = False
    return ok


def import_script(p, text, base="auto", replace=True, by="ui"):
    """대본을 자막으로 넣어요: 읽기 → 인식 단어와 글자 단위로 맞추기 → (긴 말은 max_chars 로 나눠) 자막 → 타이밍.
    base: 대본 시간이 원본(orig)·편집본(edit) 기준인지, auto 면 맞은 말소리와 더 잘 맞는 쪽.
    인식 단어가 없으면 대본 시간을 그대로 써요 (끝이 없으면 다음 줄 시작까지, 최대 6초).
    replace=True 면 기존 자막을 모두 바꿔요. {"count", "base", "unmatched"(확인 필요 줄 수), "entries", "matched"} 를 돌려줘요."""
    if base not in ("auto", "orig", "edit"):
        raise ValueError("base 는 auto·orig·edit 중 하나여야 해요")
    tot = project.total(p)
    if tot <= 0:
        raise ValueError("영상을 먼저 넣어 주세요")
    entries = parse_script(text)
    from . import analyze
    ws = analyze.words(p)
    timed = any(e.get("start") is not None for e in entries)
    if not ws and not timed:
        raise ValueError("시간이 없는 대본은 음성 인식을 먼저 해야 맞출 수 있어요")
    texts = [e["text"] for e in entries]
    if ws:
        rng, anchors = _align(texts, ws)
        first = _match(rng, anchors, [None] * len(rng))
        est = [_est_start(rng, anchors, k) if first[k] else None for k in range(len(rng))]
        base = _pick_base(p, entries, est, base)
    else:
        rng, anchors = _ranges(texts)[0], {}
        base = "orig" if base == "auto" else base

    def conv(t):
        if t is None:
            return None
        return round(project.to_original(p, t), 3) if base == "edit" else round(min(max(t, 0.0), tot), 3)

    starts = [conv(e.get("start")) for e in entries]
    ends = [conv(e.get("end")) for e in entries]
    ok = _match(rng, anchors, starts) if ws else [False] * len(rng)
    T = _char_times(rng, anchors, ok, starts, ends, tot, fill=not ws)
    rev, used = p.get("rev", 0) + 1, set() if replace else {t["id"] for t in p["subtitles"]}
    spk = _speaker_ids(p, [e.get("speaker") for e in entries], by, rev)
    max_chars = int(_settings(p)["max_chars"])
    cuts = [(a, b) for a, b in project.cut_intervals(p) if b - a > 1.0]
    new = []
    for k, e in enumerate(entries):
        i0, i1 = rng[k]
        sp = spk.get(e.get("speaker"))
        if i1 <= i0:  # 글자가 없는 줄(♪ 등)은 대본 시간이 있을 때만
            if starts[k] is not None:
                b = ends[k] if ends[k] is not None else min(tot, starts[k] + 1.0)
                new.append(_sub(p, e["text"], starts[k], b, rev, used, by, sp, bool(ws)))
            continue
        miss = bool(ws) and not ok[k]
        if e.get("cue") or ("\n" not in e["text"] and _len(e["text"]) <= max_chars
                            and (not ws or T[i1 - 1][1] - T[i0][0] <= MAX_LINE_SEC)):  # 짧은 대본 줄은 그대로 한 줄
            text = e["text"] if e.get("cue") else _clean(e["text"])
            new.append(_sub(p, text or e["text"], T[i0][0], T[i1 - 1][1], rev, used, by, sp, miss))
            continue
        toks, i = [], i0
        for tok, nl in _tokens(e["text"]):
            n = len(_keys(tok))
            if n == 0:
                continue
            toks.append({"word": tok, "start": T[i][0], "end": T[i + n - 1][1], "u": k, "nl": nl, "i": (i, i + n)})
            i += n
        for line in _group(toks, max_chars, cuts):
            a0, a1 = line[0]["i"][0], line[-1]["i"][1]
            hit = sum(1 for j in range(a0, a1) if j in anchors)
            text = _clean(" ".join(w["word"] for w in line))
            new.append(_sub(p, text or line[0]["word"], line[0]["start"], max(w["end"] for w in line), rev, used, by,
                            sp, miss or (bool(ws) and hit < _need(a1 - a0))))
    if not ws:  # 인식 단어가 없으면 대본 시간이 그대로 화면 시간이 되게 말소리 구간을 잡아요
        st = _settings(p)
        for x in new:
            a, b = x["speech"][0] + st["lead"], x["speech"][1] - st["hold"]
            if b - a >= 0.1:
                x["speech"] = [round(a, 3), round(b, 3)]
    else:  # 글에 비해 말소리가 너무 길게 잡힌 줄도 확인 표시
        for x in new:
            if x["speech"][1] - x["speech"][0] > max(3.0, len(_keys(x["text"])) * 0.45):
                x["check"] = True
    if replace:
        p["subtitles"] = []
    p["subtitles"] = sorted(p["subtitles"] + new, key=lambda t: (t["start"], t["end"]))
    retime(p)
    owner, n_all = _owners(rng), (rng[-1][1] if rng else 0)
    hit_all = sum(1 for i in anchors if ok[owner[i]])
    return {"count": len(new), "base": base, "unmatched": sum(1 for t in new if t["check"]),
            "entries": len(entries), "matched": round(hit_all / n_all, 3) if n_all else 0.0}


def _speaker_ids(p, labels, by, rev):
    """대본 화자 이름 → 화자 id (같은 이름의 화자가 있으면 그대로, 없으면 새로 만들어요. 26명까지)."""
    out, names = {}, {s["name"].strip().lower(): s["id"] for s in p["speakers"]}
    for lab in labels:
        if not lab or lab in out:
            continue
        key = lab.strip().lower()
        if key not in names:
            used = {s["id"] for s in p["speakers"]}
            free = next((c for c in map(chr, range(65, 91)) if c not in used), None)
            if free is None:
                out[lab] = None
                continue
            i = ord(free) - 65
            p["speakers"].append({"id": free, "name": lab.strip()[:40], "color": project.PALETTE[i % len(project.PALETTE)],
                                  "pos": dict(project.DEFAULT_POS), "by": by, "rev": rev})
            names[key] = free
        out[lab] = names[key]
    return out


# ---------- 나누기·합치기 ----------

def _find(p, iid):
    for i, t in enumerate(p["subtitles"]):
        if t["id"] == iid:
            return i, t
    raise ValueError(f"없는 자막이에요: {iid}")


def _num(v, label):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{label}: 숫자가 필요해요")
    return float(v)


def split_subtitle(p, sid, t, by="ui"):
    """자막 sid 를 원본 시간 t 에서 둘로 나눠요. 글은 단어 시간으로 (없으면 글자 비율로) 나눠요.
    말소리 구간도 t 에서 나누고, 잠긴 줄이면 두 줄 다 [start, t] / [t, end] 로 잠가요. (앞 줄, 뒷 줄)."""
    _, x = _find(p, sid)
    t = _num(t, "나눌 위치")
    if not x["start"] + 0.05 < t < x["end"] - 0.05:
        raise ValueError("자막 안쪽 시간에서 나눠 주세요")
    text = x["text"]
    toks = list(re.finditer(r"\S+", text))
    a, b = x.get("speech") or (x["start"], x["end"])
    lo, hi = (a, b) if a < t < b else (x["start"], x["end"])
    ratio = (t - lo) / (hi - lo) if hi > lo else 0.5
    if len(toks) >= 2:
        mids = _token_mids(p, x, [m.group() for m in toks])
        k = sum(1 for m in mids if m < t) if mids else None
        if k is None:
            total = sum(len(_keys(m.group())) for m in toks)
            acc, k = 0, 0
            for m in toks:  # 글자 비율에 가장 가까운 단어 경계
                n = len(_keys(m.group()))
                if abs(acc + n - ratio * total) <= abs(acc - ratio * total):
                    k += 1
                    acc += n
                else:
                    break
        k = min(max(k, 1), len(toks) - 1)
        cut = toks[k].start()
    else:
        cut = min(max(round(len(text) * ratio), 1), max(len(text) - 1, 1)) if len(text) >= 2 else len(text)
    t1, t2 = text[:cut].strip(), text[cut:].strip()
    rev = p.get("rev", 0) + 1
    m = min(max(t, a), b)
    first = dict(x, text=t1, end=round(t, 3), speech=[a, round(m, 3)], by=by, rev=rev)
    second = dict(copy.deepcopy(x), id=_fresh(p), text=t2, start=round(t, 3), speech=[round(m, 3), b], by=by, rev=rev)
    first["pos"], second["pos"] = copy.deepcopy(x.get("pos")), copy.deepcopy(x.get("pos"))
    i, _ = _find(p, sid)
    p["subtitles"][i:i + 1] = [first, second]
    retime(p)
    return copy.deepcopy(first), copy.deepcopy(second)


def _token_mids(p, x, toks):
    """자막 글 단어마다 말한 시간(가운데) — 인식 단어와 글자로 맞춰서. 맞출 수 없으면 None."""
    from . import analyze
    a, b = x.get("speech") or (x["start"], x["end"])
    ws = [w for w in analyze.words(p) if w["end"] > a - 0.3 and w["start"] < b + 0.3]
    if not ws:
        return None
    text = " ".join(toks)
    rng, anchors = _align([text], ws)
    if len(anchors) < _need(rng[0][1] - rng[0][0]):
        return None
    T = _char_times(rng, anchors, [True], [None], [None], project.total(p))
    out, i = [], 0
    for tok in toks:
        n = len(_keys(tok))
        out.append((T[i][0] + T[i + n - 1][1]) / 2 if n else (out[-1] if out else a))
        i += n
    return out


def merge_subtitles(p, ids, by="ui"):
    """자막 여러 줄을 하나로 합쳐요 (글은 시간 순으로 띄어 붙이고, 시간·말소리는 전체를 덮어요). 합친 줄을 돌려줘요."""
    if not isinstance(ids, (list, tuple)) or len(set(ids)) < 2:
        raise ValueError("합칠 자막을 두 줄 이상 골라 주세요")
    items = sorted((_find(p, i)[1] for i in dict.fromkeys(ids)), key=lambda t: (t["start"], t["end"]))
    first = items[0]
    sp = [x.get("speech") or (x["start"], x["end"]) for x in items]
    merged = dict(first, start=min(x["start"] for x in items), end=max(x["end"] for x in items),
                  text=" ".join(x["text"].strip() for x in items if x["text"].strip()),
                  speech=[min(a for a, _ in sp), max(b for _, b in sp)],
                  locked=any(x.get("locked") for x in items), check=any(x.get("check") for x in items),
                  by=by, rev=p.get("rev", 0) + 1)
    gone = {x["id"] for x in items[1:]}
    p["subtitles"] = [merged if t["id"] == first["id"] else t for t in p["subtitles"] if t["id"] not in gone]
    retime(p)
    return copy.deepcopy(merged)


def diff(old, new):
    """두 상태(project 전체나 일부 목록)를 id 로 비교해 apply_ops 와 같은 모양의 변경 기록을 만들어요.
    old 는 바꾸기 전에 copy.deepcopy 로 떠 둔 것이어야 해요."""
    out = []
    for coll in ("cuts", "subtitles", "speakers", "comments"):
        if coll not in old or coll not in new:
            continue
        a = {x["id"]: x for x in old[coll]}
        for x in new[coll]:
            if a.get(x["id"]) != x:
                out.append({"coll": coll, "item": copy.deepcopy(x)})
        ids = {x["id"] for x in new[coll]}
        out += [{"coll": coll, "id": i, "deleted": True} for i in a if i not in ids]
    for key in ("title", "notes", "splits"):
        if key in old and key in new and old[key] != new[key]:
            out.append({"key": key, "value": copy.deepcopy(new[key])})
    for sec, v in (new.get("settings") or {}).items():
        if "settings" in old and old["settings"].get(sec) != v:
            out.append({"key": f"settings.{sec}", "value": copy.deepcopy(v)})
    return out

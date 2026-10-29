// 타임라인 (spec 11.5·11.6): 도구 막대 · 눈금자(메모 핀·재생 헤드) · 자막 줄 · 영상(필름스트립) · 소리(파형).
// 축: 원본(잘린 구간 보기 켬 — 잘린 곳은 빗금, 끈 제안은 점선) / 편집본(남는 조각만, 캡컷처럼 2px 틈).
// 그리기: 눈금자·필름스트립·파형은 보이는 폭만 캔버스로, 자막·컷·틈·핀은 보이는 것만 DOM (요소 재사용).
// 가로 스크롤은 레이어 transform 하나, 확대·축소 때만 블록 위치를 다시 써요. 다시 그리기는 rAF 한 번으로 모아요.
// 내용 좌표: 축 시간 t → round(t*pps) px, 화면 x = 내용 x − X0.
import { h, iconBtn, btn, tip, toast, popover, menu, switchEl, clamp, debounce, isTyping } from './ui.js';
import { icon } from './icons.js';
import { api } from './api.js';
import { prefs } from './store.js';
import { fmt, fmtDur } from './time.js';
import * as X from './timeline/ops.js';
import { peaksFor, thumbsFor, silencesFor, quietRuns } from './timeline/assets.js';
import { drawRuler, drawFilm, drawWave } from './timeline/draw.js';
import { noFocus } from './panels/common.js';

const RULER_H = 24, LANE_H = 28, LANE_MIN = 20, VIDEO_H = 56, AUDIO_H = 44, GAP = 6, PAD_T = 6, PAD_B = 8;
const PAD_L = 12, PAD_R = 48, MAX_PPS = 600, SNAP_PX = 8, DRAG_PX = 3, OVERSCAN = 240;
const ED_MIN = 120, ED_MAX = 240;   // 자막 바로 고치기 칸 폭 (px): 블록 폭, 최소 ED_MIN, 빈 곳이 있으면 ED_MAX 까지
const F_LAYOUT = 1, F_MODEL = 2, F_VIEW = 4, F_ITEMS = 8, F_CANVAS = 16, F_PH = 32;
const CUT_LABEL = {silence: '무음', long: '긴 무음', filler: '필러', gap: '현장음', ng: 'NG', retake: '재촬영', manual: '요청', user: '직접', clip: '클립 뺌'};
const r3 = x => Math.round(x * 1000) / 1000;

function loadCss() {
  if (document.getElementById('css-timeline')) return Promise.resolve();
  return new Promise(res => {
    const l = h('link', {rel: 'stylesheet', id: 'css-timeline', href: new URL('../css/timeline.css', import.meta.url).href});
    l.onload = l.onerror = () => res();
    document.head.append(l);
    setTimeout(res, 2000);
  });
}

// 보이는 것만 DOM 으로: take(key) 로 요소를 받고 (없으면 재사용/생성), end() 에서 이번에 안 쓴 것을 떼어 내요
function pool(layer, make) {
  const live = new Map(), free = [];
  let stamp = 0;
  return {
    live,
    begin() { stamp++; },
    take(key) {
      let el = live.get(key);
      if (!el) { el = free.pop() || make(); el._k = el.dataset.id = key; el._c = {}; live.set(key, el); layer.append(el); }
      el._s = stamp;
      return el;
    },
    end() { for (const [k, el] of live) if (el._s !== stamp) { live.delete(k); el.remove(); free.push(el); } },
  };
}

// 정렬된 목록(a 기준)에서 [t0,t1] 과 겹치는 것
function each(list, maxLen, t0, t1, fn) {
  let lo = 0, hi = list.length;
  const from = t0 - maxLen;
  while (lo < hi) { const m = (lo + hi) >> 1; if (list[m].a < from) lo = m + 1; else hi = m; }
  for (let i = lo; i < list.length; i++) {
    const it = list[i];
    if (it.a > t1) break;
    if (it.b >= t0) fn(it);
  }
}

function nearest(arr, x) {
  if (!arr.length) return null;
  let lo = 0, hi = arr.length - 1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < x) lo = m + 1; else hi = m; }
  return lo > 0 && x - arr[lo - 1] < arr[lo] - x ? arr[lo - 1] : arr[lo];
}

export async function mount(el, store) {
  await loadCss();
  const S = {
    W: 0, H: 0, left: 0, dpr: 1, pps: 50, X0: -PAD_L, fit: true, tx: '',
    laneH: LANE_H, videoH: VIDEO_H, audioH: AUDIO_H, lanes: 1, sizes: '',
    axis: store.axis, D: 0, subs: [], subMax: 0, cuts: [], cutMax: 0, pieces: [], seams: [], clips: [], pins: [], src: new Map(),
    phX: NaN, userAt: 0, frozen: null, edit: null, hover: null,
  };
  const stats = {frames: 0, total: 0, max: 0, last: 0, reset() { this.frames = this.total = this.max = this.last = 0; }};
  let drag = null, raf = 0, dirty = 0;
  const minLen = () => 1 / (store.fps || 30);

  // ---------- 틀 ----------
  const head = (ic, label) => h('div.tl-head', {}, icon(ic, 16), h('span', {}, label));
  const corner = h('div.tl-corner');
  const rulerCv = h('canvas.tl-cv'), filmCv = h('canvas.tl-cv'), waveCv = h('canvas.tl-cv');
  const pinLayer = h('div.tl-layer');
  const ruler = h('div.tl-ruler', {}, rulerCv, pinLayer);
  const hSubs = head('captions', '자막'), hVideo = head('film', '영상'), hAudio = head('audio-lines', '소리');
  const heads = h('div.tl-heads', {}, hSubs, hVideo, hAudio);
  const subLayer = h('div.tl-layer');
  const trSubs = h('div.tl-track.tl-subs', {}, subLayer);
  const trVideo = h('div.tl-track.tl-video', {}, filmCv);
  const trAudio = h('div.tl-track.tl-audio', {}, waveCv);
  const ghost = h('div.tl-ghost', {hidden: true}, h('span'));
  const pSel = h('div.tl-pbox.sel', {hidden: true}, h('i.h.l'), h('i.h.r'));
  const pHov = h('div.tl-pbox.hov', {hidden: true}, h('i.h.l'), h('i.h.r'));
  const mediaLayer = h('div.tl-layer', {}, pHov, pSel, ghost);
  const mediaBox = h('div.tl-mediabox', {}, mediaLayer);
  const view = h('div.tl-view', {}, trSubs, trVideo, trAudio, mediaBox);
  const scroller = h('div.tl-scroll', {}, heads, view);
  const guide = h('div.tl-guide', {hidden: true});
  const badge = h('div.tl-badge', {hidden: true});
  const ph = h('div.tl-ph', {}, h('i.tl-knob'));
  const over = h('div.tl-over', {}, guide, ph, badge);
  const empty = h('div.tl-empty.empty', {hidden: true}, icon('film', 22), h('div.t', {}, '영상 없음'),
    btn('가져오기', {icon: 'plus', cls: 'sm', onClick: () => store.setTab('media')}));
  const body = h('div.tl-body', {}, corner, ruler, scroller, over, empty);
  const ctx = {ruler: rulerCv.getContext('2d'), film: filmCv.getContext('2d'), wave: waveCv.getContext('2d')};

  // ---------- 도구 막대 ----------
  const sm = {cls: 'ghost sm'};
  const B = {
    undo: iconBtn('undo', '되돌리기', () => undoRedo(false), {...sm, key: 'mod+z'}),
    redo: iconBtn('redo', '다시 하기', () => undoRedo(true), {...sm, key: 'mod+shift+z'}),
    split: btn('분할', {icon: 'split', cls: 'ghost sm', tip: '분할', key: 'mod+b', onClick: () => run('split')}),
    q: iconBtn('trim-start', '앞부분 자르기', () => run('trimStart'), {...sm, key: 'q'}),
    w: iconBtn('trim-end', '뒷부분 자르기', () => run('trimEnd'), {...sm, key: 'w'}),
    del: iconBtn('trash', '삭제', () => run('delete'), {...sm, key: 'delete'}),
    sub: iconBtn('text-plus', '자막 추가', () => run('addSub'), {...sm, key: 't'}),
    memo: iconBtn('map-pin-plus', '메모 추가', () => run('addMemo'), {...sm, key: 'm'}),
    magnet: iconBtn('magnet', '스냅', () => store.setView({magnet: !store.magnet}), {...sm, pressed: store.magnet}),
    axis: switchEl({checked: store.axis === 'orig', label: '잘린 구간 보기', onChange: v => store.setAxis(v ? 'orig' : 'edit')}),
    out: iconBtn('minus', '축소', () => run('zoomOut'), {...sm, key: 'minus'}),
    zin: iconBtn('plus', '확대', () => run('zoomIn'), {...sm, key: 'plus'}),
    fit: iconBtn('fit', '화면 맞춤', () => run('fit'), {...sm, key: 'shift+z'}),
  };
  const zoom = h('input.range.tl-zoom', {type: 'range', min: 0, max: 1000, step: 1, 'aria-label': '확대 비율'});
  zoom.addEventListener('input', () => zoomTo(fracToPps(+zoom.value / 1000)));
  zoom.addEventListener('pointerup', () => zoom.blur());   // 마우스로 끈 뒤 ←/→ 가 확대가 아니라 프레임 이동으로 가게
  const sep = () => h('span.vsep');
  // 글자를 눌러도 스위치가 포커스를 가져가지 않게 label 대신 div + click() (label 은 눌린 컨트롤에 포커스를 줘요)
  const axisRow = h('div.tl-axis', {onclick: e => { if (!B.axis.contains(e.target)) B.axis.click(); }}, h('span', {}, '잘린 구간 보기'), B.axis);
  const bar = h('div.tl-bar', {role: 'toolbar', 'aria-label': '타임라인 도구'},
    B.undo, B.redo, sep(), B.split, sep(), B.q, B.w, sep(), B.del, sep(), B.sub, sep(), B.memo, h('div.sp'),
    B.magnet, sep(), axisRow, sep(), B.out, zoom, B.zin, B.fit);
  const root = h('div.tl', {}, bar, body);
  noFocus(root);   // 마우스로 누른 버튼에 포커스 링이 남지 않게 (키보드 Tab 은 그대로)
  el.append(root);

  function undoRedo(redo) {
    const label = redo ? store.redo() : store.undo();
    if (label === null) return;
    toast(`${redo ? '다시 하기' : '되돌리기'}${label ? ` · ${label}` : ''}`, {icon: redo ? 'redo' : 'undo', timeout: 1400});
  }
  function updateBar() {
    B.undo.disabled = !store.canUndo;
    B.redo.disabled = !store.canRedo;
    tip(B.undo, store.undoLabel ? `되돌리기 · ${store.undoLabel}` : '되돌리기', 'mod+z');
    tip(B.redo, store.redoLabel ? `다시 하기 · ${store.redoLabel}` : '다시 하기', 'mod+shift+z');
    const k = store.sel?.kind;
    B.del.disabled = !(k === 'sub' || k === 'piece' || k === 'comment' || k === 'cut');
    B.magnet.setAttribute('aria-pressed', String(!!store.magnet));
    B.axis.set(store.axis === 'orig');
    const f = ppsToFrac(S.pps);
    zoom.value = String(Math.round(f * 1000));
    zoom.style.setProperty('--p', `${f * 100}%`);
    corner.textContent = store.axis === 'edit' ? '편집본' : '원본';
  }

  // ---------- 크기 ----------
  function sizeCanvas(cv, w, ht) {
    const W = Math.max(1, Math.round(w * S.dpr)), H = Math.max(1, Math.round(ht * S.dpr));
    if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; cv.style.width = `${w}px`; cv.style.height = `${ht}px`; }
  }
  function layout() {
    S.dpr = devicePixelRatio || 1;
    S.W = view.clientWidth;
    S.H = scroller.clientHeight;
    S.left = view.getBoundingClientRect().left;
    const lanes = S.lanes, fixed = PAD_T + 2 * GAP + PAD_B;
    let laneH = LANE_H, vH = VIDEO_H, aH = AUDIO_H;
    if (fixed + lanes * laneH + vH + aH > S.H) {
      vH = 48; aH = 36;
      laneH = clamp(Math.floor((S.H - fixed - vH - aH) / lanes), LANE_MIN, LANE_H);
    }
    const key = `${laneH}|${vH}|${aH}|${lanes}`;
    if (key !== S.sizes) {
      S.sizes = key;
      Object.assign(S, {laneH, videoH: vH, audioH: aH});
      const sh = `${lanes * laneH}px`;
      trSubs.style.height = hSubs.style.height = sh;
      trVideo.style.height = hVideo.style.height = `${vH}px`;
      trAudio.style.height = hAudio.style.height = `${aH}px`;
      view.style.setProperty('--lane-h', `${laneH}px`);
      mediaBox.style.top = `${PAD_T + lanes * laneH + GAP}px`;
      mediaBox.style.height = `${vH + GAP + aH}px`;
    }
    sizeCanvas(rulerCv, S.W, RULER_H);
    sizeCanvas(filmCv, S.W, vH);
    sizeCanvas(waveCv, S.W, aH);
  }

  // ---------- 확대 · 스크롤 ----------
  const fitPps = () => S.D > 0 && S.W > 0 ? clamp((S.W - PAD_L - 24) / S.D, 0.005, MAX_PPS) : 50;
  const minPps = () => Math.min(fitPps(), MAX_PPS);
  const ppsToFrac = p => { const a = Math.log(minPps()), b = Math.log(MAX_PPS); return b > a ? clamp((Math.log(p) - a) / (b - a), 0, 1) : 0; };
  const fracToPps = f => Math.exp(Math.log(minPps()) + f * (Math.log(MAX_PPS) - Math.log(minPps())));
  function clampView() {
    if (S.fit) { S.pps = fitPps(); S.X0 = -PAD_L; return; }
    if (S.pps < minPps()) S.pps = minPps();
    const max = Math.max(-PAD_L, Math.round(S.D * S.pps) + PAD_R - S.W);
    S.X0 = clamp(Math.round(S.X0), -PAD_L, max);
  }
  const phPos = () => store.toAxis(store.time);
  const phX = () => Math.round(phPos() * S.pps) - S.X0;
  const anchor = () => { const x = phX(); return x >= 0 && x <= S.W ? x : S.W / 2; };
  function zoomTo(pps, ax = anchor()) {
    const t = (S.X0 + ax) / S.pps;
    S.pps = clamp(pps, minPps(), MAX_PPS);
    S.fit = S.pps <= minPps() * 1.0001;
    S.X0 = Math.round(t * S.pps - ax);
    clampView();
    saveView();
    invalidate(F_VIEW);
    updateBar();
  }
  function scrollBy(dx) {
    S.X0 += dx;
    S.userAt = performance.now();
    clampView();
    invalidate(F_VIEW);
    saveView();
  }
  function ensureVisible(a, b, margin = 40) {
    const xa = a * S.pps - S.X0, xb = b * S.pps - S.X0;
    if (xa >= margin && xb <= S.W - margin) return;
    S.X0 = (b - a) * S.pps < S.W - 2 * margin ? Math.round(((a + b) / 2) * S.pps - S.W / 2) : Math.round(a * S.pps - margin);
    clampView();
    invalidate(F_VIEW);
  }
  const saveView = debounce(() => {
    prefs.set(`tl:${store.name}`, {pps: S.fit ? 0 : S.pps});
    store.setView({zoom: S.pps}, 'timeline');
  }, 300);

  // ---------- 데이터 → 화면 모델 (축 시간) ----------
  function srcInfo(sid) { return S.src.get(sid); }
  function syncAssets() {
    const p = store.project, m = new Map();
    let off = 0;
    for (const s of p.sources) {
      const a = store.assets?.[s.id] || {}, ready = s.status === 'ready', key = `${s.status}:${s.duration}`;
      const busy = ready ? null : s.status === 'error' ? '오류' : s.status === 'queued' ? '대기' : `분석 중 ${Math.round((+s.progress || 0) * 100)}%`;
      const redraw = () => invalidate(F_CANVAS);
      m.set(s.id, {offset: off, busy, name: s.name,
        peaks: ready && a.peaks ? peaksFor(a.peaks, key, redraw) : null,
        silences: ready && a.silences ? silencesFor(a.silences, key) : null,
        thumbs: ready && a.thumbs ? thumbsFor(a.thumbs, a.thumbs_meta || a.thumbs.replace(/\.jpg(\?|$)/, '.json$1'), key, redraw) : null});
      off += +s.duration || 0;
    }
    S.src = m;
  }

  function buildModel() {
    const p = store.project, map = store.map, edit = store.axis === 'edit';
    const E = edit ? t => map.toEdited(t) : t => t;
    S.axis = store.axis;
    const D = edit ? map.edited : map.total;
    if (D !== S.D) { S.D = D; dirty |= F_VIEW; }
    // 자막 (편집본 축에서는 다 잘린 줄은 빼요). 줄 번호는 보이는 것끼리 겹치면 아래로 (끄는 동안은 그대로)
    const subs = [], ends = [];
    let m = 0;
    for (const s of p.subtitles) {
      const a = E(s.start), b = E(s.end), hid = edit ? b - a < 1e-3 : map.toEdited(s.end) - map.toEdited(s.start) < 1e-3;
      if (edit && hid) continue;
      let lane = S.frozen?.get(s.id);
      if (lane == null) { lane = ends.findIndex(e => e <= a + 1e-6); if (lane < 0) lane = ends.length; }
      ends[lane] = Math.max(ends[lane] ?? 0, b);
      subs.push({id: s.id, s, a, b, lane, hid});
      if (b - a > m) m = b - a;
    }
    S.subs = subs; S.subMax = m;
    const nLanes = Math.max(1, ends.length, S.edit?.draft ? S.edit.draft.lane + 1 : 0);   // 새 자막 칸이 들어갈 아래 줄까지
    if (nLanes !== S.lanes) { S.lanes = nLanes; dirty |= F_LAYOUT; }
    // 컷 (원본 축)
    const cuts = [];
    m = 0;
    if (!edit) for (const c of p.cuts) { cuts.push({id: c.id, c, a: c.start, b: c.end}); if (c.end - c.start > m) m = c.end - c.start; }
    S.cuts = cuts; S.cutMax = m;
    // 남는 조각 · 틈 · 캔버스 클립
    const pcs = store.derived.pieces;
    S.pieces = pcs.map(pc => { const a = E(pc.start); return {id: pc.id, pc, a, b: edit ? a + (pc.end - pc.start) : pc.end}; });
    const seams = [], clips = [];
    if (edit) {
      let prev = 0;
      for (const it of S.pieces) {
        if (it.pc.start - prev > X.EPS) {
          seams.push({id: `s${Math.round(prev * 1000)}`, x: it.a, a: prev, b: it.pc.start});
        }
        prev = it.pc.end;
        clips.push({sid: it.pc.sid, a: it.a, b: it.b, o: it.pc.start, label: fmtDur(it.pc.end - it.pc.start)});
      }
      if (map.total - prev > X.EPS) {
        seams.push({id: `s${Math.round(prev * 1000)}`, x: D, a: prev, b: map.total});
      }
      const matches = X.regionCuts(p.cuts, seams);
      for (let i = 0; i < seams.length; i++) Object.assign(seams[i], matches[i]);
    } else {
      const splits = [...(p.splits || [])].sort((a, b) => a - b);
      for (const sp of store.derived.spans) {
        const name = S.src.get(sp.id)?.name || sp.id;
        let a = sp.start;
        for (const t of splits) if (t > a + X.EPS && t < sp.end - X.EPS) { clips.push({sid: sp.id, a, b: t, o: a, label: a === sp.start ? name : null}); a = t; }
        clips.push({sid: sp.id, a, b: sp.end, o: a, label: a === sp.start ? name : null});
      }
    }
    S.seams = seams.filter(s => s.cut); S.clips = clips;
    S.pins = p.comments.map(c => ({id: c.id, c, x: E(c.t)})).sort((a, b) => a.x - b.x);
    empty.hidden = !!p.sources.length;
  }

  // ---------- 블록 그리기 ----------
  const P = {
    subs: pool(subLayer, () => h('div.tl-sub', {}, h('i.h.l'), h('span.t'), h('i.h.r'))),
    cuts: pool(mediaLayer, () => h('div.tl-cut', {}, h('i.h.l'), h('i.h.r'))),
    seams: pool(mediaLayer, () => h('div.tl-seam', {}, h('i'))),
    pins: pool(pinLayer, () => h('div.tl-pin', {}, h('i'))),
  };
  function setTip(el, c, text) { if (c.tip !== text) { c.tip = text; if (text) el.dataset.tip = text; else delete el.dataset.tip; } }
  function place(el, c, x, y, w) {
    if (c.x !== x || c.y !== y) { el.style.transform = `translate(${x}px,${y}px)`; c.x = x; c.y = y; }
    if (w != null && c.w !== w) { el.style.width = `${w}px`; c.w = w; }
  }
  function setCls(el, c, cls, id) {
    const g = store.glowLeft(id) > 0;
    if (g && !c.glow) el.style.animationDelay = `${store.glowLeft(id) - 8000}ms`;
    c.glow = g;
    if (g) cls += ' glow';
    if (c.cls !== cls) { el.className = cls; c.cls = cls; }
  }
  function drawSub(el, it, sel) {
    const c = el._c, x = Math.round(it.a * S.pps) + 1, w = Math.max(2, Math.round(it.b * S.pps) - 1 - x);
    place(el, c, x, it.lane * S.laneH + 2, w);
    const txt = w >= 22 ? it.s.text : '', t = el.children[1];
    if (c.txt !== txt) { t.textContent = txt; c.txt = txt; }
    const stick = txt ? clamp(S.X0 - x, 0, w - 48) : 0;   // 왼쪽 끝에 걸친 블록은 글자가 화면 안에 남게
    if (c.st !== stick) { t.style.paddingLeft = stick ? `${stick + 9}px` : ''; c.st = stick; }
    const sp = store.speakerColor(it.s.speaker);
    if (c.sp !== sp) { el.style.setProperty('--sp', sp); c.sp = sp; }
    setCls(el, c, `tl-sub${sel ? ' sel' : ''}${it.hid ? ' hid' : ''}${it.s.check ? ' chk' : ''}${it.s.locked ? ' lock' : ''}${w < 16 ? ' thin' : ''}${drag?.id === it.id && drag.started ? ' drag' : ''}`, it.id);
  }
  function drawCut(el, a, b, cut, sel) {
    const c = el._c, x = Math.round(a * S.pps), w = Math.max(2, Math.round(b * S.pps) - x);
    place(el, c, x, 0, w);
    if (c.type !== cut.type) { el.dataset.type = cut.type; c.type = cut.type; }
    const why = cut.reason && (cut.type === 'ng' || cut.type === 'retake' || cut.type === 'manual') ? ` · ${cut.reason}` : '';   // Claude 가 적은 이유만
    setTip(el, c, `${CUT_LABEL[cut.type] || cut.type} · ${fmtDur(cut.end - cut.start)}${why}`);
    setCls(el, c, `tl-cut ${cut.enabled ? 'on' : 'off'}${!cut.reviewed && cut.type !== 'clip' ? ' unrev' : ''}${sel ? ' sel' : ''}${w < 14 ? ' thin' : ''}`, cut.id);
  }
  function placeBox(box, it) {
    if (!it) { box.hidden = true; return; }
    const x = Math.round(it.a * S.pps) + 1, w = Math.max(1, Math.round(it.b * S.pps) - 1 - x);
    box.hidden = false;
    box.style.transform = `translate(${x}px,0)`;
    box.style.width = `${w}px`;
    box.classList.toggle('thin', w < 18);
    box.classList.toggle('ed', S.axis === 'edit');
  }
  const pieceItem = id => S.pieces.find(x => x.id === id) || null;

  function renderItems() {
    const tx = `translate3d(${-S.X0}px,0,0)`;
    if (S.tx !== tx) { subLayer.style.transform = mediaLayer.style.transform = pinLayer.style.transform = tx; S.tx = tx; }
    const t0 = (S.X0 - OVERSCAN) / S.pps, t1 = (S.X0 + S.W + OVERSCAN) / S.pps;
    const sk = store.sel?.kind, sid = store.sel?.id;
    P.subs.begin();
    each(S.subs, S.subMax, t0, t1, it => drawSub(P.subs.take(it.id), it, sk === 'sub' && sid === it.id));
    P.subs.end();
    P.cuts.begin();
    if (S.axis === 'orig') each(S.cuts, S.cutMax, t0, t1, it => drawCut(P.cuts.take(it.id), it.a, it.b, it.c, sk === 'cut' && sid === it.id));
    else if (sk === 'cut') {   // 편집본 축: 고른 컷이 꺼져 있으면 (남는 부분 안) 점선으로
      const c = store.item('cut', sid);
      if (c && !c.enabled) drawCut(P.cuts.take(c.id), store.toEdited(c.start), store.toEdited(c.end), c, true);
    }
    P.cuts.end();
    P.seams.begin();
    const selCut = sk === 'cut' ? store.item('cut', sid) : null;
    let lastX = -1e9;
    for (const sm of S.seams) {   // 너무 붙은 표시는 건너뛰어요 (고른 것은 늘 보여요)
      if (sm.x < t0 || sm.x > t1) continue;
      const x = Math.round(sm.x * S.pps);
      const sel = !!selCut && selCut.enabled && selCut.start < sm.b - X.EPS && selCut.end > sm.a + X.EPS;
      if (x - lastX < 12 && !sel) continue;
      lastX = x;
      const el = P.seams.take(sm.id), c = el._c;
      place(el, c, x, 0);
      setTip(el, c, `−${fmtDur(sm.b - sm.a)}`);
      setCls(el, c, `tl-seam${sm.unrev ? ' unrev' : ''}${sel ? ' sel' : ''}`, sm.cut.id);
    }
    P.seams.end();
    P.pins.begin();
    for (const pn of S.pins) {
      if (pn.x < t0 || pn.x > t1) continue;
      const el = P.pins.take(pn.id), c = el._c;
      place(el, c, Math.round(pn.x * S.pps), 0);
      setTip(el, c, pn.c.text ? pn.c.text.slice(0, 80) : '메모');
      setCls(el, c, `tl-pin${pn.c.done ? ' done' : ''}${sk === 'comment' && sid === pn.id ? ' sel' : ''}`, pn.id);
    }
    P.pins.end();
    placeBox(pSel, sk === 'piece' ? pieceItem(sid) : null);
    placeBox(pHov, S.hover && !drag && !(sk === 'piece' && sid === S.hover.id) ? pieceItem(S.hover.id) : null);
    if (S.edit) placeEditor();
  }

  function drawCanvases() {
    const o = {W: S.W, X0: S.X0, pps: S.pps, D: S.D, dpr: S.dpr};
    drawRuler(ctx.ruler, {...o, H: RULER_H});
    drawFilm(ctx.film, {...o, H: S.videoH}, S.clips, srcInfo);
    drawWave(ctx.wave, {...o, H: S.audioH}, S.clips, srcInfo);
  }

  function renderPlayhead() {
    const x = phX();
    if (x !== S.phX) { ph.style.transform = `translate3d(${x}px,0,0)`; S.phX = x; }
    if (store.playing && !drag && performance.now() - S.userAt > 1500 && (x > S.W - 24 || x < 0)) {   // 재생 중 화면 넘기기
      S.X0 = Math.round(phPos() * S.pps) - 48;
      clampView();
      invalidate(F_VIEW);
    }
  }

  function invalidate(f) {
    dirty |= f;
    if (!raf) raf = requestAnimationFrame(frame);
  }
  function flushNow() {   // DOM 위치를 바로 읽어야 할 때 (팝오버 · 바로 고치기)
    if (raf) cancelAnimationFrame(raf);
    frame();
  }
  function frame() {
    raf = 0;
    const t0 = performance.now();
    let f = dirty;
    dirty = 0;
    if (f & F_MODEL) { buildModel(); f |= dirty; dirty = 0; }
    if (f & F_LAYOUT) layout();
    if (f & (F_MODEL | F_LAYOUT | F_VIEW)) { const p = S.pps; clampView(); if (p !== S.pps) updateBar(); }
    if (drag?.started && autoScroll()) f |= F_VIEW;
    if (f & (F_MODEL | F_LAYOUT | F_VIEW | F_ITEMS)) renderItems();
    if (f & (F_MODEL | F_LAYOUT | F_VIEW | F_CANVAS)) drawCanvases();
    renderPlayhead();
    const dt = performance.now() - t0;
    stats.frames++; stats.total += dt; stats.last = dt; if (dt > stats.max) stats.max = dt;
  }

  // ---------- 고르기 · 보이기 ----------
  function rangeOf(sel) {
    const it = store.item(sel.kind, sel.id), E = t => store.toAxis(t);
    if (!it) return null;
    if (sel.kind === 'comment') return [E(it.t), E(it.t)];
    if (sel.kind === 'piece') { const a = E(it.start); return [a, S.axis === 'edit' ? a + it.end - it.start : it.end]; }
    if (it.start == null) return null;
    return [E(it.start), E(it.end)];
  }
  function reveal(sel) {
    const r = rangeOf(sel);
    if (!r) return;
    ensureVisible(r[0], r[1]);
    if (sel.kind === 'sub' && scroller.scrollHeight > scroller.clientHeight) {   // 자막 줄이 아래로 넘치면 세로로도
      const lane = S.subs.find(x => x.id === sel.id)?.lane ?? 0, y = PAD_T + lane * S.laneH;
      if (y < scroller.scrollTop || y + S.laneH > scroller.scrollTop + scroller.clientHeight) scroller.scrollTop = y - 4;
    }
  }
  const select = (kind, id) => store.select(kind, id, {src: 'timeline'});
  const seek = at => store.setTime(store.fromAxis(clamp(at, 0, S.D)), 'timeline');
  const tAt = cx => (S.X0 + cx - S.left) / S.pps;

  // ---------- 스냅 ----------
  function snapTargets({sub, cut, quiet, not = []} = {}) {
    const xs = [0, S.D, phPos()], E = t => store.toAxis(t), edit = S.axis === 'edit';
    for (const it of S.subs) if (it.id !== sub) xs.push(it.a, it.b);
    if (!edit) for (const it of S.cuts) if (it.id !== cut) xs.push(it.a, it.b);
    for (const it of S.pieces) xs.push(it.a, it.b);
    for (const sp of store.derived.spans) xs.push(E(sp.start));
    for (const pn of S.pins) xs.push(pn.x);
    const q = [];
    if (quiet && !edit) {   // 쉼 경계: silences.json 의 fine 무음, 없으면 파형에서 찾은 조용한 구간
      for (const s of S.src.values()) {
        const runs = s.silences?.state === 'ready' ? s.silences.fine : s.peaks?.state === 'ready' ? quietRuns(s.peaks) : [];
        for (const [a, b] of runs) q.push(s.offset + a, s.offset + b);
      }
    }
    const keep = v => !not.some(n => Math.abs(v - n) < 1e-6);   // 끄는 항목 자신의 원래 자리는 빼요
    return {xs: Float64Array.from(xs.filter(keep)).sort(), q: Float64Array.from(q.filter(keep)).sort()};
  }
  function snap(tg, x) {
    if (!store.magnet || drag?.alt || !tg) return null;
    const thr = SNAP_PX / S.pps;
    const a = nearest(tg.xs, x);
    if (a != null && Math.abs(a - x) <= thr) return {x: a};
    const b = nearest(tg.q, x);
    return b != null && Math.abs(b - x) <= thr * 0.75 ? {x: b, quiet: true} : null;
  }
  function showGuide(sn) {
    if (!sn) { guide.hidden = true; return; }
    guide.hidden = false;
    guide.classList.toggle('q', !!sn.quiet);
    guide.style.transform = `translate3d(${Math.round(sn.x * S.pps) - S.X0}px,0,0)`;
  }
  function showBadge(text, ax) {
    badge.hidden = !text;
    if (!text) return;
    badge.textContent = text;
    badge.style.transform = `translate3d(${clamp(Math.round(ax * S.pps) - S.X0, 30, S.W - 30)}px,0,0)`;
  }

  // 편집본 축 위치 → 원본 (끝은 앞 조각 끝으로)
  const toOrigStart = x => S.axis === 'edit' ? store.toOriginal(x) : x;
  function toOrigEnd(x) {
    if (S.axis !== 'edit') return x;
    const {acc, keep} = store.map;
    let lo = 0, hi = keep.length - 1, i = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (acc[m] < x) { i = m; lo = m + 1; } else hi = m - 1; }
    if (i < 0) return keep.length ? keep[0][0] : 0;
    return Math.min(keep[i][1], keep[i][0] + (x - acc[i]));
  }

  // ---------- 끌기 ----------
  function pieceHit(x) {
    const at = (S.X0 + x) / S.pps, L = S.pieces, tol = 4 / S.pps;
    if (!L.length) return null;
    let lo = 0, hi = L.length - 1, i = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (L[m].a <= at) { i = m; lo = m + 1; } else hi = m - 1; }
    let it = L[Math.max(0, i)];
    if (i < 0 && it.a - at > tol) return null;
    if (at > it.b) { const nx = L[i + 1]; if (nx && nx.a - at <= tol) it = nx; else if (at - it.b > tol) return null; }
    let edge = null;
    if (S.axis === 'edit') {
      const xa = Math.round(it.a * S.pps) - S.X0, xb = Math.round(it.b * S.pps) - S.X0, z = Math.min(8, (xb - xa) / 3);
      if (x - xa < z) edge = 'start'; else if (xb - x < z) edge = 'end';
    }
    return {it, edge};
  }
  function hover(e) {
    let hv = null;
    if (e && !drag && S.axis === 'edit' && e.target.closest?.('.tl-mediabox') && !e.target.closest('.tl-seam,.tl-cut')) {
      const hit = pieceHit(e.clientX - S.left);
      if (hit) hv = {id: hit.it.id, edge: hit.edge};
    }
    const cur = hv?.edge ? 'ew-resize' : '';
    if (mediaBox.style.cursor !== cur) mediaBox.style.cursor = cur;
    if (hv?.id !== S.hover?.id) { S.hover = hv; invalidate(F_ITEMS); } else S.hover = hv;
  }

  function onDown(e) {
    if (e.button !== 0 || drag) return;
    const t = e.target;
    if (!(t instanceof Element) || t.closest('.tl-edit')) return;
    if (S.edit) closeEditor(true);
    if (t.closest('.tl-heads,.tl-empty,.tl-corner')) return;
    const a = document.activeElement;
    if (a && isTyping(a) && !root.contains(a)) a.blur();
    S.left = view.getBoundingClientRect().left;
    const at = tAt(e.clientX), edgeOf = n => n.classList.contains('h') ? (n.classList.contains('l') ? 'start' : 'end') : null;
    const sub = t.closest('.tl-sub'), cut = t.closest('.tl-cut'), seam = t.closest('.tl-seam'), pin = t.closest('.tl-pin');
    let d;
    if (e.shiftKey) d = {kind: 'zoom', pps0: S.pps, ax: clamp(e.clientX - S.left, 0, S.W)};   // ⇧+끌기: 오른쪽 확대 · 왼쪽 축소
    else if (pin) {
      select('comment', pin._k);
      const c = store.item('comment', pin._k);
      if (c) store.setTime(c.t, 'timeline');
      d = {kind: 'none'};
    } else if (t.closest('.tl-ruler,.tl-knob')) d = {kind: 'scrub'};
    else if (sub) { select('sub', sub._k); d = {kind: 'sub', id: sub._k, edge: edgeOf(t) || 'move'}; }
    else if (seam) { const sm = S.seams.find(x => x.id === seam._k); if (sm) select('cut', sm.cut.id); d = {kind: 'none'}; }
    else if (cut) { select('cut', cut._k); d = {kind: edgeOf(t) && S.axis === 'orig' ? 'cut' : 'none', id: cut._k, edge: edgeOf(t)}; }
    else if (t.closest('.tl-mediabox')) {
      const hit = pieceHit(e.clientX - S.left);
      if (hit) { select('piece', hit.it.id); d = hit.edge ? {kind: 'trim', id: hit.it.id, side: hit.edge} : {kind: 'none'}; }
      else d = {kind: 'scrub', empty: true};
    } else d = {kind: 'scrub', empty: true};
    if (d.kind === 'scrub') {
      if (d.empty && store.sel) select(null);
      seek(at);
    }
    Object.assign(d, {pid: e.pointerId, cx0: e.clientX, cy0: e.clientY, cx: e.clientX, started: false});
    drag = d;
    body.setPointerCapture(e.pointerId);
    hover(null);
    e.preventDefault();
  }
  function onMove(e) {
    if (!drag) return hover(e);
    if (e.pointerId !== drag.pid) return;
    drag.cx = e.clientX;
    drag.alt = e.altKey;
    if (!drag.started) {
      if (Math.abs(e.clientX - drag.cx0) < DRAG_PX && Math.abs(e.clientY - drag.cy0) < DRAG_PX) return;
      drag.started = true;
      begin(drag);
    }
    move(drag);
    invalidate(F_PH);
  }
  function onUp(e) {
    if (!drag || e.pointerId !== drag.pid) return;
    const d = drag;
    drag = null;
    if (body.hasPointerCapture(e.pointerId)) body.releasePointerCapture(e.pointerId);
    if (d.started) finish(d, false);
  }
  function cancelDrag() {
    const d = drag;
    drag = null;
    if (d && body.hasPointerCapture(d.pid)) body.releasePointerCapture(d.pid);
    if (d?.started) finish(d, true);
  }

  function begin(d) {
    if (d.kind === 'scrub') { if (store.playing) store.setPlaying(false, 'scrub'); return; }
    if (d.kind === 'sub') {
      const it = S.subs.find(x => x.id === d.id);
      if (!it) { d.kind = 'none'; return; }
      Object.assign(d, {a0: it.a, b0: it.b, grab: tAt(d.cx0) - (d.edge === 'end' ? it.b : it.a), tg: snapTargets({sub: d.id})});
      S.frozen = new Map(S.subs.map(x => [x.id, x.lane]));
      d.g = store.gesture();
    } else if (d.kind === 'cut') {
      const c = store.item('cut', d.id);
      if (!c) { d.kind = 'none'; return; }
      Object.assign(d, {c0: {start: c.start, end: c.end}, grab: tAt(d.cx0) - (d.edge === 'start' ? c.start : c.end), tg: snapTargets({cut: d.id, quiet: true, not: [c.start, c.end]})});
      d.g = store.gesture();
    } else if (d.kind === 'trim') {
      const it = pieceItem(d.id);
      if (!it) { d.kind = 'none'; return; }
      const span = store.derived.spans.find(s => s.id === it.pc.sid);
      d.pc = {...it.pc};
      d.edgeA = d.side === 'start' ? it.a : it.b;
      d.edgeO = d.side === 'start' ? it.pc.start : it.pc.end;
      d.bounds = X.trimBounds(store.map.iv, span, it.pc, d.side, minLen());
      d.grab = tAt(d.cx0) - d.edgeA;
      d.tg = snapTargets({not: [d.edgeA]});
      d.g = store.gesture();
    }
  }
  function move(d) {
    if (d.kind === 'scrub') return seek(tAt(d.cx));
    if (d.kind === 'zoom') return zoomTo(d.pps0 * Math.exp((d.cx - d.cx0) * 0.008), d.ax);
    if (d.kind === 'sub') return moveSub(d);
    if (d.kind === 'cut') return moveCut(d);
    if (d.kind === 'trim') return moveTrim(d);
  }
  function moveSub(d) {
    const len = d.b0 - d.a0, t = tAt(d.cx) - d.grab, mn = Math.min(0.1, len);
    let a = d.a0, b = d.b0, sn = null;
    if (d.edge === 'move') {
      a = clamp(t, 0, Math.max(0, S.D - len));
      const s1 = snap(d.tg, a), s2 = snap(d.tg, a + len);
      if (s1 && (!s2 || Math.abs(s1.x - a) <= Math.abs(s2.x - a - len))) { a = s1.x; sn = s1; } else if (s2) { a = s2.x - len; sn = s2; }
      a = clamp(a, 0, Math.max(0, S.D - len));
      b = a + len;
    } else if (d.edge === 'start') {
      a = clamp(t, 0, d.b0 - mn);
      sn = snap(d.tg, a);
      if (sn && sn.x <= d.b0 - mn) a = sn.x; else sn = null;
    } else {
      b = clamp(t, d.a0 + mn, S.D);
      sn = snap(d.tg, b);
      if (sn && sn.x >= d.a0 + mn) b = sn.x; else sn = null;
    }
    const start = r3(toOrigStart(a)), end = r3(Math.max(toOrigStart(a) + 0.05, toOrigEnd(b)));
    d.g.update([{op: 'put', coll: 'subtitles', item: {id: d.id, start, end}}]);
    showGuide(sn);
    showBadge(d.edge === 'move' ? fmt(a) : d.edge === 'start' ? fmt(a) : fmt(b), d.edge === 'end' ? b : a);
  }
  function moveCut(d) {
    const ml = minLen(), c0 = d.c0;
    let t = tAt(d.cx) - d.grab, sn;
    if (d.edge === 'start') {
      t = clamp(t, 0, c0.end - ml);
      sn = snap(d.tg, t);
      if (sn && sn.x <= c0.end - ml) t = sn.x; else sn = null;
    } else {
      t = clamp(t, c0.start + ml, S.D);
      sn = snap(d.tg, t);
      if (sn && sn.x >= c0.start + ml) t = sn.x; else sn = null;
    }
    d.moved = true;
    d.g.update([{op: 'put', coll: 'cuts', item: {id: d.id, [d.edge]: r3(t)}}]);
    showGuide(sn);
    const c = store.item('cut', d.id);
    showBadge(c ? fmtDur(c.end - c.start) : '', t);
  }
  function moveTrim(d) {
    const [lo, hi] = d.bounds;
    let t = clamp(d.edgeO + (tAt(d.cx) - d.grab - d.edgeA), lo, hi);
    const sn = snap(d.tg, d.edgeA + (t - d.edgeO));
    if (sn) { const ts = d.edgeO + (sn.x - d.edgeA); if (ts >= lo && ts <= hi) t = ts; }
    showGuide(sn && Math.abs(d.edgeA + t - d.edgeO - sn.x) < 1e-6 ? sn : null);
    d.t = t;
    d.ops = X.trimOps(store.project.cuts, d.pc, d.side, t, minLen());
    const dt = t - d.edgeO, removing = d.side === 'start' ? dt > 0 : dt < 0;
    const ga = Math.min(d.edgeA, d.edgeA + dt), gb = Math.max(d.edgeA, d.edgeA + dt);
    ghost.hidden = Math.abs(dt) < 1e-6;
    if (!ghost.hidden) {
      const x = Math.round(ga * S.pps), w = Math.max(1, Math.round(gb * S.pps) - x);
      ghost.style.transform = `translate(${x}px,0)`;
      ghost.style.width = `${w}px`;
      ghost.className = `tl-ghost ${removing ? 'cut' : 'add'}`;
      ghost.firstChild.textContent = `${removing ? '−' : '+'}${fmtDur(Math.abs(dt))}`;
    }
    showBadge('', 0);
  }
  function finish(d, cancel) {
    showGuide(null);
    showBadge('');
    ghost.hidden = true;
    if (d.kind === 'sub') {
      S.frozen = null;
      if (cancel) d.g.cancel(); else d.g.commit(d.edge === 'move' ? '자막 옮기기' : '자막 길이');
      invalidate(F_MODEL);
    } else if (d.kind === 'cut') {
      if (cancel || !d.moved) d.g.cancel();
      else d.g.commit('컷 조정', [{op: 'put', coll: 'cuts', item: {id: d.id, reviewed: true}}]);
    } else if (d.kind === 'trim') {
      if (cancel || !d.ops?.length) { d.g.cancel(); return; }
      d.g.commit('다듬기', d.ops);
      const keep = d.side === 'start' ? d.t + 1e-4 : d.pc.start + 1e-4, pc = X.pieceAt(store.derived.pieces, keep);
      if (pc) select('piece', pc.id);
    }
    invalidate(F_ITEMS);
  }
  function autoScroll() {
    if (!drag || drag.kind === 'none') return false;
    const x = drag.cx - S.left, m = 28;
    const v = x < m ? -Math.min(24, (m - x) * 0.6) : x > S.W - m ? Math.min(24, (x - S.W + m) * 0.6) : 0;
    if (!v) return false;
    const x0 = S.X0;
    S.X0 += Math.round(v) || Math.sign(v);
    clampView();
    if (S.X0 === x0) return false;
    move(drag);
    dirty |= F_PH;
    if (!raf) raf = requestAnimationFrame(frame);
    return true;
  }

  function onDbl(e) {
    const t = document.elementFromPoint(e.clientX, e.clientY);   // 포인터 캡처 때문에 e.target 은 body 예요
    if (!(t instanceof Element) || t.closest('.tl-edit')) return;
    const sub = t.closest('.tl-sub'), cut = t.closest('.tl-cut'), pin = t.closest('.tl-pin'), seam = t.closest('.tl-seam');
    if (sub) openEditor(sub._k);
    else if (cut && !t.classList.contains('h')) { const c = store.item('cut', cut._k); if (c) store.edit(X.toggleCut(c), c.enabled ? '살리기' : '자르기'); }
    else if (seam) { const sm = S.seams.find(x => x.id === seam._k); if (sm) { store.edit(X.toggleCut(sm.cut), '살리기'); select('cut', sm.cut.id); } }
    else if (pin) memoPopover(pin._k);
  }

  function onWheel(e) {
    if (!(e.target instanceof Element) || e.target.closest('.tl-edit')) return;
    if (e.target.closest('.tl-heads') && !e.ctrlKey && !e.metaKey) return;   // 줄 머리 위에서는 세로 스크롤
    e.preventDefault();
    const k = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? S.W : 1;
    S.left = view.getBoundingClientRect().left;
    if (e.ctrlKey || e.metaKey || e.shiftKey) {   // 핀치 · ⌘+휠 · ⇧+휠: 포인터 기준 확대 (맥은 ⇧+휠을 가로 값으로 보내요)
      const dy = e.shiftKey && Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      zoomTo(S.pps * Math.exp(-clamp(dy * k, -60, 60) * 0.009), clamp(e.clientX - S.left, 0, S.W));
      return;
    }
    scrubBy((Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY) * k);
  }
  // 휠·가로 스크롤 = 탐색: 재생선은 화면 그 자리에 두고 타임라인이 밀리면서 영상도 따라가요 (화면 밖이면 가운데로)
  function scrubBy(dx) {
    let x = phPos() * S.pps - S.X0;
    if (x < 0 || x > S.W) x = S.W / 2;
    const at = clamp(phPos() + dx / S.pps, 0, S.D);
    S.X0 = at * S.pps - x;
    S.userAt = performance.now();
    clampView();
    invalidate(F_VIEW);
    saveView();
    seek(at);
  }

  function onMenu(e) {
    const t = e.target;
    if (!(t instanceof Element) || t.closest('.tl-edit,.tl-heads')) return;
    e.preventDefault();
    S.left = view.getBoundingClientRect().left;
    const sub = t.closest('.tl-sub'), cut = t.closest('.tl-cut'), seam = t.closest('.tl-seam'), pin = t.closest('.tl-pin');
    const at = {x: e.clientX, y: e.clientY}, inSub = s => s && store.time > s.start + 0.05 && store.time < s.end - 0.05;
    let items;
    if (sub) {
      select('sub', sub._k);
      const s = store.item('sub', sub._k);
      items = [{label: '고치기', icon: 'pencil', onClick: () => openEditor(sub._k)},
        {label: '분할', icon: 'split', key: 'mod+b', disabled: !inSub(s), onClick: () => run('split')}, '-',
        {label: '삭제', icon: 'trash', key: 'delete', danger: true, onClick: () => run('delete')}];
    } else if (cut || seam) {
      const c = cut ? store.item('cut', cut._k) : S.seams.find(x => x.id === seam._k)?.cut;
      if (!c) return;
      select('cut', c.id);
      items = [{label: c.enabled ? '살리기' : '자르기', icon: c.enabled ? 'rotate-ccw' : 'scissors', onClick: () => store.edit(X.toggleCut(c), c.enabled ? '살리기' : '자르기')},
        c.type === 'user' ? {label: '컷 삭제', icon: 'trash', key: 'delete', danger: true, onClick: () => run('delete')} : null];
    } else if (pin) {
      select('comment', pin._k);
      items = [{label: '고치기', icon: 'pencil', onClick: () => memoPopover(pin._k)},
        {label: '삭제', icon: 'trash', key: 'delete', danger: true, onClick: () => run('delete')}];
    } else if (t.closest('.tl-mediabox')) {
      const hit = pieceHit(e.clientX - S.left);
      if (!hit) return;
      select('piece', hit.it.id);
      items = [{label: '분할', icon: 'split', key: 'mod+b', onClick: () => run('split')},
        {label: '삭제', icon: 'trash', key: 'delete', danger: true, onClick: () => run('delete')}];
    } else {
      items = [{label: '자막 추가', icon: 'text-plus', key: 't', onClick: () => run('addSub')},
        {label: '메모 추가', icon: 'map-pin-plus', key: 'm', onClick: () => run('addMemo')}];
    }
    menu(at, items);
  }

  body.addEventListener('pointerdown', onDown);
  body.addEventListener('pointermove', onMove);
  body.addEventListener('pointerup', onUp);
  body.addEventListener('pointercancel', e => { if (drag && e.pointerId === drag.pid) cancelDrag(); });
  body.addEventListener('lostpointercapture', e => { if (drag && e.pointerId === drag.pid) onUp(e); });
  body.addEventListener('pointerleave', () => { if (!drag) hover(null); });
  body.addEventListener('dblclick', onDbl);
  body.addEventListener('contextmenu', onMenu);
  body.addEventListener('wheel', onWheel, {passive: false});

  // ---------- 자막 바로 고치기 (더블클릭 · T) ----------
  function openEditor(id, draft) {
    closeEditor(false);
    const s = id ? store.item('sub', id) : null;
    if (id && !s) return;
    const ta = h('textarea.tl-edit', {rows: 1, spellcheck: false, 'aria-label': '자막', placeholder: '자막'});
    ta.value = s ? s.text : '';
    S.edit = {id, draft, ta};
    subLayer.append(ta);
    if (draft && draft.lane >= S.lanes) { invalidate(F_MODEL); flushNow(); }   // 빈 줄이 없으면 아래에 한 줄 더
    placeEditor();
    const fit = () => { ta.style.height = 'auto'; ta.style.height = `${Math.min(84, ta.scrollHeight)}px`; };
    fit();
    ta.focus({preventScroll: true});
    ta.select();
    ta.addEventListener('input', fit);
    ta.addEventListener('keydown', e => {
      e.stopPropagation();
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); closeEditor(true); }
      else if (e.key === 'Escape') { e.preventDefault(); closeEditor(false); }
    });
    ta.addEventListener('blur', () => closeEditor(true));
    ta.addEventListener('pointerdown', e => e.stopPropagation());
  }
  function placeEditor() {
    const ed = S.edit;
    let a, b, lane;
    if (ed.id) {
      const it = S.subs.find(x => x.id === ed.id);
      if (!it) return closeEditor(false);
      ({a, b, lane} = it);
    } else ({a, b, lane} = ed.draft);
    // 블록 폭(최소 ED_MIN)에서 같은 줄 다음 자막 앞까지만 넓혀요 — 이웃 자막을 덮지 않게
    let next = Infinity;
    for (const it of S.subs) if (it.lane === lane && it.id !== ed.id && it.a > a + 1e-6 && it.a < next) next = it.a;
    const x = Math.round(a * S.pps) + 1, own = Math.round(b * S.pps) - 1 - x;
    const w = Math.max(own, ED_MIN, Math.min(ED_MAX, Math.round(next * S.pps) - 3 - x));
    ed.ta.style.transform = `translate(${x}px,${lane * S.laneH + 1}px)`;
    ed.ta.style.width = `${w}px`;
  }
  function closeEditor(commit) {
    const ed = S.edit;
    if (!ed) return;
    S.edit = null;
    if (ed.draft) invalidate(F_MODEL);   // 새 자막 칸 때문에 늘린 줄을 되돌려요
    const v = ed.ta.value.trim();
    ed.ta.remove();
    if (!commit || !v) return;
    if (ed.id) {
      const s = store.item('sub', ed.id);
      if (s && v !== s.text) store.edit([{op: 'put', coll: 'subtitles', item: {id: ed.id, text: v}}], '자막 고치기');
      return;
    }
    const {a, b, speaker} = ed.draft, start = r3(toOrigStart(a)), end = r3(Math.max(start + 0.1, toOrigEnd(b)));
    const [op] = store.edit([{op: 'put', coll: 'subtitles', item: {start, end, text: v, speaker, locked: true}}], '자막 추가');   // 손으로 넣은 시간 그대로 (자동 타이밍에서 빼요)
    if (op) store.select('sub', op.item.id, {src: 'timeline', reveal: true});
  }

  // ---------- 메모 (M · 핀 더블클릭) ----------
  function memoPopover(id) {
    const c = id ? store.item('comment', id) : null, t = c ? c.t : store.time;
    const ax = store.toAxis(t);
    ensureVisible(ax, ax);
    flushNow();
    const r = ruler.getBoundingClientRect();
    const ta = h('textarea.input.tl-memo-in', {rows: 2, placeholder: '메모', autofocus: true, 'aria-label': '메모'});
    ta.value = c?.text || '';
    const done = () => {
      const v = ta.value.trim();
      pop?.close();
      if (c) { if (v && v !== c.text) store.edit([{op: 'put', coll: 'comments', item: {id: c.id, text: v}}], '메모 고치기'); return; }
      if (!v) return;
      const [op] = store.edit([{op: 'put', coll: 'comments', item: {t: r3(t), text: v}}], '메모 추가');
      if (op) store.select('comment', op.item.id, {src: 'timeline', reveal: true});
    };
    ta.addEventListener('keydown', e => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); done(); }
    });
    const pop = popover({x: r.left + Math.round(ax * S.pps) - S.X0, y: r.bottom}, h('div.tl-memo', {}, ta,
      h('div.tl-memo-foot', {}, h('span.tc', {}, fmt(ax)), btn(c ? '저장' : '추가', {cls: 'primary xs', onClick: done}))), {place: 'bottom', align: 'start'});
  }

  // ---------- 명령 ----------
  const cmds = {
    split() {
      const t = store.time, sel = store.sel, under = s => s && t > s.start + 0.05 && t < s.end - 0.05;
      if (sel?.kind === 'sub') {
        let s = store.item('sub', sel.id);
        if (!under(s)) s = store.project.subtitles.find(under);
        if (s) {
          store.action('자막 분할', () => api.splitSub(store.name, s.id, r3(t))).catch(e => toast(e.message, {kind: 'error'}));
          return;
        }
      }
      const ops = X.splitOps(store.project.splits, store.derived.pieces, t, minLen());
      if (!ops) return toast('여기선 나눌 수 없어요', {timeout: 1600});
      store.edit(ops, '분할');
      const pc = X.pieceAt(store.derived.pieces, t + 1e-4);
      if (pc) select('piece', pc.id);
    },
    trimStart: () => trimTo('start'),
    trimEnd: () => trimTo('end'),
    delete() {
      const sel = store.sel, it = store.selItem;
      if (!sel || !it) return false;
      if (sel.kind === 'sub') store.edit([{op: 'del', coll: 'subtitles', id: it.id}], '자막 삭제');
      else if (sel.kind === 'comment') store.edit([{op: 'del', coll: 'comments', id: it.id}], '메모 삭제');
      else if (sel.kind === 'piece') store.edit(X.deletePiece(store.project.splits, it), '클립 삭제');
      else if (sel.kind === 'cut') {
        store.edit(X.deleteCut(it), it.type === 'user' ? '컷 삭제' : '살리기');
        if (it.type !== 'user') return;
      } else return false;
      select(null);
    },
    addSub() {
      if (!store.project.sources.length) return;
      let a = phPos(), b = Math.min(S.D, a + 2);
      if (b - a < 0.5) a = Math.max(0, b - 2);
      if (b - a < 0.1) return;
      ensureVisible(a, b);
      const cur = store.sel?.kind === 'sub' ? store.selItem : null;
      flushNow();
      // 그 자리에서 비어 있는 첫 줄 (다 차 있으면 아래 새 줄) — 새 줄이 무엇과 겹치는지 보이게
      const e = Math.max(b, a + ED_MIN / S.pps);
      let lane = 0;
      while (S.subs.some(it => it.lane === lane && it.a < e - 1e-6 && it.b > a + 1e-6)) lane++;
      openEditor(null, {a, b, lane, speaker: cur?.speaker ?? null});
    },
    addMemo() {   // 메모 탭에서는 메모 패널이 목록에 바로 넣어요
      if (store.tab === 'memo') return false;
      if (store.project.sources.length) memoPopover(null);
    },
    zoomIn: () => zoomTo(S.pps * 1.5),
    zoomOut: () => zoomTo(S.pps / 1.5),
    fit() { S.fit = true; clampView(); saveView(); invalidate(F_VIEW); updateBar(); },
  };
  function trimTo(side) {
    const ops = X.trimToOps(store.derived.pieces, store.time, side, minLen());
    if (!ops) return toast('여기선 자를 수 없어요', {timeout: 1600});
    store.edit(ops, side === 'start' ? '앞부분 자르기' : '뒷부분 자르기');
  }
  const run = name => { closeEditor(true); return cmds[name]?.(); };
  store.on('cmd', c => {
    if (c.handled) return;
    if (c.name === 'escape') {
      if (drag?.started) { cancelDrag(); c.handled = true; }
      return;
    }
    if (!Object.hasOwn(cmds, c.name)) return;
    if (run(c.name) !== false) c.handled = true;
  });

  // ---------- 저장소 이벤트 ----------
  store.on('project', e => {
    if (!e.colls || e.colls.has('sources') || e.src === 'load' || e.src === 'external') syncAssets();
    const change = X.modelChange(e);
    if (change === 'model') invalidate(F_MODEL);
    else if (change === 'items') {
      const E = t => store.toAxis(t);
      S.pins = store.project.comments.map(c => ({id: c.id, c, x: E(c.t)})).sort((a, b) => a.x - b.x);
      invalidate(F_ITEMS);
    }
  });
  store.on('sel', (sel, prev, opts) => {
    invalidate(F_ITEMS);
    updateBar();
    if (sel && opts?.src !== 'timeline') reveal(sel);
  });
  store.on('time', (t, src) => {
    invalidate(F_PH);
    if (store.playing || drag || src === 'timeline' || src === 'select' || src === 'jump') return;
    const x = phX();
    if (x < 16 || x > S.W - 16) { const p = phPos(); src === 'step' ? ensureVisible(p, p, 16) : ensureVisible(p, p, S.W / 2 - 1); }
  });
  store.on('play', v => { if (v) S.userAt = 0; invalidate(F_PH); });
  store.on('view', (keys, src) => {
    if (src === 'timeline') return;
    if (keys.includes('axis')) {
      const old = S.axis === 'edit' ? store.map.toEdited(store.time) : store.time;
      const u = Math.round(old * S.pps) - S.X0, keepX = u >= 0 && u <= S.W ? u : S.W / 2;
      closeEditor(true);
      S.hover = null;
      buildModel();
      if (!S.fit) S.X0 = Math.round(phPos() * S.pps - keepX);
      invalidate(F_MODEL | F_VIEW);
    }
    updateBar();
  });
  store.on('history', updateBar);
  store.on('external', () => invalidate(F_ITEMS));
  new ResizeObserver(() => invalidate(F_LAYOUT | F_VIEW)).observe(body);

  // ---------- 시작 ----------
  syncAssets();
  buildModel();
  layout();
  const saved = prefs.get(`tl:${store.name}`, {});
  if (+saved.pps > 0) {
    S.fit = false;
    S.pps = clamp(+saved.pps, minPps(), MAX_PPS);
    S.X0 = Math.round(phPos() * S.pps - S.W / 3);
  }
  clampView();
  updateBar();
  frame();

  // 확인용 (콘솔 · 자동 점검)
  window.__tl = {S, stats, zoomTo, scrollBy, invalidate, frame, cmds, snapTargets, X};
}

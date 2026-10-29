// 미리보기 (spec 11.3): 캔버스 비율 무대 · <video> 두 개를 번갈아 써서 잘린 곳을 건너뛰는 매끄러운 편집본 재생 ·
// 컷 듣기(앞뒤 1.5초) · 지금 보이는 자막 전부 겹쳐 보이기 (누르면 선택, 끌면 이동, 두 번 누르면 고치기).
// ── 다른 모듈과의 약속 ──
// 듣기: store.command('preview', {id, start, end, from?, to?} | 컷id) → 그 창을 재생하고 컷 부분을 표시한 뒤 멈춰요 (handled=true).
//   같은 구간을 0.4초 안에 또 부르면 한 번만 해요. store.player.preview(값) · store.player.stop() 도 돼요.
// store.command('listen', 값) 은 컷 패널이 받아요 (선택한 컷 듣기/멈춤). 아무도 안 받으면 재생기가 대신 해요.
// 듣는 중: store.previewing = {id, start, end, from, to} | null, 바뀔 때 store.emit('previewing', 값)
// 시간: 재생 중에는 store.setTime(t, 'player') 를 30Hz 정도로 불러요 (원본 초). 다른 곳에서 바꾼 시간은 따라가요.
// 파일: store.preview 주소가 바뀌면 (다시 만든 preview.mp4) 두 video 를 바꾸고 보던 시간·재생을 이어 가요.
import { h, iconBtn, tip, toast, copyText, clamp, ring } from './ui.js';
import { icon } from './icons.js';
import { fmt, fmtDur } from './time.js';
import { prefs } from './store.js';
import { noFocus } from './panels/common.js';

const SELF = 'player';            // 재생기가 스스로 낸 이벤트 (받을 때 무시)
const LEAD = 1.5;                 // 듣기: 컷 앞뒤 여유 (초)
const SYNC_MS = 30;               // store.time 갱신 간격
const SNAP_PX = 7, SAFE = 5, CHIP_MS = 4000, EPS = 1e-3;
const SNAP_Y = [88, 50];

function loadCss(name) {
  const id = `css-${name}`;
  let link = document.getElementById(id);
  if (!link) { link = h('link', {rel: 'stylesheet', id, href: `/ui/css/${name}.css`}); document.head.append(link); }
  if (link.sheet) return Promise.resolve();
  return new Promise(r => { link.addEventListener('load', r, {once: true}); link.addEventListener('error', r, {once: true}); setTimeout(r, 1500); });
}
// '잘린 곳도 재생' 아이콘: 사선 그은 가위 (가위 날이 대각선이라 돌려 그려서 사선과 안 겹치게)
function scissorsOff(size) {
  const s = icon('scissors', size);
  s.innerHTML = `<g transform="translate(12 12) scale(.88) rotate(-45) translate(-12 -12)" stroke-width="2">${s.innerHTML}</g><path d="m3 3 18 18"/>`;
  return s;
}

export async function mount(el, store) {
  await loadCss('player');
  el.classList.add('pl');

  // ---------- 무대 ----------
  const mkVideo = () => {
    const v = h('video.pl-video', {preload: 'auto', playsInline: true, disablePictureInPicture: true, tabindex: -1});
    v.muted = true;
    v.disableRemotePlayback = true;
    return v;
  };
  const vids = [mkVideo(), mkVideo()];
  const subsLayer = h('div.pl-subs');
  const guideX = h('i.pl-guide.v'), guideY = h('i.pl-guide.h');
  const chip = h('button.pl-chip', {type: 'button', hidden: true, tip: '이 위치를 화자의 모든 줄에'}, h('span.dot'), h('span', {}, '화자 전체'));
  const pvCut = h('i.cut'), pvHead = h('i.head');
  const pvBar = h('div.pl-pv', {'aria-hidden': 'true'}, pvCut, pvHead);
  const pvPill = h('span.pill.pl-pv-pill', {hidden: true});
  const phRing = ring(-1, 26), phIco = h('span.pl-ph-ico', {}, phRing), phText = h('span.t');
  const ph = h('div.pl-ph', {}, phIco, h('div.pl-ph-row', {}, phText));
  const stage = h('div.pl-stage', {}, vids, subsLayer, h('div.pl-safe'), guideX, guideY, pvBar, pvPill, chip, ph);
  const view = h('div.pl-view', {}, stage);

  // ---------- 조작 줄 ----------
  const tcCur = h('span.pl-tc-cur'), tcTotal = h('span.pl-tc-total');
  const tcOrig = h('button.pl-orig', {type: 'button', onclick: copyOrig});
  tip(tcOrig, '원본 시간 복사');
  const playBtn = iconBtn('play', '재생', () => store.togglePlay('player-btn'), {key: 'space', cls: 'ghost pl-play', size: 20});
  const prevBtn = iconBtn('skip-back', '이전 편집점', () => jumpPoint(-1));
  const nextBtn = iconBtn('skip-forward', '다음 편집점', () => jumpPoint(1));
  let vol = clamp(+prefs.get('volume', 1) || 0, 0, 1), muted = !!prefs.get('muted', false);
  const volBtn = iconBtn('volume', '소리', () => {
    if (muted || vol === 0) { muted = false; if (vol === 0) vol = 0.6; } else muted = true;
    volRange.value = muted ? 0 : vol;
    applyVol(true);
  });
  const volRange = h('input.range.pl-vol-range', {type: 'range', min: 0, max: 1, step: 0.01, value: vol, 'aria-label': '음량'});
  volRange.addEventListener('input', () => { vol = +volRange.value; muted = vol === 0; applyVol(true); });
  const allBtn = iconBtn('scissors', '잘린 곳도 재생', () => store.setView({playAll: !store.playAll}), {pressed: store.playAll});
  allBtn.replaceChildren(scissorsOff(18));
  const allChip = h('span.pill.accent.pl-all', {hidden: true}, '전체 재생');
  const fsBtn = iconBtn('fullscreen', '전체 화면', toggleFs);
  const bar = h('div.pl-bar', {},
    h('div.pl-tc', {}, tcCur, h('span.pl-tc-sep', {}, '/'), tcTotal, tcOrig, allChip),
    h('div.pl-mid', {}, prevBtn, playBtn, nextBtn),
    h('div.pl-right', {}, h('div.pl-vol', {}, volBtn, volRange), allBtn, fsBtn));
  noFocus(bar);   // 마우스로 누른 버튼에 포커스 테두리가 남지 않게 (단축키는 계속 전체에)
  el.append(view, bar);

  // ---------- 재생 상태 ----------
  // base: 지금 불러온 store.preview 주소 · resume: 파일을 바꾸느라 멈췄으면 준비되는 대로 다시 재생
  // 영상은 소스(클립)마다 따로 열어요: 원본을 그대로 (크롬이 못 여는 형식만 변환본). video 마다 _url·_off(소스 시작 = 원본 축 위치)
  const P = {cur: 0, si: -1, hold: null, holdAt: 0, ready: false, loading: false, preview: null, base: null, bust: '', resume: false, retry: 0};
  let spanCache = null, raf = 0, lastSync = 0, paintRaf = 0, preseekT = 0;
  const active = () => vids[P.cur];
  const other = () => vids[P.cur ^ 1];

  // 재생할 구간들 [[s,e]] (원본 축): 듣기 = 그 창 하나, 전부 재생 = 처음부터 끝, 보통 = 남는 구간 (이어진 것은 합쳐요)
  function spans() {
    if (spanCache) return spanCache;
    if (P.preview) return (spanCache = atSources([[P.preview.from, P.preview.to]]));
    if (store.playAll) return (spanCache = store.total > 0 ? atSources([[0, store.total]]) : []);
    const out = [];
    for (const [s, e] of store.derived.keep) {
      const L = out[out.length - 1];
      if (L && s - L[1] < EPS) L[1] = e; else out.push([s, e]);
    }
    return (spanCache = atSources(out));
  }
  // 한 video 는 한 파일만 재생하니까 소스 경계에서 구간을 나눠요
  function atSources(list) {
    const bounds = srcList().slice(1).map(s => s.offset);
    if (!bounds.length) return list;
    const out = [];
    for (let [s, e] of list) {
      for (const x of bounds) if (x > s + EPS && x < e - EPS) { out.push([s, x]); s = x; }
      out.push([s, e]);
    }
    return out;
  }
  const srcList = () => store.project?.sources || [];
  function srcAt(t) {
    let r = null;
    for (const s of srcList()) { if (!r || t >= s.offset - EPS) r = s; else break; }
    return r;
  }
  function playUrl(s) {
    const u = s && store.assets?.[s.id]?.play;
    return u ? (P.bust ? `${u}${u.includes('?') ? '&' : '?'}r=${P.bust}` : u) : null;
  }
  const noMedia = t => !playUrl(srcAt(t));
  const vt = v => (v._off || 0) + v.currentTime;   // video 위치 → 원본 축
  // t 를 담고 있거나 t 다음에 오는 구간 번호 (없으면 length)
  function spanAt(t) {
    const sp = spans();
    let lo = 0, hi = sp.length - 1, r = sp.length;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (sp[m][1] > t + 1e-4) { r = m; hi = m - 1; } else lo = m + 1; }
    return r;
  }

  // ---------- video 다루기 ----------
  // 탐색은 겹치지 않게: 진행 중이면 끝난 뒤 마지막 값으로 한 번 더 (끄는 동안에도 화면이 따라와요)
  function seek(v, t) {
    const s = srcAt(t), url = playUrl(s);
    if (!url) return;
    v._want = t;
    if (v._url !== url) {   // 다른 클립 → 그 파일을 열고 열리면 그 자리부터
      v._url = url;
      v._off = s.offset;
      v._fps = s.fps || 30;
      v.src = url;
      v.currentTime = Math.max(0, t - s.offset);
      return;
    }
    const L = Math.max(0, t - v._off);
    if (v.seeking || v.readyState === 0) { if (v.readyState === 0) v.currentTime = L; return; }
    if (Math.abs(v.currentTime - L) < 5e-4) { v._want = null; return; }
    v.currentTime = L;
  }
  const atTarget = (v, t) => !v.seeking && v._want == null && Math.abs(vt(v) - t) < 0.02 && v.readyState >= 2;
  const heading = (v, t) => Math.abs((v._want ?? vt(v)) - t) < 0.02;

  for (const v of vids) {
    v.addEventListener('seeked', () => {
      if (v._want != null && Math.abs(vt(v) - v._want) > 5e-4) { v.currentTime = Math.max(0, v._want - v._off); return; }
      v._want = null;
      if (P.hold != null && v === other()) swapTo(P.hold);
    });
    v.addEventListener('canplay', () => { if (P.hold != null && v === other()) swapTo(P.hold); });
    v.addEventListener('loadedmetadata', () => {
      if (v._want != null && !v.seeking && Math.abs(vt(v) - v._want) < 5e-4) v._want = null;   // 불러오기 전 탐색은 seeked 가 안 올 수 있어요
      if (v !== active() || !P.loading) return;   // 처음 열 때만: 재생 중 다른 클립으로 넘어갈 땐 하던 대로
      P.loading = false;
      P.ready = true;
      P.retry = 0;
      seek(v, store.time);
      queuePreseek();
      paintPh();
      if (P.resume && !store.playing) store.setPlaying(true, SELF);
      P.resume = false;
      if (store.playing) startPlay();
    });
    v.addEventListener('error', () => {
      if (v !== active()) return;
      P.ready = false;
      P.resume = false;
      if (store.playing) store.setPlaying(false, SELF);
      paintPh();
      retryLater();
    });
    v.addEventListener('ended', () => {   // 클립 파일 끝: 다음 구간이 있으면 넘어가요
      if (v !== active() || !store.playing || P.hold != null) return;
      if (P.si + 1 < spans().length) swapTo(P.si + 1); else finish();
    });
  }

  function playVideo(v) {
    v.muted = muted || vol === 0;
    v.volume = vol;
    const p = v.play();
    if (p) p.catch(err => {
      if (err?.name !== 'NotAllowedError' || v !== active()) return;
      v.muted = true;   // 자동 재생 막힘: 소리 없이라도
      v.play().catch(() => {});
    });
    watchFrames(v);
  }
  // 프레임이 그려질 때마다 구간 끝을 봐요 (requestVideoFrameCallback, 없으면 rAF 가 대신)
  function watchFrames(v) {
    if (!('requestVideoFrameCallback' in v)) return;
    if (v._fcb) v.cancelVideoFrameCallback(v._fcb);
    const cb = (now, meta) => {
      v._fcb = 0;
      if (v !== active() || !store.playing) return;
      if (P.hold == null && !v.seeking) checkEnd((v._off || 0) + meta.mediaTime, true);
      if (v === active() && store.playing && !v.paused) v._fcb = v.requestVideoFrameCallback(cb);
    };
    v._fcb = v.requestVideoFrameCallback(cb);
  }
  function show(i) {
    vids[i].classList.add('on');
    vids[i ^ 1].classList.remove('on');
  }
  function applyVol(save) {
    const a = active();
    a.volume = vol;
    a.muted = muted || vol === 0;
    other().muted = true;
    volBtn.replaceChildren(icon(muted || vol === 0 ? 'volume-x' : vol < 0.5 ? 'volume-low' : 'volume', 18));
    tip(volBtn, muted || vol === 0 ? '소리 켜기' : '소리 끄기');
    volRange.style.setProperty('--p', `${(muted ? 0 : vol) * 100}%`);
    if (save) { prefs.set('volume', vol); prefs.set('muted', muted); }
  }

  // ---------- 재생 ----------
  function startPlay(from) {
    if (!P.ready) { store.setPlaying(false, SELF); paintPh(); return; }
    const sp = spans();
    if (!sp.length) { store.setPlaying(false, SELF); return; }
    let t = from ?? store.time, i = spanAt(t);
    if (i >= sp.length || (i === sp.length - 1 && t >= sp[i][1] - 0.05)) { i = 0; t = sp[0][0]; }   // 끝이면 처음부터
    if (t < sp[i][0]) t = sp[i][0];
    P.si = i;
    P.hold = null;
    if (!heading(active(), t) && atTarget(other(), t)) P.cur ^= 1;   // 숨은 쪽이 이미 거기 있으면 바로 바꿔요
    const v = active();
    if (!heading(v, t) || v._want != null) seek(v, t);
    show(P.cur);
    playVideo(v);
    other().pause();
    preseek(i + 1);
    lastSync = 0;
    if (t !== store.time) store.setTime(t, SELF);
    loop();
  }
  function stopPlay() {
    cancelAnimationFrame(raf);
    raf = 0;
    const holding = P.hold != null;
    P.hold = null;
    for (const v of vids) v.pause();
    const sp = spans();
    if (P.ready && !active().seeking) {
      const t = holding && sp[P.si] ? sp[P.si][1] : vt(active());
      store.setTime(t, SELF);
    }
    if (P.preview) endPreview();
    queuePreseek();
    paintNow();
  }
  // 끝까지 갔어요
  function finish() {
    const sp = spans(), end = sp.length ? sp[sp.length - 1][1] : 0;
    cancelAnimationFrame(raf);
    raf = 0;
    P.hold = null;
    for (const v of vids) v.pause();
    store.setTime(end, SELF);
    if (P.preview) endPreview();
    store.setPlaying(false, SELF);
    paintPlay();
    paintNow();
  }
  // 구간 끝에 닿았는지: 프레임 콜백이면 다음 프레임이 끝을 넘을 때, rAF 면 거의 닿았을 때
  function checkEnd(t, byFrame) {
    const sp = spans(), s = sp[P.si];
    if (!s) { P.si = spanAt(t); return; }
    if (t < s[0] - 0.05) return;   // 아직 탐색 중 (앞 구간 시간)
    if (byFrame ? t + 1 / (active()._fps || 30) - 0.004 < s[1] : t < s[1] - 0.008) return;
    if (P.si + 1 >= sp.length) finish();
    else swapTo(P.si + 1);
  }
  // 다음 구간으로: 미리 그 자리에 가 있던 다른 video 를 보이고 재생해요
  function swapTo(i) {
    const sp = spans();
    if (!store.playing || !sp[i]) return;
    const a = active(), b = other(), t = sp[i][0] + EPS;
    if (!atTarget(b, t)) {
      if (P.hold == null) {
        P.hold = i;
        P.holdAt = performance.now();
        a.pause();
        if (!heading(b, t)) seek(b, t);
      } else if (performance.now() - P.holdAt > 1500) {   // 너무 오래 걸리면 지금 video 로 바로 가요
        P.hold = null;
        P.si = i;
        seek(a, t);
        playVideo(a);
        preseek(i + 1);
      }
      return;
    }
    P.hold = null;
    P.si = i;
    P.cur ^= 1;
    show(P.cur);
    playVideo(b);
    a.pause();
    a.muted = true;
    preseek(i + 1);
  }
  // 숨은 video 를 다음 구간 시작으로 미리 보내 둬요
  function preseek(i) {
    const sp = spans(), b = other();
    b.pause();
    b.muted = true;
    if (P.ready && sp[i]) seek(b, sp[i][0] + EPS);
  }
  function queuePreseek() {
    clearTimeout(preseekT);
    preseekT = setTimeout(() => { if (!store.playing && P.ready) preseek(spanAt(store.time) + (inSpan(store.time) ? 1 : 0)); }, 180);
  }
  const inSpan = t => { const sp = spans(), i = spanAt(t); return !!sp[i] && t >= sp[i][0] - EPS; };
  // 재생 중에 다른 곳으로 (타임라인 클릭 · 편집점 이동)
  function jump(t) {
    const sp = spans(), from = active()._want ?? vt(active());
    let i = spanAt(t);
    if (i > 0 && t < from - EPS && (i >= sp.length || t < sp[i][0])) {   // 뒤로 가다 잘린 곳에 닿으면 앞 구간 끝으로 (다음 구간으로 밀어내지 않게)
      i -= 1;
      t = Math.max(sp[i][0], sp[i][1] - 0.04);
    }
    if (i >= sp.length) { finish(); return; }
    if (t < sp[i][0]) t = sp[i][0];
    P.si = i;
    P.hold = null;
    const v = active();
    seek(v, t);
    show(P.cur);
    if (v.paused) playVideo(v);
    preseek(i + 1);
    lastSync = 0;
  }
  // 컷이 바뀌었을 때: 지금 자리가 잘렸으면 다음 구간으로, 숨은 video 도 다시 준비
  function replan() {
    if (!store.playing || P.hold != null) return;
    const v = active(), t = v._want ?? vt(v), sp = spans(), i = spanAt(t);
    if (i >= sp.length) { finish(); return; }
    P.si = i;
    if (t < sp[i][0] - EPS) seek(v, sp[i][0] + EPS);
    preseek(i + 1);
  }
  function loop() {
    if (!raf) raf = requestAnimationFrame(tick);
  }
  function tick(now) {
    raf = 0;
    if (!store.playing) return;
    const a = active();
    if (P.hold != null) swapTo(P.hold);
    else if (!a.seeking && a._want == null) checkEnd(vt(a), false);
    if (!store.playing) return;
    if (now - lastSync >= SYNC_MS) {
      lastSync = now;
      const v = active(), sp = spans();   // 바뀌었을 수 있어요
      const t = P.hold != null ? (sp[P.si]?.[1] ?? store.time) : (v._want ?? vt(v));
      store.setTime(t, SELF);
    }
    raf = requestAnimationFrame(tick);
  }

  // ---------- 듣기 (컷 앞뒤 1.5초) ----------
  function resolveRange(arg) {
    if (arg == null || arg === '') {
      const s = store.sel;
      return s?.kind === 'cut' ? store.item('cut', s.id) : null;
    }
    if (typeof arg === 'string') return store.item('cut', arg);
    if (typeof arg === 'object') {
      if (arg.id && (arg.start == null || arg.end == null)) return store.item('cut', arg.id);
      if (Number.isFinite(+arg.start) && Number.isFinite(+arg.end) && +arg.end > +arg.start) return arg;
    }
    return null;
  }
  const num = (v, d) => Number.isFinite(+v) && v !== null && v !== '' ? +v : d;
  const sameRange = (a, b) => !!a && !!b && ((a.id && a.id === b.id) || (Math.abs(a.start - b.start) < EPS && Math.abs(a.end - b.end) < EPS));
  function preview(arg) {
    const r = resolveRange(arg);
    if (!r) return false;
    const T = store.total, from = clamp(num(r.from, +r.start - LEAD), 0, +r.start), to = clamp(num(r.to, +r.end + LEAD), +r.end, T);
    if (!(to > from)) return false;
    if (P.preview && sameRange(P.preview, r) && performance.now() - P.preview.at < 400) return true;
    P.preview = {id: r.id ?? null, start: +r.start, end: +r.end, from, to, at: performance.now()};
    spanCache = null;
    store.previewing = {...P.preview};
    store.emit('previewing', store.previewing);
    paintPreview();
    if (!P.ready) { endPreview(); paintPh(); return false; }
    const was = store.playing;
    store.setTime(from, SELF);
    if (!was) store.setPlaying(true, SELF);
    startPlay(from);
    paintPlay();
    return true;
  }
  function endPreview() {
    if (!P.preview) return;
    P.preview = null;
    spanCache = null;
    store.previewing = null;
    store.emit('previewing', null);
    paintPreview();
  }
  function stopPreview() {
    if (!P.preview) return;
    if (store.playing) store.setPlaying(false, 'player-btn');   // 'play' 핸들러가 멈추고 끝내요
    else endPreview();
  }
  store.on('cmd:preview', ctx => { if (preview(ctx.arg)) ctx.handled = true; });
  store.on('cmd:listen', ctx => {   // 컷 패널이 없거나 안 받을 때만 (같은 이벤트의 다른 처리가 다 끝난 뒤 봐요)
    if (ctx.handled) return;
    queueMicrotask(() => {
      const r = ctx.handled ? null : resolveRange(ctx.arg);
      if (!r) return;
      ctx.handled = true;
      if (P.preview && sameRange(P.preview, r) && store.playing && performance.now() - P.preview.at > 400) stopPreview();
      else preview(r);
    });
  });
  store.player = {preview, stop: stopPreview, get previewing() { return P.preview ? {...P.preview} : null; }};

  // ---------- 자막 겹치기 ----------
  const live = new Map();   // 자막 id → 요소
  let subIdx = null, editing = null, drag = null, chipFor = null, chipT = 0;
  function visibleSubs(t) {
    const subs = store.project?.subtitles || [];
    if (!subIdx || subIdx.subs !== subs || subIdx.n !== subs.length) {
      let m = 0;
      for (const s of subs) if (s.end - s.start > m) m = s.end - s.start;
      subIdx = {subs, n: subs.length, max: m};
    }
    let lo = 0, hi = subs.length - 1, r = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (subs[m].start <= t) { r = m; lo = m + 1; } else hi = m - 1; }
    const out = [];
    for (let i = r; i >= 0; i--) {
      const s = subs[i];
      if (s.start < t - subIdx.max - EPS) break;
      if (t < s.end && s.text) out.push(s);
    }
    return out.reverse();
  }
  function paintSubs() {
    const t = store.time, selId = store.sel?.kind === 'sub' ? store.sel.id : null, seen = new Set();
    for (const s of visibleSubs(t)) {
      seen.add(s.id);
      let e = live.get(s.id);
      if (!e) { e = h('div.pl-sub', {dataset: {id: s.id}}); subsLayer.append(e); live.set(s.id, e); }
      if (editing?.id === s.id) continue;
      const pos = store.posOf(s), isSel = s.id === selId;
      const key = `${s.text}\u0001${pos.x}\u0001${pos.y}\u0001${isSel}`;
      if (e._k === key) continue;
      e._k = key;
      e.textContent = s.text;
      e.style.left = `${pos.x}%`;
      e.style.top = `${pos.y}%`;
      e.classList.toggle('sel', isSel);
    }
    for (const [id, e] of live) {
      if (seen.has(id) || editing?.id === id || drag?.id === id) continue;
      e.remove();
      live.delete(id);
    }
  }

  // 끌어서 옮기기 (누른 자리 그대로 · 가운데·아래·중간 맞춤 · 안전 영역 5% · Alt 는 맞춤 없이)
  subsLayer.addEventListener('pointerdown', e => {
    const se = e.target instanceof Element ? e.target.closest('.pl-sub') : null;
    if (!se || e.button !== 0 || se.classList.contains('editing')) return;
    const id = se.dataset.id, sub = store.item('sub', id);
    if (!sub) return;
    e.preventDefault();
    hideChip();
    store.select('sub', id);
    const R = stage.getBoundingClientRect(), pos = store.posOf(sub), box = se.getBoundingClientRect();
    drag = {id, el: se, x0: e.clientX, y0: e.clientY, off: {x: e.clientX - (R.left + pos.x / 100 * R.width), y: e.clientY - (R.top + pos.y / 100 * R.height)},
      R, hw: box.width / 2 / R.width * 100, hh: box.height / 2 / R.height * 100, g: null, last: null, ev: null, raf: 0};
    se.setPointerCapture(e.pointerId);
    const move = ev => {
      const d = drag;
      if (!d || d.dead) return;
      if (!d.g) {
        if (Math.hypot(ev.clientX - d.x0, ev.clientY - d.y0) < 3) return;
        if (store.playing) store.setPlaying(false, 'player-btn');
        d.g = store.gesture();
        stage.classList.add('dragging');
      }
      d.ev = {x: ev.clientX, y: ev.clientY, alt: ev.altKey};
      if (!d.raf) d.raf = requestAnimationFrame(() => { d.raf = 0; dragTo(d); });
    };
    const up = ev => {
      se.removeEventListener('pointermove', move);
      se.removeEventListener('pointerup', up);
      se.removeEventListener('pointercancel', up);
      const d = drag;
      drag = null;
      if (!d) return;
      cancelAnimationFrame(d.raf);
      if (d.dead) return;
      if (d.g && d.ev && ev.type !== 'pointercancel') dragTo(d);
      stage.classList.remove('dragging');
      guides(null, null);
      if (!d.g) return;
      if (ev.type === 'pointercancel') { d.g.cancel(); paintNow(); return; }
      d.g.commit('자막 위치');
      const s = store.item('sub', d.id);
      if (s?.speaker && d.last) showChip(d.id);
      paintNow();
    };
    se.addEventListener('pointermove', move);
    se.addEventListener('pointerup', up);
    se.addEventListener('pointercancel', up);
  });
  function dragTo(d) {
    const {R} = d, ev = d.ev;
    const clampC = (v, half) => { const lo = SAFE + half, hi = 100 - SAFE - half; return lo > hi ? 50 : clamp(v, lo, hi); };
    let x = clampC((ev.x - d.off.x - R.left) / R.width * 100, d.hw);
    let y = clampC((ev.y - d.off.y - R.top) / R.height * 100, d.hh);
    let gx = null, gy = null;
    if (!ev.alt) {
      const tx = SNAP_PX / R.width * 100, ty = SNAP_PX / R.height * 100;
      if (Math.abs(x - 50) < tx && clampC(50, d.hw) === 50) { x = 50; gx = 50; }
      for (const v of SNAP_Y) if (Math.abs(y - v) < ty && clampC(v, d.hh) === v) { y = v; gy = v; break; }
    }
    guides(gx, gy);
    const pos = {x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10};
    if (d.last && d.last.x === pos.x && d.last.y === pos.y) return;
    d.last = pos;
    d.g.update([{op: 'put', coll: 'subtitles', item: {id: d.id, pos}}]);
  }
  function guides(x, y) {
    guideX.classList.toggle('on', x != null);
    guideY.classList.toggle('on', y != null);
    if (y != null) guideY.style.top = `${y}%`;
  }
  store.on('cmd:escape', ctx => {   // 끄는 중 Esc → 취소
    if (!drag?.g || drag.dead) return;
    drag.g.cancel();
    drag.dead = true;
    cancelAnimationFrame(drag.raf);
    stage.classList.remove('dragging');
    guides(null, null);
    ctx.handled = true;
    paintNow();
  });

  // '화자 전체' 칩: 옮긴 위치를 화자 기본 위치로 (자기 위치가 없는 그 화자의 줄이 모두 따라와요)
  function showChip(id) {
    const s = store.item('sub', id), sp = store.speaker(s?.speaker);
    if (!s || !sp) return;
    chipFor = id;
    const pos = store.posOf(s), e = live.get(id), R = stage.getBoundingClientRect();
    const hh = e ? e.getBoundingClientRect().height / 2 / R.height * 100 : 6;
    const below = pos.y + hh + 1;
    chip.style.left = `${clamp(pos.x, 12, 88)}%`;
    chip.style.top = below < 90 ? `${below}%` : `${Math.max(2, pos.y - hh - 9)}%`;
    chip.firstChild.style.setProperty('--dot', store.speakerColor(sp.id));
    chip.hidden = false;
    clearTimeout(chipT);
    chipT = setTimeout(hideChip, CHIP_MS);
  }
  function hideChip() { clearTimeout(chipT); chip.hidden = true; chipFor = null; }
  chip.addEventListener('pointerdown', e => e.stopPropagation());
  chip.addEventListener('click', () => {
    const s = store.item('sub', chipFor);
    hideChip();
    if (!s?.speaker || !s.pos) return;
    store.edit([{op: 'put', coll: 'speakers', item: {id: s.speaker, pos: {...s.pos}}}, {op: 'put', coll: 'subtitles', item: {id: s.id, pos: null}}], '화자 위치');
    toast(`${store.speaker(s.speaker)?.name || '화자'} 위치 바꿈`, {icon: 'users', timeout: 1600});
  });

  // 두 번 누르면 그 자리에서 고치기 (Enter 저장 · Shift+Enter 줄바꿈 · Esc 취소)
  subsLayer.addEventListener('dblclick', e => {
    const se = e.target instanceof Element ? e.target.closest('.pl-sub') : null;
    if (se && !se.classList.contains('editing')) startEdit(se);
  });
  function startEdit(se) {
    const id = se.dataset.id, sub = store.item('sub', id);
    if (!sub) return;
    if (store.playing) store.setPlaying(false, 'player-btn');
    hideChip();
    editing = {id, el: se, old: sub.text};
    se.classList.add('editing');
    se.contentEditable = 'plaintext-only';
    if (se.contentEditable !== 'plaintext-only') se.contentEditable = 'true';
    se.spellcheck = false;
    se.textContent = sub.text;
    se.focus();
    const r = document.createRange();
    r.selectNodeContents(se);
    const s = getSelection();
    s.removeAllRanges();
    s.addRange(r);
    se.addEventListener('keydown', onEditKey);
    se.addEventListener('blur', onEditBlur);
  }
  function onEditKey(e) {
    e.stopPropagation();
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); endEdit(true); }
    else if (e.key === 'Enter') { e.preventDefault(); document.execCommand('insertText', false, '\n'); }
    else if (e.key === 'Escape') { e.preventDefault(); endEdit(false); }
  }
  function onEditBlur() { endEdit(true); }
  function endEdit(save) {
    const ed = editing;
    if (!ed) return;
    editing = null;
    const se = ed.el, text = se.innerText.replace(/\u00a0/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
    se.removeEventListener('keydown', onEditKey);
    se.removeEventListener('blur', onEditBlur);
    se.removeAttribute('contenteditable');
    se.classList.remove('editing');
    se._k = '';
    if (save && text && text !== ed.old && store.item('sub', ed.id)) store.edit([{op: 'put', coll: 'subtitles', item: {id: ed.id, text}}], '자막 고치기');
    if (document.activeElement === se) se.blur();
    paintNow();
  }
  // 빈 곳을 누르면 자막 선택을 풀어요
  view.addEventListener('pointerdown', e => {
    if (e.button !== 0 || (e.target instanceof Element && e.target.closest('.pl-sub,.pl-chip'))) return;
    if (store.sel?.kind === 'sub') store.select(null);
  });

  // ---------- 그리기 ----------
  function layout() {
    const cs = getComputedStyle(view), padX = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight), padY = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    const W = view.clientWidth - padX, H = view.clientHeight - padY;
    const c = store.project?.canvas || {}, cw = +c.w || 16, chh = +c.h || 9, k = Math.max(0, Math.min(W / cw, H / chh));
    const w = Math.floor(cw * k), hh = Math.floor(chh * k);
    stage.style.width = `${w}px`;
    stage.style.height = `${hh}px`;
    stage.style.setProperty('--fs', `${(hh * 0.052).toFixed(2)}px`);
  }
  new ResizeObserver(layout).observe(view);

  function paintTime() {
    const t = store.time, all = store.playAll;   // 잘린 곳도 재생하면 원본 시간이 흘러요
    tcCur.textContent = fmt(all ? t : store.toEdited(t));
    tcTotal.textContent = fmt(all ? store.total : store.editedDuration);
    tcOrig.textContent = `원본 ${fmt(t, 1)}`;
    const c = P.preview ? null : store.map.cutAt(t);
    stage.classList.toggle('in-cut', !!c && t > c[0] + EPS);
    prevBtn.disabled = !(t > 0.01);
    nextBtn.disabled = !(t < store.total - 0.01);
    if (P.preview) paintPreviewHead();
    if (P.ready && noMedia(t) !== !ph.hidden) paintPh();
  }
  function paintNow() {
    cancelAnimationFrame(paintRaf);
    paintRaf = 0;
    paintTime();
    paintSubs();
  }
  const paintSoon = () => { if (!paintRaf) paintRaf = requestAnimationFrame(() => { paintRaf = 0; paintTime(); paintSubs(); }); };
  function paintPlay() {
    const on = store.playing;
    playBtn.replaceChildren(icon(on ? 'pause' : 'play', 20));
    tip(playBtn, on ? '일시 정지' : '재생', 'space');
    el.classList.toggle('playing', on);
  }
  function paintAll() {
    const on = !!store.playAll;
    allBtn.setAttribute('aria-pressed', String(on));
    tip(allBtn, on ? '잘린 곳도 재생 · 켜짐' : '잘린 곳도 재생');
    allChip.hidden = !on;
    tcOrig.hidden = on;   // 위 시간이 이미 원본이에요
  }
  function paintPreview() {
    const p = P.preview;
    pvBar.classList.toggle('on', !!p);
    stage.classList.toggle('previewing', !!p);
    if (!p) { pvPill.hidden = true; stage.classList.remove('pv-in'); return; }
    const L = p.to - p.from || 1;
    pvCut.style.left = `${(p.start - p.from) / L * 100}%`;
    pvCut.style.width = `${(p.end - p.start) / L * 100}%`;
    paintPreviewHead();
  }
  function paintPreviewHead() {
    const p = P.preview, t = store.time;
    if (!p) return;
    pvHead.style.left = `${clamp((t - p.from) / (p.to - p.from || 1), 0, 1) * 100}%`;
    const inCut = t >= p.start - EPS && t < p.end;
    const c = p.id ? store.item('cut', p.id) : null, on = c ? !!c.enabled : true;
    pvCut.classList.toggle('keep', !on);
    stage.classList.toggle('pv-in', inCut);
    stage.classList.toggle('pv-keep', inCut && !on);
    pvPill.hidden = !inCut;
    if (inCut) {
      const label = `${on ? '자름' : '살림'} · ${fmtDur(p.end - p.start)}`;
      if (pvPill.textContent !== label) { pvPill.textContent = label; pvPill.className = `pill pl-pv-pill ${on ? 'cut' : 'keep'}`; }
    }
  }
  // 미리보기 파일이 없을 때 (분석 중): 전체 진행 고리 + '분석 중 · 2/3' (몇 번째 클립인지). 클립별 % 는 카드·타임라인에만 있어요.
  function paintPh() {
    const srcs = store.project?.sources || [], t = store.time;
    const beyond = P.ready && noMedia(t);   // 아직 분석 중인 클립 자리
    const showIt = !P.ready || beyond || !srcs.length;
    ph.hidden = !showIt;
    stage.classList.toggle('no-media', showIt);
    if (!showIt) return;
    if (!srcs.length) { setPh(null, '영상 없음'); return; }
    const busy = srcs.filter(s => s.status === 'queued' || s.status === 'analyzing');
    const job = (store.jobs || []).find(j => (j.kind === 'analyze' || j.kind === 'transcribe') && j.state !== 'done' && j.state !== 'error');
    if (busy.length || job) {
      const p = job && Number.isFinite(+job.progress) ? +job.progress : busy.reduce((a, s) => a + (+s.progress || 0), 0) / (busy.length || 1);
      const i = srcs.findIndex(s => s.status === 'analyzing'), k = i >= 0 ? i : srcs.findIndex(s => s.status === 'queued');
      setPh(p > 0 ? p : -1, srcs.length > 1 && k >= 0 ? `분석 중 · ${k + 1}/${srcs.length}` : '분석 중');
    } else if (srcs.some(s => s.status === 'error')) setPh('err', '분석 실패');
    else setPh(-1, '불러오는 중');
  }
  function setPh(p, text) {
    if (typeof p === 'number') {
      if (phIco.firstChild !== phRing) phIco.replaceChildren(phRing);
      phRing.set(p);
    } else if (phIco.dataset.ico !== String(p)) phIco.replaceChildren(icon(p === 'err' ? 'alert' : 'film', 24));
    phIco.dataset.ico = typeof p === 'number' ? 'ring' : String(p);
    phText.textContent = text;
  }

  // ---------- 영상 파일 열기 ----------
  // 클립 목록·순서·재생 주소(원본 / 변환본)가 바뀌면 두 video 를 새로 열어요.
  // 재생 중이었으면 지금 자리를 잡아 두고 멈췄다가, 준비되면 그 자리에서 다시 재생해요.
  function load(force) {
    const key = srcList().map(s => `${s.id}@${s.offset}=${store.assets?.[s.id]?.play || ''}`).join('|');
    if (!force && key === P.base) return;
    P.base = key;
    P.bust = force ? Date.now().toString(36) : '';
    clearTimeout(P.retryT);
    if (store.playing) {
      P.resume = !P.preview;
      store.setPlaying(false, SELF);
      stopPlay();
    }
    P.ready = false;
    P.loading = true;
    for (const v of vids) { v._want = null; v._url = null; v._off = 0; v.removeAttribute('src'); v.load(); }
    show(P.cur);
    seek(active(), store.time);
    paintPh();
  }
  function retryLater() {
    clearTimeout(P.retryT);
    const srcs = store.project?.sources || [];
    if (!srcs.length || srcs.some(s => s.status === 'queued' || s.status === 'analyzing')) return;   // 끝나면 project 이벤트로 다시 해요
    P.retryT = setTimeout(() => load(true), Math.min(15000, 2000 * 2 ** P.retry++));
  }

  // ---------- 편집점 · 원본 시간 · 전체 화면 ----------
  function jumpPoint(dir) {
    const t = store.time, pts = new Set([0]), edit = !store.playAll && store.axis === 'edit';
    const sp = spans();
    if (edit) { for (const [s] of sp) pts.add(s); if (sp.length) pts.add(sp[sp.length - 1][1]); }
    else {
      for (const [s, e] of store.derived.cutIntervals) { pts.add(s); pts.add(e); }
      for (const s of store.derived.spans) pts.add(s.start);
      pts.add(store.total);
    }
    for (const x of store.project?.splits || []) if (!edit || !store.map.cutAt(x)) pts.add(x);
    const list = [...pts].sort((a, b) => a - b);
    const to = dir < 0 ? list.filter(x => x < t - 0.05).pop() : list.find(x => x > t + 0.05);
    if (to != null) store.setTime(to, 'player-nav');
  }
  async function copyOrig() {
    const s = `원본 ${fmt(store.time, 1)}`;
    if (await copyText(s)) toast('복사됨', {icon: 'check', timeout: 1400});
  }
  function toggleFs() {
    if (document.fullscreenElement) document.exitFullscreen?.();
    else el.requestFullscreen?.().catch(() => {});
  }
  document.addEventListener('fullscreenchange', () => {
    const on = document.fullscreenElement === el;
    el.classList.toggle('fs', on);
    fsBtn.replaceChildren(icon(on ? 'fullscreen-exit' : 'fullscreen', 18));
    tip(fsBtn, on ? '전체 화면 끝' : '전체 화면');
  });

  // ---------- 상태 → 화면 ----------
  store.on('play', (on, src) => {
    if (src !== SELF) { if (on) startPlay(); else stopPlay(); }
    paintPlay();
  });
  store.on('time', (t, src) => {
    if (src === SELF) { paintNow(); return; }
    paintSoon();
    if (P.preview) {
      const p = P.preview;
      if (src === 'select' && performance.now() - p.at < 300 && t >= p.start - EPS && t <= p.end + EPS) return;   // 듣기 바로 뒤 선택 이동은 무시
      if (t < p.from - EPS || t > p.to + EPS) { endPreview(); paintPreview(); }
    }
    if (!P.ready) { seek(active(), t); return; }   // 아직 못 연 클립이면 지금 자리 클립부터 열어 봐요
    if (store.playing) jump(t);
    else { seek(active(), t); queuePreseek(); }
  });
  store.on('project', ({colls}) => {
    const has = k => !colls || colls.has(k);
    if (has('cuts') || has('sources') || has('canvas')) {
      spanCache = null;
      if (store.playing) replan(); else queuePreseek();
    }
    if (has('subtitles') || has('speakers')) subIdx = null;
    if (has('canvas')) layout();
    if (has('sources')) load(false);
    paintPh();
    paintSoon();
  });
  store.on('preview', () => load(false));
  store.on('view', keys => {
    if (keys.includes('playAll')) {
      paintAll();
      spanCache = null;
      if (store.playing && !P.preview) jump(active()._want ?? vt(active())); else queuePreseek();
      paintSoon();
    }
    if (keys.includes('review') && !store.review) stopPreview();
    if (keys.includes('axis')) paintSoon();
  });
  store.on('sel', sel => {
    if (P.preview && !(sel?.kind === 'cut' && sameRange(P.preview, store.item('cut', sel.id) || {}))) stopPreview();
    if (chipFor && !(sel?.kind === 'sub' && sel.id === chipFor)) hideChip();
    paintSoon();
  });
  // 탭이 가려지면 프레임 콜백이 멈춰서 잘린 곳을 못 건너뛰어요 → 멈춰 둬요
  document.addEventListener('visibilitychange', () => { if (document.hidden && store.playing) store.setPlaying(false, 'hidden'); });
  store.on('jobs', paintPh);

  layout();
  applyVol(false);
  paintPlay();
  paintAll();
  load(false);
  paintNow();
}

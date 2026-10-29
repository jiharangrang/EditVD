// 원본 ↔ 편집본 시간 계산 (editvdo/project.py 와 같은 규칙) + 시간 표기.
// 원본 축 = 모든 영상을 순서대로 이어 붙인 시간. 편집본 축 = 켜진 컷을 뺀 시간.
export const r3 = x => Math.round(x * 1000) / 1000;

export function total(p) {
  let t = 0;
  for (const s of p?.sources || []) t += +s.duration || 0;
  return r3(t);
}

// 영상 경계: [{id, start, end}]
export function sourceSpans(p) {
  let t = 0;
  return (p?.sources || []).map(s => { const a = t; t += +s.duration || 0; return {id: s.id, start: r3(a), end: r3(t)}; });
}

// 켜진 컷(enabled 가 없으면 켜진 것) + 'clip' 컷을 합치고 정렬해서 [0,total] 안으로 [[s,e],...]
export function cutIntervals(p) {
  const T = total(p), iv = [];
  for (const c of p?.cuts || []) {
    if (!((c.enabled ?? true) || c.type === 'clip')) continue;
    const s = Math.max(0, +c.start), e = Math.min(T, +c.end);
    if (e > s) iv.push([s, e]);
  }
  iv.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const out = [];
  for (const [s, e] of iv) {
    const L = out[out.length - 1];
    if (L && s <= L[1]) L[1] = Math.max(L[1], e);
    else out.push([s, e]);
  }
  return out;
}

// 남는 구간: 컷의 여집합을 영상 경계에서 한 번 더 나누고 1프레임보다 짧은 조각은 버려요
export function keepSegments(p, iv = cutIntervals(p)) {
  const T = total(p), minLen = 1 / (p?.canvas?.fps || 30) - 1e-9;
  const bounds = sourceSpans(p).slice(1).map(s => s.start);
  const out = [];
  const push = (a, b) => {
    let s = a;
    for (const x of bounds) if (x > s && x < b) { if (x - s >= minLen) out.push([s, x]); s = x; }
    if (b - s >= minLen) out.push([s, b]);
  };
  let t = 0;
  for (const [s, e] of iv) { if (s > t) push(t, s); t = Math.max(t, e); }
  if (T > t) push(t, T);
  return out;
}

export function editedDuration(p, keep = keepSegments(p)) {
  return keep.reduce((a, [s, e]) => a + (e - s), 0);
}

// 원본 → 편집본. 잘린 구간 안의 시간은 그 컷 시작(= 다음 조각 시작)의 편집본 위치로 붙어요.
export function toEdited(p, t, keep = keepSegments(p)) {
  let acc = 0;
  for (const [s, e] of keep) {
    if (t < s) return acc;
    if (t < e) return acc + (t - s);
    acc += e - s;
  }
  return acc;
}

// 편집본 → 원본. 경계에서는 다음 남는 조각의 시작으로 가요.
export function toOriginal(p, x, keep = keepSegments(p)) {
  let acc = 0;
  for (const [s, e] of keep) {
    if (x < acc + (e - s)) return s + Math.max(0, x - acc);
    acc += e - s;
  }
  return keep.length ? keep[keep.length - 1][1] : 0;
}

// 빠른 변환 (이진 탐색): makeMap(p) → {total, iv, keep, starts, acc, edited, toEdited, toOriginal, isCut, segAt, cutAt}
export function makeMap(p) {
  const iv = cutIntervals(p), keep = keepSegments(p, iv), T = total(p);
  const starts = keep.map(k => k[0]), acc = [];
  let a = 0;
  for (const [s, e] of keep) { acc.push(a); a += e - s; }
  const edited = a;
  const last = (arr, v) => { let lo = 0, hi = arr.length - 1, r = -1; while (lo <= hi) { const m = (lo + hi) >> 1; if (arr[m] <= v) { r = m; lo = m + 1; } else hi = m - 1; } return r; };
  const ivStarts = iv.map(x => x[0]);
  return {
    total: T, iv, keep, starts, acc, edited,
    toEdited(t) {
      const i = last(starts, t);
      if (i < 0) return 0;
      const [s, e] = keep[i];
      return t < e ? acc[i] + (t - s) : acc[i] + (e - s);
    },
    toOriginal(x) {
      if (!keep.length) return 0;
      const i = last(acc, x);
      if (i < 0) return keep[0][0];
      const [s, e] = keep[i];
      return x < acc[i] + (e - s) ? s + (x - acc[i]) : keep[keep.length - 1][1];
    },
    segAt(t) { const i = last(starts, t); return i >= 0 && t < keep[i][1] ? i : -1; },   // t 가 들어 있는 남는 조각 번호 (없으면 -1)
    cutAt(t) { const i = last(ivStarts, t); return i >= 0 && t < iv[i][1] ? iv[i] : null; },   // t 를 덮는 잘린 구간 [s,e]
    isCut(t) { return this.segAt(t) < 0; },
  };
}

// ---------- 표기 ----------
const pad = (n, w = 2) => String(n).padStart(w, '0');
// fmt(12.4) → '00:12.40', fmt(3725.5) → '1:02:05.50', fmt(312, 0) → '05:12'
export function fmt(t, d = 2) {
  if (!Number.isFinite(t)) t = 0;
  const neg = t < 0, f = 10 ** d;
  let x = Math.round(Math.abs(t) * f);
  const hh = Math.floor(x / (3600 * f)); x -= hh * 3600 * f;
  const mm = Math.floor(x / (60 * f)); x -= mm * 60 * f;
  const ss = Math.floor(x / f), fr = x - ss * f;
  const sign = neg ? '-' : '';
  if (hh) return `${sign}${hh}:${pad(mm)}:${pad(ss)}${d ? '.' + pad(fr, d) : ''}`;
  return `${sign}${pad(mm)}:${pad(ss)}${d ? '.' + pad(fr, d) : ''}`;
}
// 길이: 0.8s · 12.4s · 01:05
export function fmtDur(t) {
  t = Math.abs(+t || 0);
  if (t < 10) return `${(Math.round(t * 10) / 10).toFixed(1)}s`;
  if (t < 60) return `${Math.round(t * 10) / 10}s`;
  return fmt(t, 0);
}
// '1:02.5' · '12' · '1:02:03.25' · '00:12.40' → 초 (못 읽으면 NaN)
export function parseTime(str) {
  const s = String(str ?? '').trim().replace(/s$/i, '');
  if (!/^-?\d+(:\d{1,2}){0,2}([.,]\d+)?$/.test(s)) return NaN;
  const neg = s.startsWith('-'), parts = s.replace('-', '').replace(',', '.').split(':').map(Number);
  const v = parts.reduce((a, x) => a * 60 + x, 0);
  return neg ? -v : v;
}
// '방금' · '5분 전' · '3시간 전' · '어제' · '4일 전' · '10월 3일' · '2025. 10. 3.'
export function ago(iso, now = Date.now()) {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const s = (now - d) / 1000;
  if (s < 60) return '방금';
  if (s < 3600) return `${Math.floor(s / 60)}분 전`;
  const today = new Date(now); today.setHours(0, 0, 0, 0);
  const days = Math.floor((today - new Date(d).setHours(0, 0, 0, 0)) / 864e5);
  if (days <= 0) return `${Math.floor(s / 3600)}시간 전`;
  if (days === 1) return '어제';
  if (days < 7) return `${days}일 전`;
  return d.getFullYear() === today.getFullYear() ? `${d.getMonth() + 1}월 ${d.getDate()}일` : `${d.getFullYear()}. ${d.getMonth() + 1}. ${d.getDate()}.`;
}

// ---------- 자체 점검 ----------
// node -e "import('./ui/js/time.js').then(m => console.log(m.selfTest()))"
export function selfTest() {
  const eq = (a, b, msg) => { if (Math.abs(a - b) > 1e-9) throw new Error(`${msg}: ${a} != ${b}`); };
  const same = (a, b, msg) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${msg}: ${JSON.stringify(a)} != ${JSON.stringify(b)}`); };
  const p = {canvas: {fps: 30}, sources: [{id: 's1', duration: 10}, {id: 's2', duration: 5}],
    cuts: [{start: 2, end: 3, enabled: true}, {start: 2.5, end: 4, enabled: true}, {start: 9.5, end: 10.5, enabled: true},
      {start: 12, end: 13, enabled: false}, {start: 14.99, end: 20, enabled: true}, {start: 6, end: 6.01, enabled: false, type: 'clip'}]};
  same(cutIntervals(p), [[2, 4], [6, 6.01], [9.5, 10.5], [14.99, 15]], 'cutIntervals');
  same(keepSegments(p), [[0, 2], [4, 6], [6.01, 9.5], [10.5, 14.99]], 'keepSegments');
  const p2 = {canvas: {fps: 30}, sources: [{id: 's1', duration: 10}, {id: 's2', duration: 5}], cuts: []};
  same(keepSegments(p2), [[0, 10], [10, 15]], 'source split');
  eq(editedDuration(p), 2 + 2 + 3.49 + 4.49, 'editedDuration');
  const m = makeMap(p);
  for (const t of [0, 1, 2, 2.5, 3.99, 4, 5, 6.005, 7, 9.5, 10, 10.5, 12, 14.99, 15]) {
    eq(m.toEdited(t), toEdited(p, t), `toEdited(${t})`);
  }
  eq(toEdited(p, 3), 2, 'inside cut → cut start');
  eq(toEdited(p, 15), m.edited, 'end');
  for (const x of [0, 1, 2, 3, 4, 5.99, 6, 7.5, 11.97, 12]) eq(m.toOriginal(x), toOriginal(p, x), `toOriginal(${x})`);
  eq(toOriginal(p, 2), 4, 'boundary → next kept start');
  for (const t of [0.5, 4.5, 7, 11]) eq(toOriginal(p, toEdited(p, t)), t, `roundtrip ${t}`);
  eq(m.segAt(3), -1, 'segAt in cut'); eq(m.segAt(4), 1, 'segAt start');
  same(m.cutAt(3), [2, 4], 'cutAt');
  same([fmt(12.4), fmt(3725), fmt(312, 0), fmt(59.999), fmt(-1.5)], ['00:12.40', '1:02:05.00', '05:12', '01:00.00', '-00:01.50'], 'fmt');
  same([fmtDur(1.23), fmtDur(12.44), fmtDur(65)], ['1.2s', '12.4s', '01:05'], 'fmtDur');
  same([parseTime('1:02.5'), parseTime('12'), parseTime('1:02:03.25'), parseTime('00:12.40'), parseTime('x')].map(String), ['62.5', '12', '3723.25', '12.4', 'NaN'], 'parseTime');
  const empty = makeMap({sources: [], cuts: []});
  eq(empty.toEdited(5), 0, 'empty toEdited'); eq(empty.toOriginal(5), 0, 'empty toOriginal');
  return true;
}

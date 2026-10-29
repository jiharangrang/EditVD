// 소스별 파형(peaks.json)·필름스트립(thumbs.jpg) 불러오기. 같은 키는 한 번만 받아요.
// 파형은 2배씩 줄인 최댓값 단계(mipmap)를 만들어 두어서 어떤 확대에서도 픽셀당 몇 칸만 읽어요.
const cache = new Map();

function entry(key, load, onReady) {
  let e = cache.get(key);
  if (!e) {
    e = {state: 'loading', wait: new Set()};
    cache.set(key, e);
    load(e).then(() => { e.state = 'ready'; }, err => { e.state = 'error'; e.error = err; })
      .finally(() => { for (const fn of e.wait) fn(); e.wait.clear(); });
  }
  if (e.state === 'loading' && onReady) e.wait.add(onReady);
  return e;
}

// key 에 상태를 섞어 주면 (예: 분석 끝) 새로 받아요
export function peaksFor(url, key, onReady) {
  return entry(`p|${url}|${key}`, async e => {
    const r = await fetch(url, {cache: 'no-store'});
    if (!r.ok) throw new Error(`peaks ${r.status}`);
    const d = await r.json();
    e.perSec = +d.per_sec || 50;
    const mip = [Uint8Array.from(d.data || [])];
    while (mip[mip.length - 1].length > 1) {
      const a = mip[mip.length - 1], b = new Uint8Array(Math.ceil(a.length / 2));
      for (let i = 0; i < b.length; i++) b[i] = Math.max(a[2 * i], a[2 * i + 1] ?? 0);
      mip.push(b);
    }
    e.mip = mip;
    e.len = mip[0].length / e.perSec;
  }, onReady);
}

// 샘플 [s0, s1) (peaks 칸 단위, 실수) 의 최댓값 0..255
export function peakMax(e, s0, s1) {
  const mip = e.mip, n = mip[0].length;
  if (s1 <= 0 || s0 >= n) return 0;
  const span = Math.max(1, s1 - s0);
  let lv = Math.min(mip.length - 1, Math.max(0, Math.floor(Math.log2(span))));
  const a = mip[lv], k = 1 << lv;
  let i = Math.max(0, Math.floor(s0 / k)), j = Math.min(a.length, Math.ceil(s1 / k));
  if (j <= i) j = Math.min(a.length, i + 1);
  let m = 0;
  for (; i < j; i++) if (a[i] > m) m = a[i];
  return m;
}

// 조용한 구간 (소스 안 시간, 초): 쉼 0.15초 이상. 문턱 = 하위 10% 음량 + 10 dB (−50~−28 dB) — analyze 의 fine 무음과 비슷하게
export function quietRuns(e) {
  if (e.quiet || e.state !== 'ready') return e.quiet || [];
  const a = e.mip[0], n = a.length;
  const db = new Float32Array(n);
  for (let i = 0; i < n; i++) db[i] = a[i] ? 40 * Math.log10(a[i] / 255) : -90;
  const sorted = Float32Array.from(db).sort();
  const p10 = sorted[Math.floor(n * 0.1)] ?? -90;
  const thr = Math.min(-28, Math.max(-50, p10 + 10));
  const min = Math.ceil(0.15 * e.perSec), out = [];
  let s = -1;
  for (let i = 0; i <= n; i++) {
    const q = i < n && db[i] < thr;
    if (q && s < 0) s = i;
    else if (!q && s >= 0) { if (i - s >= min) out.push([s / e.perSec, i / e.perSec]); s = -1; }
  }
  return (e.quiet = out);
}

// 무음 목록 (silences.json: {fine: [[s,e]], silences, threshold_db}, 소스 안 시간) — 끝 맞추기용
export function silencesFor(url, key) {
  return entry(`s|${url}|${key}`, async e => {
    const r = await fetch(url, {cache: 'no-store'});
    if (!r.ok) throw new Error(`silences ${r.status}`);
    const d = await r.json();
    e.fine = Array.isArray(d?.fine) ? d.fine : Array.isArray(d?.silences) ? d.silences : [];
  });
}

// 필름스트립: meta = {interval, w, h, cols, count} 또는 그 JSON 주소
export function thumbsFor(url, meta, key, onReady) {
  return entry(`t|${url}|${key}`, async e => {
    const get = (u, as) => fetch(u, {cache: 'no-store'}).then(r => { if (!r.ok) throw new Error(`thumbs ${r.status}`); return r[as](); });
    const [m, blob] = await Promise.all([typeof meta === 'string' ? get(meta, 'json') : meta, get(url, 'blob')]);
    if (!m?.count) throw new Error('thumbs meta');
    e.meta = m;
    e.img = await createImageBitmap(blob);
  }, onReady);
}

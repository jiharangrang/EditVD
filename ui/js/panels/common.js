// 왼쪽 패널 공용: 스타일 불러오기 · 가상 목록 · 단어 색인 · 작은 도구
import { h } from '../ui.js';

// panels.css 를 한 번만 붙이고, 다 불러올 때까지 기다릴 수 있어요 (처음 그릴 때 깜빡이지 않게)
let cssReady = null;
export function ensureCss() {
  return cssReady ||= new Promise(res => {
    if (document.getElementById('css-panels')) return res();
    const link = h('link', {rel: 'stylesheet', id: 'css-panels', href: '/ui/css/panels.css'});
    link.addEventListener('load', () => res(), {once: true});
    link.addEventListener('error', () => res(), {once: true});
    document.head.append(link);
    setTimeout(res, 1500);
  });
}

// 한 프레임에 한 번만 부르기: const draw = frame(() => ...); draw(); draw();
export function frame(fn) {
  let id = 0;
  const f = () => { if (!id) id = requestAnimationFrame(() => { id = 0; fn(); }); };
  f.now = () => { if (id) cancelAnimationFrame(id); id = 0; fn(); };
  return f;
}

export const setText = (el, v) => { v = v == null ? '' : String(v); if (el.textContent !== v) el.textContent = v; };
export const toggle = (el, cls, on) => { if (el.classList.contains(cls) !== !!on) el.classList.toggle(cls, !!on); };
export const isEditable = el => el instanceof HTMLElement && (el.isContentEditable || el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && !/^(button|checkbox|radio|range|color|file|submit|reset)$/i.test(el.type)));

// ---------- 가상 목록 ----------
// 보이는 줄만 DOM 으로 그려요. 줄 높이는 고정(height: 숫자) 또는 줄마다 추정(height: fn) + 그린 뒤 실제 높이로 고쳐요.
// new VList(스크롤요소, {height, create(item) → el, update(el, item, i), key, sig(item) → 높이를 바꾸는 내용, measure(els) → 높이들})
export class VList {
  constructor(sc, {height, create, update, key = it => it.id, sig = null, measure = null, overscan = 360, padTop = 2, padBottom = 12} = {}) {
    Object.assign(this, {sc, height, create, update, key, sig, measureFn: measure, overscan, padTop, padBottom});
    this.fixed = typeof height === 'number';
    this.sizer = h('div.pn-sizer');
    sc.append(this.sizer);
    sc.style.overflowAnchor = 'none';
    this.items = []; this.keys = []; this.index = new Map(); this.tops = new Float64Array(1);
    this.rows = new Map();
    this.measured = new Map();   // key → {sig, h}
    this._full = false; this._vh = 0; this._w = 0; this._reveal = null;
    this.draw = frame(() => this._render());
    sc.addEventListener('scroll', () => this.draw(), {passive: true});
    new ResizeObserver(() => {
      const vh = sc.clientHeight, w = sc.clientWidth;
      if (w !== this._w && !this.fixed) {
        this.measured.clear();
        for (const el of this.rows.values()) el._sig = undefined;
        this.onWidth?.(w);
        this._w = w;
        this._relayout();
        this._full = true;
      }
      const shown = vh && !this._vh;
      this._w = w; this._vh = vh;
      if (vh && this._reveal) { const r = this._reveal; this._reveal = null; this.reveal(r.key, r.align); }
      if (shown) this.draw.now(); else this.draw();   // 탭이 막 보이면 같은 프레임에 그려요
    }).observe(sc);
  }
  get width() { return this._w || this.sc.clientWidth; }
  setItems(items) {
    this.items = items;
    this.keys = items.map(this.key);
    this.index = new Map(this.keys.map((k, i) => [k, i]));
    this._relayout();
    this._full = true;
    this.draw();
  }
  _h(i) {
    if (this.fixed) return this.height;
    const it = this.items[i], m = this.measured.get(this.keys[i]);
    if (m && (!this.sig || m.sig === this.sig(it))) return m.h;
    return this.height(it, i);
  }
  _relayout() {
    const n = this.items.length, tops = new Float64Array(n + 1);
    let y = this.padTop;
    for (let i = 0; i < n; i++) { tops[i] = y; y += this._h(i); }
    tops[n] = y;
    this.tops = tops;
    this.sizer.style.height = `${y + this.padBottom}px`;
  }
  _place() {
    for (const [k, el] of this.rows) {
      const i = this.index.get(k);
      if (i == null) continue;
      const y = this.tops[i];
      if (el._y !== y) { el.style.transform = `translateY(${y}px)`; el._y = y; }
    }
  }
  refresh() { this._full = true; this.draw(); }   // 보이는 줄을 다시 채워요
  el(key) { return this.rows.get(key) || null; }
  first(y) {   // y 를 덮는 줄 번호
    const t = this.tops, n = this.items.length;
    let lo = 0, hi = n - 1, r = 0;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (t[m] <= y) { r = m; lo = m + 1; } else hi = m - 1; }
    return r;
  }
  _render() {
    const sc = this.sc, vh = this._vh, n = this.items.length;
    if (!vh) return;   // 숨은 탭이면 보일 때 다시 그려요 (_full 은 남겨 둬요)
    const full = this._full;
    this._full = false;
    const st = sc.scrollTop, seen = new Set(), fresh = [];
    const keep = (el, i, isNew) => {
      if (isNew || full) this.update(el, this.items[i], i);
      if (!this.fixed && (isNew || (full && el._sig !== (this.sig ? this.sig(this.items[i]) : el._sig)))) fresh.push(i);
      const y = this.tops[i];
      if (el._y !== y) { el.style.transform = `translateY(${y}px)`; el._y = y; }
    };
    if (n) {
      const i0 = this.first(Math.max(0, st - this.overscan)), i1 = Math.min(n - 1, this.first(st + vh + this.overscan));
      for (let i = i0; i <= i1; i++) {
        const k = this.keys[i];
        let el = this.rows.get(k), isNew = false;
        if (!el) { el = this.create(this.items[i], i); el._y = NaN; this.rows.set(k, el); this.sizer.append(el); isNew = true; }
        keep(el, i, isNew);
        seen.add(k);
      }
    }
    const active = document.activeElement;
    for (const [k, el] of this.rows) {
      if (seen.has(k)) continue;
      if (this.index.has(k) && el.contains(active)) { keep(el, this.index.get(k), false); continue; }   // 입력 중인 줄은 남겨요
      el.remove();
      this.rows.delete(k);
    }
    if (fresh.length) this._measure(fresh);
  }
  // 그린 줄의 실제 높이를 재서 추정과 다르면 자리를 다시 잡아요 (보이는 곳 위쪽이 바뀌면 스크롤도 맞춰요)
  _measure(list) {
    const els = list.map(i => this.rows.get(this.keys[i]));
    const hs = this.measureFn ? this.measureFn(els) : els.map(el => el.offsetHeight);
    const st = this.sc.scrollTop;
    let shift = 0, changed = false;
    list.forEach((i, k) => {
      const sig = this.sig ? this.sig(this.items[i]) : null, hgt = hs[k], est = this.tops[i + 1] - this.tops[i];
      els[k]._sig = sig;
      this.measured.set(this.keys[i], {sig, h: hgt});
      if (Math.abs(hgt - est) > 0.5) { changed = true; if (this.tops[i + 1] <= st + 1) shift += hgt - est; }
    });
    if (!changed) return;
    this._relayout();
    this._place();
    if (shift) this.sc.scrollTop = st + shift;
  }
  // 한 줄의 높이가 바뀌었을 수 있을 때 (예: 입력하며 늘어나는 글상자)
  resized(key) {
    const i = this.index.get(key);
    if (i == null || !this.rows.has(key) || this.fixed) return;
    this._measure([i]);
  }
  // 줄이 보이게 스크롤해요 (align: 'nearest' | 'center'). 숨은 탭이면 보일 때 해요.
  reveal(key, align = 'nearest') {
    const i = this.index.get(key);
    if (i == null) return false;
    const vh = this._vh;
    if (!vh) { this._reveal = {key, align}; return false; }
    const top = this.tops[i], bot = this.tops[i + 1], st = this.sc.scrollTop;
    let to = st;
    if (align === 'center') to = top - (vh - (bot - top)) / 2;
    else if (top < st + 4) to = top - 8;
    else if (bot > st + vh - 4) to = bot - vh + 8;
    to = Math.max(0, Math.round(to));
    if (to !== st) this.sc.scrollTop = to;
    this._render();   // 바로 그려서 줄 요소가 있게 해요
    return true;
  }
}

// ---------- 단어 (원본 축, 시작 순) ----------
export function wordIndex(words) {
  const ws = (words || []).filter(w => Number.isFinite(+w.start)).slice().sort((a, b) => a.start - b.start);
  return {ws, starts: Float64Array.from(ws, w => +w.start)};
}
function lowerBound(arr, v) {
  let lo = 0, hi = arr.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < v) lo = m + 1; else hi = m; }
  return lo;
}
// 컷 [s,e] 앞 n개 · 안 · 뒤 n개 단어
export function wordsAround(idx, s, e, n = 2, maxIn = 8) {
  if (!idx?.ws.length) return null;
  const {ws, starts} = idx, tol = 0.06;
  const a = lowerBound(starts, s - tol);
  const inside = [];
  let j = a;
  for (; j < ws.length && ws[j].start < e - tol; j++) inside.push(ws[j]);
  const before = [];
  for (let k = a - 1; k >= 0 && before.length < n; k--) before.unshift(ws[k]);
  const after = ws.slice(j, j + n);
  const more = inside.length > maxIn;
  return {before, inside: more ? inside.slice(0, maxIn) : inside, after, more};
}

// 숨은 파일 고르기 (브라우저 창): pickFile('.srt,.vtt,.txt') → File | null
export function pickFile(accept) {
  return new Promise(res => {
    const inp = h('input', {type: 'file', accept, style: 'position:fixed;left:-9999px;opacity:0'});
    let done = false;
    const finish = f => { if (done) return; done = true; inp.remove(); res(f || null); };
    inp.addEventListener('change', () => finish(inp.files?.[0]));
    inp.addEventListener('cancel', () => finish(null));
    document.body.append(inp);
    inp.click();
  });
}

// 마우스로 누른 버튼·탭·스위치가 포커스를 가져가지 않게 해요 (←/→·Enter 같은 단축키가 계속 전체에 가요 · 시안 포커스 테두리도 안 남아요)
export function noFocus(root) {
  root.addEventListener('mousedown', e => {
    const b = e.target instanceof Element ? e.target.closest('button,[role=button],[role=tab],[role=switch]') : null;
    if (b && root.contains(b) && !b.closest('.popover')) e.preventDefault();
  });
}

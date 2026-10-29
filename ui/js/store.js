// 편집 화면의 단일 상태 저장소 (spec 11.7).
// ── 모듈 계약 ──
// 모듈은 export function mount(el, store) 하나. el 은 비어 있는 flex 세로 패널이에요 (머리줄·본문은 모듈이 그려요).
// 상태는 store 에 바로 있어요: project rev assets name sel{kind,id} axis('orig'|'edit') zoom scrollX time(원본 초) playing
//   tab('media'|'cuts'|'subs'|'memo') review magnet playAll jobs save{state,message} preview(버전 붙은 preview.mp4 주소 | null)
// 이벤트 store.on(이름, fn) → 끄는 함수:
//   project({src:'load'|'local'|'gesture'|'server'|'undo'|'redo'|'cancel'|'external', colls:Set|null, live?}) · sel(sel, prev, opts)
//   time(t, src) · play(bool, src) · view(keys[], src) · jobs(list) · job(끝난 작업) · save · online · history · layout
//   external({items, notes}) · preview(url, 바뀔 때) · error(err) · cmd(ctx) · cmd:<이름>(ctx)   ctx = {name, arg, event, handled}
// 바꾸기: edit(ops, 라벨, {merge, throttle, undo}) · gesture() → {update, commit, cancel} · action(라벨, () => api.x(...))
//   select(kind, id, {seek, reveal}) · setTime(t, src) · setPlaying(v) · togglePlay() · step(dir, big) · setView({...}) · setTab · setAxis
// 읽기: derived{keep, cutIntervals, pieces, spans, lanes, laneCount, unreviewed, openComments} · toEdited/toOriginal/toAxis/fromAxis
//   item(kind, id) · selItem · speaker(id) · speakerColor(id) · posOf(sub) · isHidden(sub) · words() · glowLeft(id) · previewUrl
// kind: cut · sub · comment · speaker · source · piece (별칭 subtitle, clip 등도 받아요)
import { api, media } from './api.js';
import { makeMap, sourceSpans } from './time.js';

export const SPEAKER_COLORS = ['#40DAF5', '#F5A524', '#A78BFA', '#34D399', '#F472B6', '#60A5FA'];
export const DEFAULT_POS = {x: 50, y: 88};
export const TABS = ['media', 'cuts', 'subs', 'memo'];
const VIEW = ['axis', 'zoom', 'scrollX', 'tab', 'review', 'magnet', 'playAll'];
const PREFIX = {cuts: 'c', subtitles: 't', comments: 'm'};
const KIND = {cut: 'cut', cuts: 'cut', sub: 'sub', subtitle: 'sub', subtitles: 'sub', comment: 'comment', comments: 'comment', memo: 'comment',
  piece: 'piece', pieces: 'piece', clip: 'piece', source: 'source', sources: 'source', speaker: 'speaker', speakers: 'speaker'};
export const KIND_COLL = {cut: 'cuts', sub: 'subtitles', comment: 'comments', speaker: 'speakers', source: 'sources'};
const GLOW_MS = 8000, OFFLINE = '서버에 연결할 수 없어요';

// localStorage (막혀 있어도 동작)
export const prefs = {
  get(k, d) { try { const v = localStorage.getItem(`ev.${k}`); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(`ev.${k}`, JSON.stringify(v)); } catch { /* 저장 못 해도 괜찮아요 */ } },
};

export function newId(prefix = '') {
  const b = crypto.getRandomValues(new Uint8Array(3));
  return prefix + [...b].map(x => x.toString(16).padStart(2, '0')).join('');
}
const clone = v => (v == null || typeof v !== 'object') ? v : (typeof structuredClone === 'function' ? structuredClone(v) : JSON.parse(JSON.stringify(v)));
const strip = it => { const o = clone(it); delete o.rev; delete o.by; return o; };
const isObj = v => !!v && typeof v === 'object' && !Array.isArray(v);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const editing = () => { const a = document.activeElement; return !!a && (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)); };
const getKey = (p, key) => key.split('.').reduce((o, k) => o?.[k], p);
function setKey(p, key, value, merge) {
  const ks = key.split('.'), last = ks.pop();
  let o = p;
  for (const k of ks) o = isObj(o[k]) ? o[k] : (o[k] = {});
  o[last] = merge && isObj(value) && isObj(o[last]) ? {...o[last], ...clone(value)} : clone(value);
}
const findItem = (p, coll, id) => (p?.[coll] || []).find(x => x.id === id);
const sortByTime = arr => arr?.sort((a, b) => (a.start ?? a.t ?? 0) - (b.start ?? b.t ?? 0));

// 새 항목 기본값 (project._blank, UI 가 만든 경우)
function blank(coll, id) {
  if (coll === 'cuts') return {type: 'user', enabled: true, reviewed: true, reason: ''};
  if (coll === 'subtitles') return {text: '', speaker: null, pos: null, locked: false, check: false};
  if (coll === 'comments') return {text: '', done: false, reply: ''};
  if (coll === 'speakers') { const i = id.charCodeAt(0) - 65; return {name: `화자 ${i + 1}`, color: SPEAKER_COLORS[((i % 6) + 6) % 6], pos: {...DEFAULT_POS}}; }
  return {};
}
// 자막의 시작·끝을 locked 없이 바꾸면 서버가 잠가요 (project._put) — 되돌리기에도 locked 를 같이 담아요
const withLock = (coll, keys) => coll === 'subtitles' && (keys.includes('start') || keys.includes('end')) && !keys.includes('locked') ? [...keys, 'locked'] : keys;

// op 를 로컬에 적용해요 (서버 apply_ops 와 같은 뜻, 자막 재계산은 서버가 해요)
export function applyOp(p, op) {
  if (op.op === 'set') return setKey(p, op.key, op.value, op.key.startsWith('settings.'));
  const arr = p[op.coll] ||= [];
  if (op.op === 'put') {
    const i = arr.findIndex(x => x.id === op.item.id);
    if (i < 0) { arr.push({id: op.item.id, ...blank(op.coll, String(op.item.id)), ...clone(op.item)}); return; }
    const cur = arr[i];
    if (op.coll === 'subtitles' && !('locked' in op.item) && (('start' in op.item && op.item.start !== cur.start) || ('end' in op.item && op.item.end !== cur.end))) cur.locked = true;
    Object.assign(cur, clone(op.item));
  } else if (op.op === 'del') {
    const i = arr.findIndex(x => x.id === op.id);
    if (i >= 0) arr.splice(i, 1);
  }
}
// 서버가 돌려준 변경 기록(항목 전체)을 적용해요. 항목 객체는 그대로 두고 내용만 바꿔요.
export function applyChange(p, c) {
  if (c.key) return setKey(p, c.key, c.value, false);
  const arr = p[c.coll] ||= [], id = c.deleted ? c.id : c.item?.id, i = arr.findIndex(x => x.id === id);
  if (c.deleted) { if (i >= 0) arr.splice(i, 1); return; }
  if (i < 0) { arr.push(clone(c.item)); return; }
  const cur = arr[i];
  for (const k of Object.keys(cur)) if (!(k in c.item)) delete cur[k];
  Object.assign(cur, clone(c.item));
}
// 서버의 항목 전체 응답에서 실제로 바뀐 필드만 골라 화면의 갱신 범위를 알려줘요.
export function changeOp(p, c) {
  const before = c.key ? getKey(p, c.key) : findItem(p, c.coll, c.deleted ? c.id : c.item?.id);
  if (c.deleted) return before ? {op: 'del', coll: c.coll, id: c.id} : null;
  if (c.key) return JSON.stringify(before) === JSON.stringify(c.value) ? null : {op: 'set', key: c.key, value: c.value};
  if (!before) return {op: 'put', coll: c.coll, item: c.item};
  const item = {id: c.item.id};
  for (const key of new Set([...Object.keys(before), ...Object.keys(c.item)])) {
    if (key === 'id' || key === 'rev' || key === 'by') continue;
    if (JSON.stringify(before[key]) !== JSON.stringify(c.item[key])) item[key] = c.item[key];
  }
  return Object.keys(item).length > 1 ? {op: 'put', coll: c.coll, item} : null;
}
// op 를 적용하기 직전 상태로 되돌리는 op
export function invertOp(p, op) {
  if (op.op === 'set') return {op: 'set', key: op.key, value: clone(getKey(p, op.key) ?? null)};
  const id = op.op === 'put' ? op.item.id : op.id, cur = findItem(p, op.coll, id);
  if (op.op === 'del') return cur ? {op: 'put', coll: op.coll, item: strip(cur)} : null;
  if (!cur) return {op: 'del', coll: op.coll, id};
  const item = {id};
  for (const k of withLock(op.coll, Object.keys(op.item))) if (k !== 'id' && k !== 'rev' && k !== 'by') item[k] = clone(cur[k] ?? null);
  return {op: 'put', coll: op.coll, item};
}
// 같은 key 의 set 은 마지막 것만 남겨요 (합친 되돌리기 기록용)
function compact(ops) {
  const seen = new Set(), out = [];
  for (let i = ops.length - 1; i >= 0; i--) {
    const o = ops[i];
    if (o.op === 'set') { if (seen.has(o.key)) continue; seen.add(o.key); }
    out.push(o);
  }
  return out.reverse();
}
const collOf = op => op.coll || (op.key.startsWith('settings') ? 'settings' : op.key);

function makeLanes(subs) {
  const lane = new Map(), ends = [];
  for (const s of [...subs].sort((a, b) => a.start - b.start || a.end - b.end)) {
    let i = ends.findIndex(e => e <= s.start + 1e-6);
    if (i < 0) { i = ends.length; ends.push(0); }
    ends[i] = s.end;
    lane.set(s.id, i);
  }
  return {lane, count: Math.max(1, ends.length)};
}

export class Store {
  constructor() {
    this._ev = new Map();
    this._bound = false;
    this._reset();
    this.magnet = prefs.get('magnet', true);
  }
  _reset() {
    Object.assign(this, {
      name: null, project: null, rev: 0, assets: {}, preview: null, wordsCount: 0,
      sel: null, axis: 'orig', zoom: 0, scrollX: 0, time: 0, playing: false, tab: 'cuts', review: false, playAll: false, jobs: [],
      save: {state: 'saved', message: ''}, online: true,
    });
    this._undo = []; this._redo = []; this._queue = [];
    this._flushT = 0; this._flushing = null; this._sending = false; this._loading = false; this._reloading = false;
    this._pollT = 0; this._gestures = 0; this._words = null; this._d = {}; this._map = null; this._jobsKey = '';
    this._glow = new Map(); this._changed = []; this._ci = -1; this._typedAt = 0;
    this._axisTab = {cuts: 'orig', subs: 'edit', ...prefs.get('axisByTab', {})};
  }
  get state() { return this; }

  // ---------- 이벤트 ----------
  on(ev, fn) {
    let s = this._ev.get(ev);
    if (!s) this._ev.set(ev, s = new Set());
    s.add(fn);
    return () => s.delete(fn);
  }
  off(ev, fn) { this._ev.get(ev)?.delete(fn); }
  once(ev, fn) { const off = this.on(ev, (...a) => { off(); fn(...a); }); return off; }
  emit(ev, ...args) {
    const s = this._ev.get(ev);
    if (!s) return;
    for (const fn of [...s]) { try { fn(...args); } catch (e) { console.error(`[store:${ev}]`, e); } }
  }
  // 단축키·툴바 명령: 모듈은 store.on('cmd', ctx => ...) 또는 store.on('cmd:split', ctx => ...) 로 받아요.
  // 처리한 모듈은 ctx.handled = true 로 표시해요.
  command(name, arg, event) {
    const ctx = {name, arg, event, handled: false};
    this.emit('cmd', ctx);
    this.emit(`cmd:${name}`, ctx);
    return ctx.handled;
  }

  // ---------- 파생 값 (컷·자막이 바뀔 때만 다시 계산) ----------
  get map() { return this._map ||= makeMap(this.project || {}); }
  get total() { return this.map.total; }
  get editedDuration() { return this.map.edited; }
  get fps() { return this.project?.canvas?.fps || 30; }
  toEdited(t) { return this.map.toEdited(t); }
  toOriginal(x) { return this.map.toOriginal(x); }
  toAxis(t) { return this.axis === 'edit' ? this.map.toEdited(t) : t; }        // 원본 → 지금 타임라인 축
  fromAxis(x) { return this.axis === 'edit' ? this.map.toOriginal(x) : x; }    // 지금 타임라인 축 → 원본
  get axisDuration() { return this.axis === 'edit' ? this.editedDuration : this.total; }
  get derived() {
    const s = this, c = this._d;
    const lanes = () => (c.lanes ||= makeLanes(s.project?.subtitles || []));
    return c.api ||= {
      get map() { return s.map; },
      get total() { return s.map.total; },
      get edited() { return s.map.edited; },
      get cutIntervals() { return s.map.iv; },                       // [[s,e]] 원본 축, 합친 잘린 구간
      get keep() { return s.map.keep; },                             // [[s,e]] 남는 구간 (영상 경계에서 나뉨)
      get spans() { return (c.spans ||= sourceSpans(s.project)); },   // [{id,start,end}] 영상별 원본 구간
      get pieces() { return (c.pieces ||= s._pieces()); },           // [{id,start,end,sid}] 남는 구간을 분할 표시로 나눈 조각
      get lanes() { return lanes().lane; },                          // Map 자막id → 줄 번호 (겹치면 아래로)
      get laneCount() { return lanes().count; },
      get unreviewed() { return (c.unrev ??= (s.project?.cuts || []).filter(x => !x.reviewed && x.type !== 'clip').length); },
      get openComments() { return (c.open ??= (s.project?.comments || []).filter(x => !x.done).length); },
    };
  }
  _pieces() {
    const splits = [...(this.project?.splits || [])].sort((a, b) => a - b), spans = this.derived.spans, out = [];
    const piece = (a, b) => {
      const src = spans.find(sp => a >= sp.start - 1e-6 && a < sp.end) || spans[spans.length - 1];
      out.push({id: `p${Math.round(a * 1000)}`, start: a, end: b, sid: src?.id ?? null});
    };
    for (const [a, b] of this.map.keep) {
      let x = a;
      for (const t of splits) if (t > x + 1e-6 && t < b - 1e-6) { piece(x, t); x = t; }
      piece(x, b);
    }
    return out;
  }
  _invalidate(colls) {
    const has = k => !colls || colls.has(k), d = this._d;
    if (has('cuts') || has('sources') || has('canvas')) { this._map = null; delete d.pieces; delete d.unrev; delete d.spans; }
    if (has('splits')) delete d.pieces;
    if (has('subtitles')) delete d.lanes;
    if (has('comments')) delete d.open;
    for (const k of Object.keys(d)) if (k.startsWith('by:') && has(k.slice(3))) delete d[k];
  }
  byId(coll) { return (this._d[`by:${coll}`] ||= new Map((this.project?.[coll] || []).map(x => [x.id, x]))); }
  // item('cut', id) · item('subtitles', id) · item('piece', id)
  item(kind, id) {
    const k = KIND[kind] || kind;
    if (k === 'piece') return this.derived.pieces.find(x => x.id === id) || null;
    return this.byId(KIND_COLL[k] || kind).get(id) || null;
  }
  get selItem() { return this.sel ? this.item(this.sel.kind, this.sel.id) : null; }
  speaker(id) { return id ? this.byId('speakers').get(id) || null : null; }
  speakerColor(id) { return this.speaker(id)?.color || (id ? SPEAKER_COLORS[(id.charCodeAt(0) - 65 + 600) % 6] : '#A1A1AA'); }
  posOf(sub) { return sub?.pos || this.speaker(sub?.speaker)?.pos || DEFAULT_POS; }   // 자막 위치 (가운데, % 단위)
  isHidden(sub) { return this.map.toEdited(sub.end) - this.map.toEdited(sub.start) < 1e-3; }   // 전부 잘린 줄
  get previewUrl() { return this.name ? media.preview(this.name) : ''; }
  sourceUrl(sid, file) { return media.source(this.name, sid, file); }
  words() {   // 전체 단어 (원본 축) — 한 번 받아 두고 다시 써요
    if (!this.name) return Promise.resolve([]);
    return (this._words ||= api.words(this.name, 0, this.total + 1)
      .then(d => Array.isArray(d) ? d : (d?.words || []))
      .catch(e => { this._words = null; throw e; }));
  }

  // ---------- 선택 · 시간 · 보기 ----------
  // select('cut', id, {seek, reveal}) · select({kind, id}) · select(null)
  select(kind, id, opts = {}) {
    if (isObj(kind)) { opts = id || {}; id = kind.id; kind = kind.kind; }
    const k = kind ? (KIND[kind] || kind) : null, next = k && id != null ? {kind: k, id} : null, prev = this.sel;
    if (!opts.force && prev?.kind === next?.kind && prev?.id === next?.id) return;
    this.sel = next;
    this.emit('sel', next, prev, opts);
    if (opts.seek) { const it = this.selItem, t = it?.start ?? it?.t; if (t != null) this.setTime(t, 'select'); }
  }
  setTime(t, src = '') {
    t = Math.min(Math.max(+t || 0, 0), this.total || 0);
    if (t === this.time) return;
    this.time = t;
    this.emit('time', t, src);
  }
  setPlaying(v, src = '') {
    v = !!v;
    if (v === this.playing) return;
    this.playing = v;
    this.emit('play', v, src);
  }
  togglePlay(src = '') { this.setPlaying(!this.playing, src); }
  // 한 프레임(big: 1초) 이동. 편집본 축이면 잘린 곳을 건너뛰어요.
  step(dir, big = false) {
    this.setPlaying(false, 'step');
    const fps = this.fps, d = big ? 1 : 1 / fps;
    if (this.axis === 'edit' && !this.playAll) {
      const x = Math.min(Math.max(this.toEdited(this.time) + dir * d, 0), this.editedDuration);
      this.setTime(this.toOriginal(x), 'step');
    } else {
      const t = this.time + dir * d;
      this.setTime(big ? t : Math.round(t * fps) / fps, 'step');
    }
  }
  setView(patch, src = '') { this._view(patch, true, src); }
  _view(patch, remember, src) {
    const keys = [];
    for (const [k, v] of Object.entries(patch)) if (VIEW.includes(k) && this[k] !== v) { this[k] = v; keys.push(k); }
    if (!keys.length) return;
    if (remember && keys.includes('axis')) { this._axisTab[this.tab] = this.axis; prefs.set('axisByTab', this._axisTab); }
    if (keys.includes('magnet')) prefs.set('magnet', this.magnet);
    if (keys.includes('tab') && this.name) prefs.set(`view:${this.name}`, {tab: this.tab});
    this.emit('view', keys, src);
  }
  setAxis(a) { this.setView({axis: a === 'edit' ? 'edit' : 'orig'}); }
  // 탭을 바꾸면 그 탭에서 마지막으로 쓴 축(기본: 컷=원본, 자막=편집본)으로 바꿔요
  setTab(tab) {
    if (!TABS.includes(tab) || tab === this.tab) return;
    const patch = {tab};
    if (this._axisTab[tab]) patch.axis = this._axisTab[tab];
    if (this.review && tab !== 'cuts') patch.review = false;
    this._view(patch, false, 'tab');
  }

  // ---------- 프로젝트 열기 ----------
  async open(name) {
    if (this.name) this.close();
    this.name = name;
    this._loading = true;
    try {
      const d = await api.project(name);
      if (this.name !== name) return;
      const v = prefs.get(`view:${name}`, {});
      this.tab = TABS.includes(v.tab) ? v.tab : (d.project?.sources || []).some(s => s.status !== 'ready') ? 'media' : 'cuts';
      this.axis = this._axisTab[this.tab] || 'orig';
      this._setData(d);
      if (v.time) this.time = Math.min(+v.time || 0, this.total);
    } catch (e) {
      this.name = null;
      throw e;
    } finally { this._loading = false; }
    this._bindPage();
    this._poll(1000);
    this.emit('open', name);
  }
  close() {
    this._unload();
    clearTimeout(this._pollT);
    clearTimeout(this._flushT);
    this._reset();
  }
  reload() { return this.flush().then(() => this._fetch()); }
  async _fetch() {
    const name = this.name;
    const d = await api.project(name);
    if (name !== this.name) return null;
    for (const op of this._queue) applyOp(d.project, op);   // 아직 안 보낸 내 변경은 다시 얹어요
    this._setData(d);
    return d;
  }
  _setData(d) {
    const p = d.project;
    for (const k of ['cuts', 'subtitles', 'speakers', 'comments', 'sources', 'splits']) p[k] ||= [];
    sortByTime(p.cuts); sortByTime(p.subtitles); sortByTime(p.comments);
    this.project = p;
    this.rev = p.rev || 0;
    this.assets = d.assets || {};
    const preview = d.preview || null, newPreview = preview !== this.preview;
    this.preview = preview;
    if ((d.words_count || 0) !== this.wordsCount) this._words = null;
    this.wordsCount = d.words_count || 0;
    this._invalidate();
    if (this.sel && !this.selItem) { const prev = this.sel; this.sel = null; this.emit('sel', null, prev, {}); }
    if (this.time > this.total) { this.time = this.total; this.emit('time', this.time, 'load'); }
    this.emit('project', {src: 'load', colls: null});
    if (newPreview) this.emit('preview', preview);
  }

  // ---------- 편집 ----------
  // edit([{op:'put', coll:'cuts', item:{id, enabled:true, reviewed:true}}], '자르기', {merge:'key', throttle:true, undo:false})
  // 바로 화면에 반영하고, 프레임마다 모아서 서버에 보내요. 새 항목에 id 가 없으면 여기서 만들어요.
  edit(ops, label = '', opts = {}) {
    if (!this.project) return [];
    ops = this._norm(ops);
    if (!ops.length) return [];
    const inverse = [];
    for (const op of ops) {
      const inv = invertOp(this.project, op);
      if (inv) inverse.unshift(inv);
      applyOp(this.project, op);
    }
    this._after(ops, 'local');
    if (opts.undo !== false) this._record({ops, inverse, label, merge: opts.merge});
    this._send(ops, opts.throttle ? 150 : 16);
    return ops;
  }
  _norm(ops) {
    const used = new Set((this.project.speakers || []).map(s => s.id));
    return (Array.isArray(ops) ? ops : [ops]).filter(Boolean).map(o => {
      const op = clone(o);
      if (op.op === 'put') {
        op.item ||= {};
        if (op.item.id == null) {
          if (op.coll === 'speakers') {
            op.item.id = [...'ABCDEFGHIJKLMNOPQRSTUVWXYZ'].find(c => !used.has(c)) || newId('S');
            used.add(op.item.id);
          } else op.item.id = newId(PREFIX[op.coll] || 'x');
        }
      }
      return op;
    });
  }
  _after(ops, src, extra = {}) {
    const colls = new Set(ops.map(collOf));
    for (const c of ['cuts', 'subtitles', 'comments']) if (colls.has(c)) sortByTime(this.project[c]);
    this._invalidate(colls);
    this.emit('project', {src, colls, ops, ...extra});
  }
  _record(entry) {
    const last = this._undo[this._undo.length - 1], now = Date.now();
    if (entry.merge && last && last.merge === entry.merge && now - last.at < 2000) {
      last.ops = compact(last.ops.concat(entry.ops));
      last.inverse = compact(entry.inverse.concat(last.inverse));
      last.at = now;
    } else {
      this._undo.push({...entry, at: now});
      if (this._undo.length > 200) this._undo.shift();
    }
    this._redo.length = 0;
    this.emit('history');
  }
  // 직접 만든 되돌리기 (예: 영상 순서): pushUndo({label, undo: async () => {}, redo: async () => {}})
  pushUndo({label = '', undo, redo}) {
    this._undo.push({label, undo, redo, at: Date.now()});
    this._redo.length = 0;
    this.emit('history');
  }
  get canUndo() { return this._undo.length > 0; }
  get canRedo() { return this._redo.length > 0; }
  get undoLabel() { return this._undo[this._undo.length - 1]?.label || ''; }
  get redoLabel() { return this._redo[this._redo.length - 1]?.label || ''; }
  undo() { return this._history(this._undo, this._redo, 'undo'); }
  redo() { return this._history(this._redo, this._undo, 'redo'); }
  _history(from, to, dir) {
    const e = from.pop();
    if (!e || !this.project) return null;
    to.push(e);
    if (e.undo) Promise.resolve(dir === 'undo' ? e.undo() : e.redo?.()).catch(err => this.emit('error', err));
    else {
      const ops = clone(dir === 'undo' ? e.inverse : e.ops);
      for (const op of ops) applyOp(this.project, op);
      this._after(ops, dir);
      this._send(ops);
    }
    this.emit('history');
    return e.label || '';
  }

  // 끄는 동작: g = store.gesture(); g.update(ops) 여러 번(화면만) → g.commit('라벨') 한 번 저장 · g.cancel()
  gesture() {
    const T = new Map();
    let open = true;
    this._gestures++;
    const capture = op => {
      if (op.op === 'set') { const k = `k:${op.key}`; if (!T.has(k)) T.set(k, {key: op.key, orig: clone(getKey(this.project, op.key) ?? null)}); return; }
      const id = op.op === 'put' ? op.item.id : op.id, k = `${op.coll}:${id}`;
      let t = T.get(k);
      if (!t) { const cur = findItem(this.project, op.coll, id); T.set(k, t = {coll: op.coll, id, full: cur ? strip(cur) : null, fields: new Set()}); }
      if (op.op === 'put') for (const f of Object.keys(op.item)) if (f !== 'id' && f !== 'rev' && f !== 'by') t.fields.add(f);
    };
    const end = () => { if (open) { open = false; this._gestures--; } };
    return {
      update: ops => {
        if (!open || !this.project) return;
        ops = this._norm(ops);
        for (const op of ops) { capture(op); applyOp(this.project, op); }
        this._after(ops, 'gesture', {live: true});
      },
      commit: (label = '', ops) => {
        if (!open) return;
        if (ops) for (const op of this._norm(ops)) { capture(op); applyOp(this.project, op); }
        const fwd = [], inv = [], p = this.project;
        for (const t of T.values()) {
          if (t.key) { fwd.push({op: 'set', key: t.key, value: clone(getKey(p, t.key) ?? null)}); inv.push({op: 'set', key: t.key, value: t.orig}); continue; }
          const cur = findItem(p, t.coll, t.id);
          if (cur) {
            const item = t.full ? {id: t.id} : strip(cur);
            if (t.full) for (const f of t.fields) item[f] = clone(cur[f] ?? null);
            fwd.push({op: 'put', coll: t.coll, item});
            if (t.full) { const back = {id: t.id}; for (const f of withLock(t.coll, [...t.fields])) back[f] = clone(t.full[f] ?? null); inv.push({op: 'put', coll: t.coll, item: back}); }
            else inv.push({op: 'del', coll: t.coll, id: t.id});
          } else if (t.full) { fwd.push({op: 'del', coll: t.coll, id: t.id}); inv.push({op: 'put', coll: t.coll, item: t.full}); }
        }
        end();
        if (!fwd.length) return;
        this._after(fwd, 'local');
        this._record({ops: fwd, inverse: inv.reverse(), label});
        this._send(fwd);
      },
      cancel: () => {
        if (!open) return;
        const back = [];
        for (const t of [...T.values()].reverse()) {
          if (t.key) back.push({op: 'set', key: t.key, value: t.orig});
          else if (!t.full) back.push({op: 'del', coll: t.coll, id: t.id});
          else { const item = {id: t.id}; for (const f of withLock(t.coll, [...t.fields])) item[f] = clone(t.full[f] ?? null); back.push(findItem(this.project, t.coll, t.id) ? {op: 'put', coll: t.coll, item} : {op: 'put', coll: t.coll, item: t.full}); }
        }
        for (const op of back) applyOp(this.project, op);
        end();
        if (back.length) this._after(back, 'cancel');
      },
      get active() { return open; },
    };
  }

  // 서버 기능 호출 (자막 분할·합치기·대본 불러오기·컷 다시 찾기 …): 결과 changes 로 되돌리기 기록도 만들어요.
  // await store.action('분할', () => api.splitSub(store.name, id, t))
  async action(label, fn, {undo = true} = {}) {
    await this.flush();
    const res = await fn();
    if (!res || !this.project) return res;
    if (Array.isArray(res.changes)) {
      const fwd = [], inv = [], p = this.project;
      for (const c of res.changes) {
        if (c.key) { inv.push({op: 'set', key: c.key, value: clone(getKey(p, c.key) ?? null)}); fwd.push({op: 'set', key: c.key, value: clone(c.value)}); continue; }
        const id = c.deleted ? c.id : c.item?.id, cur = findItem(p, c.coll, id);
        if (c.deleted) { if (cur) inv.push({op: 'put', coll: c.coll, item: strip(cur)}); fwd.push({op: 'del', coll: c.coll, id}); }
        else { inv.push(cur ? {op: 'put', coll: c.coll, item: strip(cur)} : {op: 'del', coll: c.coll, id}); fwd.push({op: 'put', coll: c.coll, item: strip(c.item)}); }
      }
      const base = this.rev;
      this._applyServer(res);
      if (undo && label && fwd.length) this._record({ops: fwd, inverse: inv.reverse(), label});
      if (res.rev > base + 1) await this._external({prevRev: base});
    } else if (res.rev != null && res.rev > this.rev) await this._fetch();
    return res;
  }

  // ---------- 저장 ----------
  _send(ops, delay = 16) {
    this._queue.push(...ops);
    this._setSave('saving');
    if (!this._flushT) this._flushT = setTimeout(() => this.flush(), delay);
  }
  // 모아 둔 변경을 지금 보내요 (다 보낼 때까지 기다릴 수 있어요)
  flush() {
    clearTimeout(this._flushT);
    this._flushT = 0;
    if (!this._flushing) this._flushing = this._drain().finally(() => { this._flushing = null; });
    return this._flushing;
  }
  async _drain() {
    let retry = 0;
    while (this._queue.length && this.name) {
      const name = this.name, ops = this._queue.splice(0), base = this.rev;
      let res;
      this._sending = true;
      try { res = await api.ops(name, base, ops); }
      catch (e) {
        this._sending = false;
        if (name !== this.name) return;
        if (e.status === 0) {   // 연결이 끊기면 다시 보내요
          this._queue.unshift(...ops);
          this._online(false);
          await sleep(1500);
          continue;
        }
        if (e.status === 409 && retry++ < 2) {   // 그 사이 다른 곳에서 바뀌면 새로 받고 내 변경을 다시 얹어요
          this._queue.unshift(...ops);
          await this._fetch().catch(() => {});
          continue;
        }
        this._setSave('error', e.message);
        this.emit('error', e, {ops});
        await this._fetch().catch(() => {});   // 서버 상태로 맞춰요
        return;
      }
      this._sending = false;
      if (name !== this.name) return;
      this._online(true);
      this._applyServer(res);
      if (!this._queue.length) this._setSave('saved');
      if (res?.rev > base + 1) this._external({prevRev: base});   // 기다리지 않아요 (_external 이 flush 를 기다리므로)
    }
  }
  _applyServer(res) {
    if (!res || !this.project) return;
    if (res.rev != null) this.rev = Math.max(this.rev, res.rev);
    const ch = res.changes || [];
    if (!ch.length) return;
    const colls = new Set(), ops = [];
    for (const c of ch) {
      const coll = c.coll || (c.key.startsWith('settings') ? 'settings' : c.key);
      colls.add(coll);
      const op = changeOp(this.project, c);
      if (op) ops.push(op);
      applyChange(this.project, c);
    }
    for (const op of this._queue) applyOp(this.project, op);
    for (const c of ['cuts', 'subtitles', 'comments']) if (colls.has(c)) sortByTime(this.project[c]);
    this._invalidate(colls);
    if (ops.length) this.emit('project', {src: 'server', colls, changes: ch, ops});
  }
  _setSave(state, message = '') {
    if (this.save.state === state && this.save.message === message) return;
    this.save = {state, message};
    this.emit('save', this.save);
  }
  _online(v) {
    if (v === this.online) return;
    this.online = v;
    this.emit('online', v);
    if (!v) this._setSave('error', OFFLINE);
    else if (this.save.message === OFFLINE) this._setSave(this._queue.length ? 'saving' : 'saved');
  }

  // ---------- 바깥 변경 (Claude · 다른 창 · 분석 작업) ----------
  _poll(delay = 1000) {
    clearTimeout(this._pollT);
    this._pollT = setTimeout(() => this._tick(), delay);
  }
  async _tick() {
    const name = this.name;
    if (!name) return;
    if (!document.hidden) {
      try {
        const r = await api.rev(name);
        if (name !== this.name) return;
        this._online(true);
        this._setJobs(r.jobs || []);
        if (r.rev > this.rev && !this._busy()) await this._external({by: r.by});
      } catch (e) {
        if (name === this.name && e.status === 0) this._online(false);
      }
    }
    if (name === this.name) this._poll(1000);
  }
  _busy() {
    return this._loading || this._reloading || this._sending || this._queue.length > 0 || this._gestures > 0
      || (editing() && Date.now() - this._typedAt < 4000);
  }
  async _external({by, prevRev = this.rev} = {}) {
    if (this._reloading) return;
    this._reloading = true;
    try {
      await this.flush();
      const oldNotes = this.project?.notes || '';
      if (!await this._fetch()) return;
      // /rev 가 Claude 라고 하면 그 사이 바뀐 항목은 다 Claude 것 (켜기·끄기만 바꾸면 항목의 by 는 그대로예요)
      const p = this.project, items = [], cli = by === 'cli';
      for (const coll of ['cuts', 'subtitles', 'comments', 'speakers']) {
        for (const it of p[coll]) if ((it.rev || 0) > prevRev && (cli || it.by === 'cli')) items.push({coll, id: it.id});
      }
      by ||= p.by;
      const notes = (p.notes || '') !== oldNotes && by === 'cli';
      if (!items.length && !notes) return;
      const until = Date.now() + GLOW_MS;
      for (const x of items) this._glow.set(x.id, until);
      this._changed = [...items, ...(notes ? [{coll: 'notes', id: 'notes'}] : [])];
      this._ci = -1;
      this.emit('external', {items, notes, by});
      this.emit('project', {src: 'external', colls: null});
    } finally { this._reloading = false; }
  }
  glowLeft(id) {
    const u = this._glow.get(id);
    if (!u) return 0;
    const left = u - Date.now();
    if (left <= 0) { this._glow.delete(id); return 0; }
    return left;
  }
  get changedItems() { return this._changed; }
  // 'Claude가 고쳤어요' 보기 버튼: 고친 항목을 차례로 선택하고 그 시간으로 가요
  jumpChanged() {
    const list = this._changed.filter(x => x.coll === 'notes' || this.item(x.coll, x.id));
    if (!list.length) return null;
    this._ci = (this._ci + 1) % list.length;
    const x = list[this._ci];
    if (x.coll === 'notes') { this.setTab('memo'); return x; }
    const kind = {cuts: 'cut', subtitles: 'sub', comments: 'comment', speakers: 'speaker'}[x.coll];
    const tab = {cuts: 'cuts', subtitles: 'subs', comments: 'memo', speakers: 'subs'}[x.coll];
    this.setTab(tab);
    this.select(kind, x.id, {reveal: true, force: true});
    const it = this.item(x.coll, x.id), t = it?.start ?? it?.t;
    if (t != null) this.setTime(t, 'jump');
    return x;
  }

  // ---------- 작업 ----------
  _setJobs(jobs) {
    const key = JSON.stringify(jobs);
    if (key === this._jobsKey) return;
    const prev = new Map(this.jobs.map(j => [j.id, j]));
    this._jobsKey = key;
    this.jobs = jobs;
    this.emit('jobs', jobs);
    for (const j of jobs) {
      const o = prev.get(j.id);
      if (o && o.state !== j.state && (j.state === 'done' || j.state === 'error')) this.emit('job', j);
    }
  }
  // 작업이 끝날 때까지 기다려요: await store.waitJob(id, job => bar.set(job.progress))
  waitJob(id, onProgress) {
    return new Promise((resolve, reject) => {
      const tick = async () => {
        let j;
        try { j = await api.job(id); } catch (e) { return reject(e); }
        try { onProgress?.(j); } catch (e) { console.error(e); }
        if (j.state === 'done') return resolve(j);
        if (j.state === 'error') return reject(new Error(j.message || '작업이 실패했어요'));
        setTimeout(tick, 700);
      };
      tick();
    });
  }

  // ---------- 페이지 ----------
  _bindPage() {
    if (this._bound) return;
    this._bound = true;
    document.addEventListener('visibilitychange', () => { if (!document.hidden && this.name) this._poll(0); });
    document.addEventListener('keydown', () => { if (editing()) this._typedAt = Date.now(); }, true);
    addEventListener('pagehide', () => this._unload());
  }
  _unload() {
    if (!this.name) return;
    prefs.set(`view:${this.name}`, {tab: this.tab, time: this.time});
    if (this._queue.length) {
      const ops = this._queue.splice(0);
      api.ops(this.name, this.rev, ops, {keepalive: true}).catch(() => {});
    }
  }
}

export const store = new Store();
export default store;

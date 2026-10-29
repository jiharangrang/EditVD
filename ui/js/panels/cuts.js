// 컷 탭: 확인 진행 · 거르기 칩 · 다시 찾기 설정 · 차례로 확인(리뷰 모드) · 가상 목록 (spec 11.2, 11.6)
// 명령: cut / keep (X/V) · listen (Enter) · nav (↑/↓, 컷 탭일 때)
// 듣기: store.command('preview', {id, start, end, from, to}) 를 먼저 보내고 (재생기가 받아요), 아무도 안 받으면 '전체 재생'으로 직접 틀어요.
// 듣는 중 표시는 store.previewing (재생기) 을 따라요.
import { api } from '../api.js';
import { h, iconBtn, btn, menu, popover, toast, sliderRow, switchRow, chips, bar, kbd, glow, closePopover, clamp } from '../ui.js';
import { icon } from '../icons.js';
import { fmt, fmtDur } from '../time.js';
import { ensureCss, frame, VList, wordIndex, wordsAround, setText, toggle, noFocus } from './common.js';

const TYPE_LABEL = {silence: '무음', long: '긴 무음', filler: '필러', gap: '현장음', ng: 'NG', retake: '재촬영', manual: '요청', user: '직접', clip: '클립'};
const FILTERS = [{value: 'all', label: '전체'}, {value: 'todo', label: '미확인'}, {value: 'cut', label: '자름'}, {value: 'keep', label: '살림'}];
const CUT_DEFAULTS = {min_silence: 0.5, pad_before: 0.15, pad_after: 0.25, fillers: true};
const PRE = 1.5, POST = 1.5, ROW_H = 52;

export async function mount(el, store) {
  await ensureCss();
  let filter = 'all', words = null, wordsN = -1, wordsVer = 0, pv = null, items = [], all = [];
  const sticky = new Set();   // '미확인' 거르기 중에 결정한 컷은 거르기를 바꿀 때까지 보여요

  // ---------- 틀 ----------
  const prog = bar(0);
  const count = h('span.pn-head-n.tc');
  const gear = iconBtn('settings', '컷 찾기 설정', () => openSettings());
  const more = iconBtn('more', '더 보기', () => openMore());
  const chipsEl = chips(FILTERS.map(f => ({...f, n: 0})), filter, v => setFilter(v), {label: '컷 거르기'});
  const filtersEl = h('div.pn-filters', {}, chipsEl);
  const reviewEl = h('div.pn-review');
  const scroller = h('div.pn-scroll', {role: 'listbox', 'aria-label': '컷 목록'});
  const emptyEl = h('div.empty', {hidden: true});
  el.append(h('div.pn.pn-cuts', {},
    h('div.phead', {}, prog, count, gear, more),
    filtersEl,
    reviewEl,
    h('div', {style: 'position:relative;flex:1;min-height:0;display:flex;flex-direction:column'}, scroller, emptyEl)));
  emptyEl.style.cssText = 'position:absolute;inset:0;height:auto';

  noFocus(el);
  const vl = new VList(scroller, {height: ROW_H, create: createRow, update: updateRow});

  // ---------- 줄 ----------
  function createRow() {
    const r = h('div.row.pn-cut', {role: 'option'});
    r._dot = h('i.unread');
    r._bar = h('span.type-bar');
    r._t = h('span.tc.t');
    r._len = h('span.tc.len');
    r._ty = h('span.ty');
    r._eq = h('span.eq', {}, icon('audio-lines', 14));
    r._sub = h('div.sub');
    r._pill = h('button.pill', {type: 'button', dataset: {act: 'toggle'}});
    r.append(r._dot, r._bar, h('div.meta', {}, h('div.top', {}, r._t, r._len, r._ty, r._eq), r._sub), r._pill);
    return r;
  }
  function updateRow(r, c) {
    r.dataset.id = c.id;
    if (r.dataset.type !== c.type) r.dataset.type = c.type;
    const sel = store.sel?.kind === 'cut' && store.sel.id === c.id;
    toggle(r, 'sel', sel);
    r.setAttribute('aria-selected', String(sel));
    toggle(r, 'off', !c.enabled);
    toggle(r, 'playing', playingId() === c.id);
    r._dot.hidden = !!c.reviewed;
    setText(r._t, fmt(c.start, 1));
    setText(r._len, fmtDur(c.end - c.start));
    setText(r._ty, TYPE_LABEL[c.type] || c.type);
    setText(r._pill, c.enabled ? '자름' : '살림');
    r._pill.className = `pill ${c.enabled ? 'cut' : 'keep'}`;
    if (r._pill.dataset.tip !== (c.enabled ? '살리기' : '자르기')) {
      r._pill.dataset.tip = c.enabled ? '살리기' : '자르기';
      r._pill.dataset.key = c.enabled ? 'v' : 'x';
    }
    const sig = `${c.type}|${c.reason}|${c.by}|${c.start}|${c.end}|${wordsVer}`;
    if (r._sig !== sig) { r._sig = sig; fillSub(r._sub, c); }
    glow(r, store, c.id);
  }
  // 아래 줄: Claude 가 판단한 컷(NG·재촬영·요청 · Claude 가 넣은 컷)은 '✎ Claude · 이유', 나머지는 컷 앞뒤 단어 (잘리는 말은 줄 긋기)
  // 무음·필러의 자동 이유('2.5초 무음')는 안 보여요 (길이와 어긋나요). 인식된 말이 없으면 비워요.
  const CLAUDE_TYPES = new Set(['ng', 'retake', 'manual']);
  function fillSub(sub, c) {
    sub.className = 'sub';
    sub.hidden = false;
    if (c.reason && (CLAUDE_TYPES.has(c.type) || c.by === 'cli')) {
      sub.classList.add('reason');
      sub.replaceChildren(icon('pencil', 12), h('b', {}, 'Claude'), ` · ${c.reason}`);
      return;
    }
    const w = wordsAround(words, c.start, c.end);
    if (!w) { sub.replaceChildren(); sub.hidden = true; return; }
    const txt = ws => ws.map(x => x.word).join(' ');
    sub.replaceChildren(...[
      w.before.length ? h('span', {}, `…${txt(w.before)} `) : null,
      w.inside.length ? h('span.in', {}, txt(w.inside) + (w.more ? '…' : '')) : h('i.gapmark'),
      w.after.length ? h('span', {}, ` ${txt(w.after)}…`) : null].filter(Boolean));
  }

  // ---------- 목록 · 머리 ----------
  const allCuts = () => (store.project?.cuts || []).filter(c => c.type !== 'clip');
  function pick(list) {
    if (filter === 'todo') return list.filter(c => !c.reviewed || sticky.has(c.id));
    if (filter === 'cut') return list.filter(c => c.enabled);
    if (filter === 'keep') return list.filter(c => !c.enabled);
    return list;
  }
  let headKey = '';
  const sync = frame(() => {
    all = allCuts();
    items = pick(all);
    vl.setItems(items);
    const done = all.filter(c => c.reviewed).length, cut = all.filter(c => c.enabled).length;
    const key = `${done}|${all.length}|${cut}|${filter}|${store.review}`;
    if (key !== headKey) {
      headKey = key;
      prog.hidden = count.hidden = filtersEl.hidden = reviewEl.hidden = !all.length;
      prog.set(all.length ? done / all.length : 0);
      toggle(prog, 'done', all.length > 0 && done === all.length);
      count.replaceChildren(h('b', {}, String(done)), ` / ${all.length}`);
      count.dataset.tip = '확인한 컷';
      const n = [all.length, all.length - done, cut, all.length - cut];
      chipsEl.buttons.forEach((b, i) => setText(b.querySelector('.n'), n[i]));
      renderReview(all.length - done);
    }
    renderEmpty();
  });
  let emptyKey = '';
  function renderEmpty() {
    const none = !items.length;
    emptyEl.hidden = !none;
    if (!none) return;
    const busy = (store.project?.sources || []).some(s => s.status !== 'ready' && s.status !== 'error');
    const key = `${all.length > 0}|${filter}|${busy}`;
    if (key === emptyKey) return;
    emptyKey = key;
    if (!all.length) {
      emptyEl.replaceChildren(...[icon('scissors', 26), h('div.t', {}, busy ? '분석이 끝나면 보여요' : '컷이 없어요'),
        busy ? null : btn('컷 찾기', {icon: 'refresh', cls: 'sm', onClick: () => openSettings()})].filter(Boolean));
    } else {
      emptyEl.replaceChildren(icon(filter === 'todo' ? 'check-circle' : 'filter', 24),
        h('div.t', {}, {todo: '모두 확인했어요', cut: '자른 컷이 없어요', keep: '살린 컷이 없어요'}[filter] || '없어요'));
    }
  }
  function setFilter(v) {
    if (v !== filter) sticky.clear();
    filter = v;
    chipsEl.set(v);
    headKey = '';
    sync.now();
    const s = store.sel?.kind === 'cut' ? store.sel.id : null;
    if (s) vl.reveal(s); else scroller.scrollTop = 0;
  }

  // 차례로 확인 버튼 / 확인 중 막대
  function renderReview(left) {
    if (store.review) {
      reviewEl.replaceChildren(h('div.pn-rbar', {role: 'status'},
        h('i.live'), h('span', {}, '확인 중'), h('span.left', {}, `${left} 남음`), h('span.sp'),
        h('button.btn.cut', {type: 'button', tip: '자르기', key: 'x', onclick: () => store.command('cut')}, '자르기', kbd('x')),
        h('button.btn', {type: 'button', tip: '살리기', key: 'v', onclick: () => store.command('keep')}, '살리기', kbd('v')),
        iconBtn('x', '끝내기', () => exitReview(), {key: 'esc', cls: 'icon'})));
      return;
    }
    const b = h('button.btn.block', {type: 'button', class: left ? '' : 'done', disabled: !all.length || !left, onclick: () => enterReview()},
      icon(left ? 'play' : 'check-circle', 16), h('span', {}, left ? '차례로 확인' : '모두 확인'), left ? h('span.badge', {}, left > 99 ? '99+' : String(left)) : null);
    reviewEl.replaceChildren(b);
  }

  // ---------- 결정 · 이동 ----------
  const selCut = () => (store.sel?.kind === 'cut' ? store.item('cut', store.sel.id) : null);
  function decide(c, enabled) {
    if (filter === 'todo') sticky.add(c.id);
    store.edit([{op: 'put', coll: 'cuts', item: {id: c.id, enabled, reviewed: true}}], enabled ? '자르기' : '살리기');
  }
  function nextTodo(fromId) {
    const list = allCuts(), i = list.findIndex(c => c.id === fromId);
    for (let k = 0; k < list.length; k++) {
      const c = list[(i + 1 + k) % list.length];
      if (!c.reviewed) return c;
    }
    return null;
  }
  function goTo(c, play) {
    store.select('cut', c.id, {reveal: true});
    vl.reveal(c.id);
    if (play) startPreview(c); else store.setTime(c.start, 'select');
  }
  function enterReview() {
    if (filter === 'cut' || filter === 'keep') setFilter('all');
    const cur = selCut();
    const first = cur && !cur.reviewed ? cur : nextTodo(cur?.id ?? null);
    if (!first) { toast('모두 확인했어요', {kind: 'ok', icon: 'check'}); return; }
    store.setTab('cuts');
    goTo(first, true);
    store.setView({review: true});
  }
  function exitReview() {
    if (store.review) store.setView({review: false});
    stopPreview(true);
  }
  function advance(c) {
    const n = nextTodo(c.id);
    if (n) return goTo(n, true);
    exitReview();
    toast('모두 확인했어요', {kind: 'ok', icon: 'check'});
  }
  function move(dir) {
    const list = items, sel = store.sel?.kind === 'cut' ? store.sel.id : null;
    if (!list.length) return;
    const i = list.findIndex(c => c.id === sel), t = store.time;
    let j = i < 0 ? (dir > 0 ? list.findIndex(c => c.start >= t - 1e-3) : list.findLastIndex(c => c.start < t - 1e-3)) : clamp(i + dir, 0, list.length - 1);
    if (j < 0) j = dir > 0 ? list.length - 1 : 0;   // 고른 컷이 없으면 재생 위치 근처에서 시작해요
    if (j === i) return;
    if (store.review) goTo(list[j], true);
    else { store.select('cut', list[j].id, {seek: true, reveal: true}); vl.reveal(list[j].id); }
  }

  // ---------- 듣기 (1.5초 앞 → 컷 → 1.5초 뒤) ----------
  const playingId = () => store.previewing ? store.previewing.id : pv?.own ? pv.id : null;
  function startPreview(c) {
    const from = Math.max(0, c.start - PRE), to = Math.min(store.total, c.end + POST);
    const all0 = pv?.own ? pv.all : store.playAll;
    pv = null;
    const handled = store.command('preview', {id: c.id, start: c.start, end: c.end, from, to});
    pv = {id: c.id, from, to, own: !handled, all: all0};
    if (!handled) {
      if (!store.playAll) store.setView({playAll: true}, 'preview');
      store.setTime(from, 'preview');
      store.setPlaying(true, 'preview');
    }
    vl.refresh();
  }
  function stopPreview(pause) {
    if (!pv) return;
    const p = pv;
    pv = null;
    if (pause && store.playing) {
      if (!p.own && store.player?.stop) store.player.stop();
      else store.setPlaying(false, 'preview');
    }
    if (p.own && !p.all && store.playAll) store.setView({playAll: false}, 'preview');
    vl.refresh();
  }
  store.on('time', t => {
    if (!pv?.own) return;
    if (t >= pv.to - 0.02) stopPreview(true);
    else if (t < pv.from - 0.75 || t > pv.to + 0.75) stopPreview(false);
  });
  store.on('play', v => { if (!v && pv) stopPreview(false); });
  store.on('previewing', () => vl.refresh());

  // ---------- 설정 · 메뉴 ----------
  function openSettings() {
    const st = {...CUT_DEFAULTS, ...(store.project?.settings?.cut || {})}, v = {...st};
    const sec = x => `${(+x).toFixed(2)}s`;
    const go = btn('다시 찾기', {icon: 'refresh', cls: 'primary block', onClick: run});
    const body = h('div', {style: {width: '264px'}},
      h('div.pop-title', {}, '컷 찾기'),
      h('div.sliders', {},
        sliderRow({label: '무음 길이', min: 0.2, max: 2, step: 0.05, value: st.min_silence, format: sec, onInput: x => { v.min_silence = x; }}),
        sliderRow({label: '앞 여유', min: 0, max: 0.5, step: 0.01, value: st.pad_before, format: sec, onInput: x => { v.pad_before = x; }}),
        sliderRow({label: '뒤 여유', min: 0, max: 1, step: 0.01, value: st.pad_after, format: sec, onInput: x => { v.pad_after = x; }}),
        switchRow('필러', {checked: !!st.fillers, onChange: x => { v.fillers = x; }})),
      h('div.pop-foot', {}, go));
    popover(gear, body, {align: 'end'});
    async function run() {
      go.disabled = true;
      go.replaceChildren(h('span.ring.spin', {style: {width: '16px', height: '16px'}}), h('span', {}, '찾는 중'));
      try {
        await store.action('컷 다시 찾기', () => api.detectCuts(store.name,
          {min_silence: v.min_silence, pad_before: v.pad_before, pad_after: v.pad_after, fillers: v.fillers}));
        closePopover();
        toast(`컷 ${allCuts().length}개`, {icon: 'scissors', action: {label: '되돌리기', onClick: () => store.undo()}});
      } catch (e) {
        toast(e.message, {kind: 'error'});
        go.disabled = false;
        go.replaceChildren(icon('refresh', 16), h('span', {}, '다시 찾기'));
      }
    }
  }
  function openMore() {
    const todo = allCuts().filter(c => !c.reviewed);
    const bulk = enabled => {
      const ops = allCuts().filter(c => !c.reviewed).map(c => ({op: 'put', coll: 'cuts', item: {id: c.id, enabled, reviewed: true}}));
      if (!ops.length) return;
      const label = enabled ? '미확인 모두 자르기' : '미확인 모두 살리기';
      store.edit(ops, label);
      toast(`${ops.length}개 ${enabled ? '자름' : '살림'}`, {icon: enabled ? 'scissors' : 'check', action: {label: '되돌리기', onClick: () => store.undo()}});
    };
    menu(more, [
      {label: '미확인 모두 자르기', icon: 'scissors', disabled: !todo.length, onClick: () => bulk(true)},
      {label: '미확인 모두 살리기', icon: 'check', disabled: !todo.length, onClick: () => bulk(false)},
    ], {align: 'end'});
  }

  // ---------- 마우스 ----------
  scroller.addEventListener('click', e => {
    const r = e.target.closest('.pn-cut');
    const c = r && store.item('cut', r.dataset.id);
    if (!c) return;
    if (e.target.closest('[data-act=toggle]')) {
      store.select('cut', c.id);
      decide(c, !c.enabled);
      if (store.review) advance(c);
      return;
    }
    if (store.review) goTo(c, true);
    else store.select('cut', c.id, {seek: true});
  });
  scroller.addEventListener('dblclick', e => {
    const r = e.target.closest('.pn-cut');
    const c = r && !e.target.closest('button') && store.item('cut', r.dataset.id);
    if (c) decide(c, !c.enabled);
  });

  // ---------- 상태 → 화면 ----------
  async function loadWords() {
    if (store.wordsCount === wordsN && words) return;
    wordsN = store.wordsCount;
    if (!wordsN) { words = null; wordsVer++; vl.refresh(); return; }
    try { words = wordIndex(await store.words()); wordsVer++; vl.refresh(); } catch { /* 단어 없이도 보여요 */ }
  }
  store.on('project', e => {
    if (e.colls && !e.colls.has('cuts') && !e.colls.has('sources')) return;
    if (!e.colls) loadWords();
    sync();
  });
  store.on('sel', (s, prev) => {
    for (const x of [prev, s]) if (x?.kind === 'cut') { const r = vl.el(x.id), c = store.item('cut', x.id); if (r && c) updateRow(r, c); }
    if (s?.kind === 'cut') vl.reveal(s.id);
    if (pv && !(s?.kind === 'cut' && (s.id === pv.id || store.review))) stopPreview(true);
  });
  store.on('view', keys => {
    if (!keys.includes('review')) return;
    headKey = '';
    sync();
    if (!store.review) { stopPreview(true); return; }
    if (!selCut()) { const n = nextTodo(null); if (n) goTo(n, true); }
  });
  store.on('external', () => vl.refresh());

  // ---------- 명령 ----------
  const onDecide = enabled => ctx => {
    const c = selCut();
    if (!c || ctx.handled) return;
    ctx.handled = true;
    decide(c, enabled);
    if (store.review) advance(c);
  };
  store.on('cmd:cut', onDecide(true));
  store.on('cmd:keep', onDecide(false));
  store.on('cmd:listen', ctx => {   // 속성 패널의 듣기 버튼은 컷 id 를 같이 보내요
    const a = ctx.arg, c = (typeof a === 'string' && store.item('cut', a)) || (a?.id && store.item('cut', a.id)) || selCut();
    if (!c || ctx.handled) return;
    ctx.handled = true;
    if (pv?.id === c.id) stopPreview(true); else startPreview(c);
  });
  store.on('cmd:nav', ctx => {
    if (store.tab !== 'cuts' || ctx.handled) return;
    ctx.handled = true;
    move(ctx.arg > 0 ? 1 : -1);
  });

  sync.now();
  loadWords();
  if (store.sel?.kind === 'cut') vl.reveal(store.sel.id, 'center');
}

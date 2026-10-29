// 미디어 탭: 가져오기 · 클립 카드(첫 장면 · 길이 · 순서 · 분석 진행 · 오류와 다시) · 끌어서 순서 바꾸기(되돌리기 가능) · ⋯ 다시 분석 / 빼기
// 다시 분석은 POST /api/p/<n>/sources {analyze: sid} 를 써요.
import { api, media } from '../api.js';
import { h, iconBtn, btn, menu, popover, toast, ring, closePopover, clamp } from '../ui.js';
import { icon } from '../icons.js';
import { fmt } from '../time.js';
import { ensureCss, frame, setText, toggle, noFocus } from './common.js';

// 타임라인 블록과 같은 말: '대기' · '분석 중 33%' (진행률은 클립마다)
const stepWord = s => s.status === 'queued' ? '대기' : `분석 중 ${Math.round((+s.progress || 0) * 100)}%`;

export async function mount(el, store) {
  await ensureCss();
  const cards = new Map();   // sid → 카드
  const ver = new Map();     // sid → 썸네일 판 (분석이 끝날 때마다 올려요)
  let pendingOrder = null, busy = false, lastStatus = new Map();

  const importBtn = h('button.btn.sm', {type: 'button', onclick: () => importClips()}, icon('plus', 16, 'lead-ico'), h('span', {}, '가져오기'));
  importBtn.dataset.tip = '영상 가져오기';
  const sum = h('span.pn-head-n.tc');
  const grid = h('div.pn-mgrid', {role: 'list', 'aria-label': '클립'});
  const emptyEl = h('div.empty', {hidden: true}, icon('film', 26), h('div.t', {}, '영상이 없어요'),
    btn('가져오기', {icon: 'plus', cls: 'primary sm', onClick: () => importClips()}));
  const scroller = h('div.pn-scroll', {}, grid, emptyEl);
  el.append(h('div.pn.pn-media', {}, h('div.phead', {}, importBtn, h('span.sp'), sum), scroller));

  noFocus(el);
  // ---------- 카드 ----------
  function makeCard(sid) {
    const c = h('div.mcard', {role: 'listitem', dataset: {id: sid}});
    c._num = h('span.tag.tl');
    c._dur = h('span.tag.br');
    c._ph = h('div.ph', {}, icon('film', 22));
    c._more = iconBtn('more', '더 보기', e => { e.stopPropagation(); openMore(c); }, {cls: 'more'});
    c._thumb = h('div.thumb', {}, c._ph, c._num, c._dur, c._more);
    c._dot = h('i.dot', {hidden: true});
    c._label = h('span');
    c._name = h('div.name', {}, c._dot, c._label);
    c.append(c._thumb, c._name);
    return c;
  }
  function updateCard(c, s, i) {
    setText(c._num, i + 1);
    setText(c._dur, fmt(+s.duration || 0, 0));
    setText(c._label, s.name || s.id);
    const err = s.status === 'error';
    toggle(c, 'err', err);
    c._dot.hidden = !err;
    if (err) c._name.dataset.tip = s.error || '분석하지 못했어요'; else delete c._name.dataset.tip;
    toggle(c, 'sel', isSel(s.id));
    overlay(c, s);
    loadThumb(c, s);
  }
  function isSel(sid) {
    const sel = store.sel;
    if (!sel) return false;
    if (sel.kind === 'source') return sel.id === sid;
    if (sel.kind === 'piece') return store.item('piece', sel.id)?.sid === sid;
    return false;
  }
  // 분석 중: 원형 진행 + 분석 중 N% · 대기 · 오류: 다시 버튼
  function overlay(c, s) {
    const key = `${s.status}|${s.step}|${Math.round((+s.progress || 0) * 100)}`;
    if (c._ovKey === key) return;
    c._ovKey = key;
    c._ov?.remove();
    c._ov = null;
    c._ph.hidden = !!c._cv || (s.status !== 'ready' && !!s.status);
    if (s.status === 'ready' || !s.status) return;
    if (s.status === 'error') {
      c._ov = h('div.ov.err', {}, icon('alert', 20),
        h('button.btn', {type: 'button', onclick: e => { e.stopPropagation(); reanalyze(s.id); }}, icon('refresh', 14), h('span', {}, '다시')));
    } else {
      c._ov = h('div.ov', {}, ring(s.status === 'queued' ? -1 : Math.max(0.03, +s.progress || 0), 30), h('span', {}, stepWord(s)));
    }
    c._thumb.insertBefore(c._ov, c._num);
  }
  // 썸네일 묶음의 첫 칸을 16:9 칸에 그려요 (세로 영상은 흐린 배경 위 가운데)
  async function loadThumb(c, s) {
    if (s.status && s.status !== 'ready' && s.status !== 'error') return;   // 분석이 끝나면 받아요
    const a = store.assets?.[s.id] || {};
    const v = ver.get(s.id) || 0;
    const url = `${a.thumbs || media.source(store.name, s.id, 'thumbs.jpg')}`;
    const key = `${url}|${v}|${s.status === 'ready' ? 'r' : `${s.status}:${s.step}`}`;
    if (c._thumbKey === key) return;
    c._thumbKey = key;
    let meta = a.thumbs_meta && typeof a.thumbs_meta === 'object' ? a.thumbs_meta : null;
    if (!meta) {
      try {
        const r = await fetch(typeof a.thumbs_meta === 'string' ? a.thumbs_meta : url.replace(/\.jpg(\?.*)?$/, '.json'), {cache: 'no-store'});
        if (r.ok) meta = await r.json();
      } catch { /* 없으면 추정 */ }
    }
    const img = new Image();
    img.src = `${url}${url.includes('?') ? '&' : '?'}v=${v}`;
    try { await img.decode(); } catch { return; }   // 아직 없어요 (분석이 끝나면 다시 해요)
    if (c._thumbKey !== key) return;
    const cols = meta?.cols || 10, tw = meta?.w || img.naturalWidth / cols, th = meta?.h || tw * 9 / 16;
    const W = 320, H = 180, cv = h('canvas', {width: W, height: H});
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#0A0A0C';
    ctx.fillRect(0, 0, W, H);
    const cover = Math.max(W / tw, H / th), fit = Math.min(W / tw, H / th), tall = fit < cover * 0.98;
    if (tall && 'filter' in ctx) {
      ctx.filter = 'blur(12px) brightness(.45)';
      ctx.drawImage(img, 0, 0, tw, th, (W - tw * cover) / 2, (H - th * cover) / 2, tw * cover, th * cover);
      ctx.filter = 'none';
    }
    const k = tall ? fit : cover;
    ctx.drawImage(img, 0, 0, tw, th, (W - tw * k) / 2, (H - th * k) / 2, tw * k, th * k);
    c._cv?.remove();
    c._cv = cv;
    c._ph.hidden = true;
    c._thumb.prepend(cv);
  }

  // ---------- 그리기 ----------
  const sources = () => store.project?.sources || [];
  const ordered = () => {
    const list = sources();
    if (!pendingOrder) return list;
    const by = new Map(list.map(s => [s.id, s]));
    const out = pendingOrder.map(id => by.get(id)).filter(Boolean);
    return out.length === list.length ? out : list;
  };
  const render = frame(() => {
    const list = ordered();
    for (const s of list) {   // 분석이 끝나면 썸네일을 새로 받아요
      const was = lastStatus.get(s.id);
      if (was && was !== 'ready' && s.status === 'ready') ver.set(s.id, (ver.get(s.id) || 0) + 1);
      lastStatus.set(s.id, s.status);
    }
    const seen = new Set();
    list.forEach((s, i) => {
      let c = cards.get(s.id);
      if (!c) { c = makeCard(s.id); cards.set(s.id, c); }
      if (grid.children[i] !== c) grid.insertBefore(c, grid.children[i] || null);
      updateCard(c, s, i);
      seen.add(s.id);
    });
    for (const [id, c] of cards) if (!seen.has(id)) { c.remove(); cards.delete(id); }
    emptyEl.hidden = list.length > 0;
    grid.hidden = !list.length;
    const ready = list.filter(s => s.status === 'ready').length;
    setText(sum, list.length ? `${list.length}개 · ${fmt(store.total, 0)}` : '');
    sum.dataset.tip = ready < list.length ? `분석 ${ready}/${list.length}` : '클립 · 원본 길이';
  });

  // ---------- 가져오기 · 다시 분석 · 빼기 ----------
  async function importClips() {
    if (busy) return;
    let paths;
    try { paths = await api.pick('video'); } catch (e) { toast(e.message, {kind: 'error'}); return; }
    if (!paths?.length) return;
    busy = true;
    importBtn.disabled = true;
    try {
      await store.flush();
      await api.addSources(store.name, paths);
      await store.reload();
      toast(`영상 ${paths.length}개 추가`, {kind: 'ok', icon: 'check'});
      requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; });
    } catch (e) { toast(e.message, {kind: 'error'}); }
    finally { busy = false; importBtn.disabled = false; }
  }
  async function reanalyze(sid) {
    try {
      await store.flush();
      await api.sources(store.name, {analyze: sid});
      await store.reload();
    } catch (e) { toast(e.message, {kind: 'error'}); }
  }
  async function remove(sid) {
    try {
      await store.flush();
      await api.removeSource(store.name, sid);
      await store.reload();
      toast('영상을 뺐어요', {icon: 'trash'});
    } catch (e) { toast(e.message, {kind: 'error'}); }
  }
  function openMore(c) {
    const s = sources().find(x => x.id === c.dataset.id);
    if (!s) return;
    const working = s.status === 'queued' || s.status === 'analyzing';
    menu(c._more, [
      {label: '다시 분석', icon: 'refresh', disabled: working, onClick: () => reanalyze(s.id)},
      '-',
      {label: '빼기', icon: 'trash', danger: true, disabled: sources().length <= 1, onClick: () => confirmRemove(c, s)},
    ], {align: 'end'});
  }
  function confirmRemove(c, s) {
    const span = [s.offset ?? 0, (s.offset ?? 0) + (+s.duration || 0)];
    const inside = x => x.start >= span[0] - 1e-6 && x.end <= span[1] + 1e-6;
    const nc = (store.project.cuts || []).filter(inside).length, ns = (store.project.subtitles || []).filter(inside).length;
    popover(c._more, h('div.pn-confirm', {},
      h('div.pop-title', {}, icon('trash', 16), '영상 빼기'),
      nc || ns ? h('div.dim', {style: {fontSize: '12px', marginTop: '-6px'}}, [nc ? `컷 ${nc}` : null, ns ? `자막 ${ns}` : null].filter(Boolean).join(' · ')) : null,
      h('div.pop-foot', {},
        btn('취소', {cls: 'ghost sm', onClick: () => closePopover()}),
        btn('빼기', {cls: 'sm danger', onClick: () => { closePopover(); remove(s.id); }}))), {align: 'end'});
  }

  // ---------- 순서 바꾸기 (포인터로 끌기, 놓으면 저장 · 되돌리기 가능) ----------
  async function reorder(from, to, record = true) {
    pendingOrder = to;
    render.now();
    try {
      await store.flush();
      await api.orderSources(store.name, to);
      await store.reload();
      if (record) {
        store.pushUndo({label: '영상 순서', undo: () => reorder(to, from, false), redo: () => reorder(from, to, false)});
        toast('순서 바꿈', {icon: 'move', action: {label: '되돌리기', onClick: () => store.undo()}});
      }
    } catch (e) { toast(e.message, {kind: 'error'}); }
    finally { pendingOrder = null; render.now(); }
  }
  grid.addEventListener('pointerdown', e => {
    const c = e.target.closest('.mcard');
    if (!c || e.button !== 0 || e.target.closest('button')) return;
    const x0 = e.clientX, y0 = e.clientY, sid = c.dataset.id;
    let d = null;
    c.setPointerCapture(e.pointerId);
    const move = ev => {
      if (!d) {
        if (Math.hypot(ev.clientX - x0, ev.clientY - y0) < 5 || pendingOrder) return;
        d = startDrag(c, ev);
      }
      dragMove(d, ev);
    };
    const end = ev => {
      c.removeEventListener('pointermove', move);
      c.removeEventListener('pointerup', end);
      c.removeEventListener('pointercancel', end);
      removeEventListener('keydown', esc, true);
      if (d) finishDrag(d, ev.type === 'pointerup');
      else if (ev.type === 'pointerup') clickCard(sid);
    };
    const esc = ev => { if (ev.key === 'Escape' && d) { ev.preventDefault(); ev.stopPropagation(); finishDrag(d, false); d = null; } };
    c.addEventListener('pointermove', move);
    c.addEventListener('pointerup', end);
    c.addEventListener('pointercancel', end);
    addEventListener('keydown', esc, true);
  });
  function startDrag(c, ev) {
    closePopover();
    const all = [...grid.children], r = c.getBoundingClientRect();
    const ghost = h('div.pn-ghost', {style: {left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${c._thumb.offsetHeight}px`}});
    if (c._cv) { const g = h('canvas', {width: c._cv.width, height: c._cv.height}); g.getContext('2d').drawImage(c._cv, 0, 0); ghost.append(g); }
    document.body.append(ghost);
    c.classList.add('dragging');
    document.body.style.cursor = 'grabbing';
    return {c, ghost, offX: ev.clientX - r.left, offY: ev.clientY - r.top, slots: all.map(x => x.getBoundingClientRect()),
      order0: all.map(x => x.dataset.id), to: all.indexOf(c)};
  }
  function dragMove(d, ev) {
    d.ghost.style.left = `${ev.clientX - d.offX}px`;
    d.ghost.style.top = `${ev.clientY - d.offY}px`;
    let best = d.to, bd = Infinity;
    d.slots.forEach((r, i) => {
      const dist = Math.hypot(ev.clientX - (r.left + r.width / 2), ev.clientY - (r.top + r.height / 2));
      if (dist < bd) { bd = dist; best = i; }
    });
    if (best === d.to) return;
    const all = [...grid.children], before = new Map(all.map(x => [x, x.getBoundingClientRect()]));
    all.splice(all.indexOf(d.c), 1);
    all.splice(best, 0, d.c);
    all.forEach((x, i) => { if (grid.children[i] !== x) grid.insertBefore(x, grid.children[i] || null); });
    d.to = best;
    const moved = all.filter(x => x !== d.c);
    for (const x of moved) {   // FLIP: 옛 자리에서 새 자리로 미끄러지게
      const a = before.get(x), b = x.getBoundingClientRect();
      x.classList.remove('anim');
      x.style.transform = a.left !== b.left || a.top !== b.top ? `translate(${a.left - b.left}px,${a.top - b.top}px)` : '';
    }
    void grid.offsetWidth;
    for (const x of moved) { x.classList.add('anim'); x.style.transform = ''; }
    all.forEach((x, i) => setText(x._num, i + 1));
  }
  function finishDrag(d, ok) {
    d.ghost.remove();
    d.c.classList.remove('dragging');
    document.body.style.cursor = '';
    const order = [...grid.children].map(x => x.dataset.id);
    if (!ok || order.join() === d.order0.join()) { render.now(); return; }
    reorder(d.order0, order);
  }
  function clickCard(sid) {
    const s = sources().find(x => x.id === sid);
    if (!s) return;
    store.select('source', sid);
    store.setTime(s.offset ?? 0, 'select');
  }

  // ---------- 상태 → 화면 ----------
  store.on('project', e => { if (!e.colls || e.colls.has('sources') || e.colls.has('cuts')) render(); });
  store.on('sel', () => { for (const [id, c] of cards) toggle(c, 'sel', isSel(id)); });
  store.on('cmd:nav', ctx => {
    if (store.tab !== 'media' || ctx.handled) return;
    const list = ordered();
    if (!list.length) return;
    ctx.handled = true;
    const sel = store.sel;
    let i = sel?.kind === 'source' ? list.findIndex(s => s.id === sel.id) : sel?.kind === 'piece' ? list.findIndex(s => s.id === store.item('piece', sel.id)?.sid) : -1;
    i = i < 0 ? (ctx.arg > 0 ? 0 : list.length - 1) : clamp(i + (ctx.arg > 0 ? 1 : -1), 0, list.length - 1);
    clickCard(list[i].id);
    cards.get(list[i].id)?.scrollIntoView({block: 'nearest'});
  });
  render.now();
}

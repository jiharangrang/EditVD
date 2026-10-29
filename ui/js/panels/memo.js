// 메모 탭: Claude 메모(notes) · 메모 목록(시간 · 글 · Claude 답 · 완료) · 재생 위치에 추가(M / + 메모) · 누르면 그 시간으로
// M 은 타임라인이 먼저 받고, 아무도 안 받으면 여기서 새 메모를 열어요.
import { h, iconBtn, btn, toast, glow, clamp } from '../ui.js';
import { icon } from '../icons.js';
import { fmt } from '../time.js';
import { ensureCss, frame, setText, toggle, isEditable, noFocus } from './common.js';

const DRAFT = '__draft';

export async function mount(el, store) {
  await ensureCss();
  const rows = new Map();
  let draft = null, editing = null, notesGlowT = 0;

  const countEl = h('span.pn-head-n');
  const addBtn = btn('메모', {icon: 'plus', cls: 'sm', tip: '메모 추가', key: 'm', onClick: () => addAt()});
  const notes = h('div.pn-notes', {hidden: true});
  const list = h('div.pn-memo-list', {role: 'list', 'aria-label': '메모'});
  const emptyEl = h('div.empty', {hidden: true, style: {height: 'auto', paddingTop: '40px'}}, icon('message-text', 26), h('div.t', {}, '메모가 없어요'),
    btn('메모', {icon: 'plus', cls: 'sm', onClick: () => addAt()}));
  const scroller = h('div.pn-scroll', {}, notes, list, emptyEl);
  const head = h('div.phead', {}, countEl, h('span.sp'), addBtn);   // 메모가 없으면 숨겨요 (빈 화면에 버튼이 하나만 있게)
  el.append(h('div.pn.pn-memo', {}, head, scroller));

  noFocus(el);
  // ---------- Claude 메모 ----------
  function renderNotes() {
    const tx = (store.project?.notes || '').trim();
    notes.hidden = !tx;
    if (notes._tx === tx) return;
    notes._tx = tx;
    const long = tx.length > 180 || tx.split('\n').length > 4;
    let open = !long;
    const more = long ? iconBtn('chevron-down', '펼치기', () => {
      open = !open;
      toggle(notes, 'clamp', !open);
      more.replaceChildren(icon(open ? 'chevron-up' : 'chevron-down', 16));
      more.dataset.tip = open ? '접기' : '펼치기';
    }, {cls: 'ghost xs icon'}) : null;
    notes.replaceChildren(h('div.hd', {}, icon('pencil', 14), h('span', {}, 'Claude'), more), h('div.tx', {}, tx));
    toggle(notes, 'clamp', !open);
  }

  // ---------- 메모 줄 ----------
  function makeRow(id) {
    const r = h('div.row.pn-memo-row', {role: 'listitem', dataset: {id}});
    r._done = h('button.done', {type: 'button', dataset: {act: 'done'}}, icon('check', 12));
    r._time = h('span');
    r._when = h('div.when', {}, icon('map-pin', 13), r._time);
    r._txt = h('div.txt', {dataset: {ph: '메모'}});
    r._replyTx = h('span');
    r._reply = h('div.reply', {hidden: true}, icon('corner-down-left', 13), r._replyTx);
    const edit = iconBtn('pencil', '고치기', null, {cls: 'ghost xs'});
    edit.dataset.act = 'edit';
    const del = iconBtn('trash', '지우기', null, {cls: 'ghost xs'});
    del.dataset.act = 'del';
    r._acts = h('div.acts', {}, edit, del);
    r.append(r._done, h('div.body', {}, r._when, r._txt, r._reply), r._acts);
    return r;
  }
  function updateRow(r, m) {
    const sel = store.sel?.kind === 'comment' && store.sel.id === m.id;
    toggle(r, 'sel', sel);
    toggle(r, 'is-done', !!m.done);
    r._done.setAttribute('aria-pressed', String(!!m.done));
    r._done.dataset.tip = m.done ? '다시 열기' : '완료';
    r._done.hidden = m.id === DRAFT;
    r._acts.hidden = m.id === DRAFT;
    setText(r._time, fmt(store.toEdited(m.t)));
    r._when.dataset.tip = `원본 ${fmt(m.t, 1)}`;
    if (editing?.id !== m.id) setText(r._txt, m.text);
    r._reply.hidden = !m.reply;
    setText(r._replyTx, m.reply || '');
    glow(r, store, m.id);
  }
  const comments = () => {
    const arr = store.project?.comments || [];
    if (!draft) return arr;
    const out = arr.slice(), i = out.findIndex(m => m.t > draft.t);
    out.splice(i < 0 ? out.length : i, 0, draft);
    return out;
  };
  const render = frame(() => {
    const arr = comments(), seen = new Set();
    arr.forEach((m, i) => {
      let r = rows.get(m.id);
      if (!r) { r = makeRow(m.id); rows.set(m.id, r); }
      if (list.children[i] !== r) list.insertBefore(r, list.children[i] || null);
      updateRow(r, m);
      seen.add(m.id);
    });
    for (const [id, r] of rows) if (!seen.has(id)) { r.remove(); rows.delete(id); }
    const all = store.project?.comments || [], open = all.filter(m => !m.done).length;
    setText(countEl, all.length ? `${all.length}개${open && open < all.length ? ` · ${open} 남음` : ''}` : '');
    emptyEl.hidden = arr.length > 0;
    head.hidden = !arr.length;
    renderNotes();
  });

  // ---------- 고치기 ----------
  function startEdit(id) {
    const r = rows.get(id), m = id === DRAFT ? draft : store.item('comment', id);
    if (!r || !m) return;
    if (editing && editing.id !== id) finishEdit(true);
    if (editing?.id === id) { editing.ta.focus(); return; }
    const ta = h('textarea', {rows: 1, placeholder: '메모', 'aria-label': '메모 글'});
    ta.spellcheck = false;
    ta.value = m.text || '';
    editing = {id, ta};
    r._txt.replaceWith(ta);
    const grow = () => { ta.style.height = '0px'; ta.style.height = `${ta.scrollHeight}px`; };
    ta.addEventListener('input', grow);
    ta.addEventListener('keydown', e => {
      e.stopPropagation();
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); finishEdit(true); }
      else if (e.key === 'Escape') { e.preventDefault(); finishEdit(false); }
    });
    ta.addEventListener('blur', () => { if (editing?.ta === ta) finishEdit(true); });
    grow();
    ta.focus({preventScroll: true});
    ta.setSelectionRange(ta.value.length, ta.value.length);
    r.scrollIntoView({block: 'nearest'});
  }
  function finishEdit(save) {
    const ed = editing;
    if (!ed) return;
    editing = null;
    const r = rows.get(ed.id), text = ed.ta.value.trim();
    if (r) ed.ta.replaceWith(r._txt);
    if (ed.id === DRAFT) {
      const t = draft?.t ?? store.time;
      draft = null;
      if (save && text) {
        const [op] = store.edit([{op: 'put', coll: 'comments', item: {t, text}}], '메모 추가');
        store.select('comment', op.item.id);
      }
    } else {
      const m = store.item('comment', ed.id);
      if (save && m && text !== m.text) {
        if (text) store.edit([{op: 'put', coll: 'comments', item: {id: m.id, text}}], '메모 고치기');
        else remove(m);
      }
    }
    render.now();
  }
  function remove(m) {
    store.edit([{op: 'del', coll: 'comments', id: m.id}], '메모 지우기');
    toast('메모를 지웠어요', {icon: 'trash', action: {label: '되돌리기', onClick: () => store.undo()}});
  }
  // 재생 위치에 새 메모 (저장하기 전까지는 목록에만 있어요)
  function addAt() {
    if (editing) finishEdit(true);
    if (store.tab !== 'memo') store.setTab('memo');
    draft = {id: DRAFT, t: store.time, text: '', done: false, reply: ''};
    render.now();
    startEdit(DRAFT);
  }

  // ---------- 마우스 ----------
  list.addEventListener('click', e => {
    const r = e.target.closest('.pn-memo-row');
    if (!r || e.target.tagName === 'TEXTAREA') return;
    const id = r.dataset.id, m = store.item('comment', id);
    const act = e.target.closest('[data-act]')?.dataset.act;
    if (!m) return;
    if (act === 'done') return store.edit([{op: 'put', coll: 'comments', item: {id, done: !m.done}}], m.done ? '메모 다시 열기' : '메모 완료');
    if (act === 'edit') return startEdit(id);
    if (act === 'del') return remove(m);
    store.select('comment', id, {force: true});
    store.setTime(m.t, 'select');
  });
  list.addEventListener('dblclick', e => {
    const r = e.target.closest('.pn-memo-row');
    if (r && !e.target.closest('button,textarea') && r.dataset.id !== DRAFT) startEdit(r.dataset.id);
  });

  // ---------- 상태 → 화면 ----------
  store.on('project', e => {
    if (e.colls && !e.colls.has('comments') && !e.colls.has('notes') && !e.colls.has('cuts') && !e.colls.has('sources')) return;
    render();
  });
  store.on('sel', (s, prev) => {
    for (const x of [prev, s]) if (x?.kind === 'comment') { const r = rows.get(x.id), m = store.item('comment', x.id); if (r && m) updateRow(r, m); }
    if (s?.kind !== 'comment') return;
    render.now();
    const m = store.item('comment', s.id), r = rows.get(s.id);
    r?.scrollIntoView({block: 'nearest'});
    // 다른 곳(타임라인 M)에서 만든 빈 메모면 여기서 바로 쓰게 해요
    if (m && !m.text && !isEditable(document.activeElement)) {
      if (store.tab !== 'memo') store.setTab('memo');
      requestAnimationFrame(() => startEdit(s.id));
    }
  });
  store.on('external', ({notes: changed}) => {
    if (!changed) return;
    render.now();
    clearTimeout(notesGlowT);
    notes.classList.remove('glow');
    void notes.offsetWidth;
    notes.classList.add('glow');
    notesGlowT = setTimeout(() => notes.classList.remove('glow'), 8000);
  });

  // ---------- 명령 ----------
  store.on('cmd:addMemo', ctx => { queueMicrotask(() => { if (!ctx.handled) { ctx.handled = true; addAt(); } }); });
  store.on('cmd:nav', ctx => {
    if (store.tab !== 'memo' || ctx.handled) return;
    const arr = store.project?.comments || [];
    if (!arr.length) return;
    ctx.handled = true;
    const i = store.sel?.kind === 'comment' ? arr.findIndex(m => m.id === store.sel.id) : -1;
    const j = i < 0 ? (ctx.arg > 0 ? 0 : arr.length - 1) : clamp(i + (ctx.arg > 0 ? 1 : -1), 0, arr.length - 1);
    if (j === i) return;
    store.select('comment', arr[j].id);
    store.setTime(arr[j].t, 'select');
  });
  store.on('cmd:listen', ctx => {   // Enter: 고른 메모 고치기
    if (ctx.handled || store.tab !== 'memo' || store.sel?.kind !== 'comment') return;
    ctx.handled = true;
    startEdit(store.sel.id);
  });

  render.now();
}

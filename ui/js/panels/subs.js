// 자막 탭: 비었을 때 두 갈래(AI로 만들기 · 대본 불러오기) · 화자 칩(이름·색·기본 위치) · 타이밍 설정(바로 적용) · 가상 목록(제자리 고치기)
// 명령: nav (↑/↓, 자막 탭일 때) · speaker n (1–9) · listen (Enter: 고르면 글 고치기)
import { api } from '../api.js';
import { h, iconBtn, btn, menu, popover, toast, sliderRow, sheet, bar, ring, glow, closePopover, clamp } from '../ui.js';
import { icon } from '../icons.js';
import { fmt } from '../time.js';
import { SPEAKER_COLORS, DEFAULT_POS } from '../store.js';
import { ensureCss, frame, VList, setText, toggle, pickFile, isEditable, noFocus } from './common.js';

const SUBS_DEFAULTS = {lead: 0, hold: 0.3, min: 0.8, bridge: 0.3, max_chars: 18};
const SUB_FIELDS = ['start', 'end', 'text', 'speech', 'speaker', 'pos', 'locked', 'check'];
// 되돌리기용 자막 사본 (서버가 받는 필드만)
const plain = s => JSON.parse(JSON.stringify(Object.fromEntries([['id', s.id], ...SUB_FIELDS.filter(k => k in s).map(k => [k, s[k]])])));
const LINE = 20, TA_PAD = 4, CHROME = 33;   // 줄 높이 = CHROME + 글상자(LINE × 줄 수 + TA_PAD)
const SCRIPT_ACCEPT = '.srt,.vtt,.txt,text/plain';
const LETTERS = [...'ABCDEFGHIJKLMNOPQRSTUVWXYZ'];

export async function mount(el, store) {
  await ensureCss();
  let items = [], editing = null, nowId = null, genBusy = false, genP = -1, spkKey = '';

  // ---------- 틀 ----------
  const countEl = h('span.pn-head-n');
  const addBtn = iconBtn('text-plus', '자막 추가', () => addLine(), {key: 't'});
  const timeBtn = iconBtn('settings', '자막 타이밍', () => openTiming());
  const moreBtn = iconBtn('more', '더 보기', () => openMore());
  const head = h('div.phead', {}, countEl, h('span.sp'), addBtn, timeBtn, moreBtn);
  const genText = h('span'), genBar = bar(0);
  const genEl = h('div.pn-gen', {hidden: true, role: 'status'}, genText, genBar);   // 자막이 있을 때 다시 만드는 진행
  const spkRow = h('div.pn-spk.chips.nowrap', {role: 'toolbar', 'aria-label': '화자'});
  const scroller = h('div.pn-scroll', {role: 'listbox', 'aria-label': '자막 목록'});
  const aiCard = h('button.choice', {type: 'button', onclick: () => generate('AI 자막')});
  const scCard = h('button.choice', {type: 'button', onclick: () => importFile()},
    h('span.ico', {}, icon('file-text', 22)), h('span', {}, '대본 불러오기'), h('small', {}, 'SRT · VTT · TXT'));
  const emptyView = h('div.pn-empty-subs', {},
    h('div.pn-choices', {}, aiCard, scCard),
    h('div.pn-paste', {}, btn('붙여넣기', {icon: 'clipboard', cls: 'ghost sm', onClick: () => openPaste()})));
  const root = h('div.pn.pn-subs', {}, head, genEl, spkRow, scroller, emptyView);
  el.append(root);

  noFocus(el);
  // ---------- 줄 높이 추정 (그린 뒤 실제 높이로 고쳐요) ----------
  let ctx2d = null, avail = 0;
  const lineCache = new Map();
  function estLines(text) {
    if (!text) return 1;
    let n = lineCache.get(text);
    if (n) return n;
    if (!ctx2d) { ctx2d = document.createElement('canvas').getContext('2d'); ctx2d.font = `13px ${getComputedStyle(document.body).fontFamily}`; }
    const w = avail || Math.max(140, vl.width - 56);
    n = 0;
    for (const par of text.split('\n')) n += Math.max(1, Math.ceil((ctx2d.measureText(par).width + 2) / w));
    if (lineCache.size > 4000) lineCache.clear();
    lineCache.set(text, n);
    return n;
  }
  const vl = new VList(scroller, {
    height: s => CHROME + TA_PAD + LINE * estLines(s.text),
    sig: s => s.text,
    create: createRow, update: updateRow,
    measure: rows => {
      if (!avail && rows[0]) avail = rows[0]._ta.clientWidth - 12;
      for (const r of rows) r._ta.style.height = '0px';
      const hs = rows.map(r => r._ta.scrollHeight);
      rows.forEach((r, k) => { r._ta.style.height = `${hs[k]}px`; });
      return hs.map(x => x + CHROME);
    },
  });
  vl.onWidth = () => { avail = 0; lineCache.clear(); };

  // ---------- 줄 ----------
  function createRow() {
    const r = h('div.row.pn-sub', {role: 'option'});
    r._spk = h('button.spk', {type: 'button', dataset: {act: 'spk'}}, h('i'));
    r._tm = h('span.tm', {dataset: {act: 'seek'}});
    r._chk = h('button.chk', {type: 'button', dataset: {act: 'chk'}, tip: '확인 필요'}, h('i'));
    r._lock = h('button.lk', {type: 'button', dataset: {act: 'unlock'}, tip: '자동 타이밍 켜기'}, icon('lock', 13));
    r._ta = h('textarea', {rows: 1, placeholder: '자막', 'aria-label': '자막 글'});
    r._ta.spellcheck = false;
    r.append(r._spk, r._tm, h('div.ic', {}, r._chk, r._lock), r._ta);
    return r;
  }
  function updateRow(r, s) {
    r.dataset.id = s.id;
    const sel = store.sel?.kind === 'sub' && store.sel.id === s.id;
    toggle(r, 'sel', sel);
    r.setAttribute('aria-selected', String(sel));
    toggle(r, 'now', s.id === nowId);
    toggle(r, 'dimmed', store.isHidden(s));
    const sp = s.speaker ? store.speaker(s.speaker) : null, color = s.speaker ? store.speakerColor(s.speaker) : '';
    if (r._color !== color) { r._color = color; r._spk.style.setProperty('--dot', color || null); }
    const tipText = sp ? sp.name : s.speaker ? `화자 ${s.speaker}` : '화자 없음';
    if (r._spk.dataset.tip !== tipText) { r._spk.dataset.tip = tipText; r._spk.setAttribute('aria-label', tipText); }
    setText(r._tm, fmt(store.toEdited(s.start)));
    r._chk.hidden = !s.check;
    r._lock.hidden = !s.locked;
    if (document.activeElement !== r._ta && r._ta.value !== s.text) r._ta.value = s.text;
    glow(r, store, s.id);
  }

  // ---------- 목록 · 머리 ----------
  const sync = frame(() => {
    const p = store.project;
    items = p?.subtitles || [];
    const empty = !items.length;
    head.hidden = spkRow.hidden = scroller.hidden = empty;
    emptyView.hidden = !empty;
    renderGen();
    if (empty) { if (editing) editing = null; renderAi(); return; }
    vl.setItems(items);
    setText(countEl, `${items.length}줄`);
    renderSpeakers();
    updateNow(store.time);
  });
  function renderSpeakers() {
    const sp = store.project?.speakers || [], counts = new Map();
    for (const s of items) if (s.speaker) counts.set(s.speaker, (counts.get(s.speaker) || 0) + 1);
    const key = JSON.stringify(sp.map(s => [s.id, s.name, s.color, counts.get(s.id) || 0]));
    if (key === spkKey) return;
    spkKey = key;
    spkRow.replaceChildren(...sp.map(s => h('button.chip', {type: 'button', dataset: {id: s.id}, tip: s.name},
      h('span.dot', {style: {'--dot': s.color}}), h('span', {}, s.name), h('span.n', {}, counts.get(s.id) || 0))),
    sp.length ? h('button.chip.add', {type: 'button', dataset: {act: 'add'}, tip: '화자 추가', 'aria-label': '화자 추가'}, icon('plus', 14))
      : h('button.chip', {type: 'button', dataset: {act: 'add'}}, icon('plus', 14), h('span', {}, '화자')));
  }
  spkRow.addEventListener('click', e => {
    const b = e.target.closest('.chip');
    if (!b) return;
    if (b.dataset.act === 'add') return addSpeaker();
    openSpeaker(b, b.dataset.id);
  });

  // 지금 재생 위치의 줄 (재생 중이면 목록이 따라가요)
  function updateNow(t) {
    let lo = 0, hi = items.length - 1, r = -1, id = null;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (items[m].start <= t) { r = m; lo = m + 1; } else hi = m - 1; }
    for (let k = r; k >= 0 && k > r - 6; k--) if (items[k].end > t) { id = items[k].id; break; }
    if (id === nowId) return;
    const old = nowId && vl.el(nowId);
    nowId = id;
    if (old) old.classList.remove('now');
    const cur = id && vl.el(id);
    if (cur) cur.classList.add('now');
    follow();
  }
  // 목록을 2초 안에 만졌거나(스크롤·클릭·키) 글을 고치는 중이면 따라가지 않아요
  let touchedAt = -1e9;
  for (const ev of ['wheel', 'pointerdown', 'touchstart', 'keydown']) scroller.addEventListener(ev, () => { touchedAt = performance.now(); }, {passive: true});
  function follow() {
    if (!nowId || !store.playing || store.tab !== 'subs' || editing || performance.now() - touchedAt < 2000) return;
    vl.reveal(nowId);
  }

  // ---------- 고치기 (Enter 저장·다음 줄 · Shift+Enter 줄바꿈 · Esc 취소) ----------
  const rowOf = ta => ta.closest('.pn-sub');
  scroller.addEventListener('focusin', e => {
    if (e.target.tagName !== 'TEXTAREA') return;
    begin(rowOf(e.target).dataset.id);
  });
  scroller.addEventListener('focusout', e => {
    if (e.target.tagName !== 'TEXTAREA') return;
    const id = rowOf(e.target)?.dataset.id;
    if (editing?.id === id) commit();
  });
  scroller.addEventListener('input', e => {
    if (e.target.tagName !== 'TEXTAREA') return;
    const r = rowOf(e.target), id = r.dataset.id;
    if (editing?.id !== id) begin(id);
    if (!editing) return;
    editing.g ||= store.gesture();
    editing.g.update([{op: 'put', coll: 'subtitles', item: {id, text: e.target.value}}]);
    vl.resized(id);
  });
  scroller.addEventListener('keydown', e => {
    if (e.target.tagName !== 'TEXTAREA' || e.isComposing || e.keyCode === 229) return;
    const id = rowOf(e.target).dataset.id;
    if (e.key === 'Enter' && !e.shiftKey && !e.altKey) { e.preventDefault(); commit(); focusLine(id, 1); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancel(e.target); }
    else if (e.key === 'Tab') { e.preventDefault(); commit(); focusLine(id, e.shiftKey ? -1 : 1); }
  });
  function begin(id) {
    if (editing?.id === id) return;
    if (editing) commit();
    const s = store.item('sub', id);
    if (!s) return;
    editing = {id, orig: s.text, g: null};
    if (store.sel?.kind !== 'sub' || store.sel.id !== id) store.select('sub', id, {seek: true});
  }
  function commit() {
    const ed = editing;
    editing = null;
    if (!ed?.g) return;
    const s = store.item('sub', ed.id);
    if (!s || s.text === ed.orig) ed.g.cancel(); else ed.g.commit('자막 고치기');
  }
  function cancel(ta) {
    const ed = editing;
    editing = null;
    if (ed?.g) ed.g.cancel();
    if (ed) ta.value = ed.orig;
    vl.resized(rowOf(ta).dataset.id);
    ta.blur();
  }
  // 옆 줄 글상자로 (dir: 1 다음, -1 앞)
  function focusLine(id, dir) {
    const i = vl.index.get(id), j = i == null ? -1 : i + dir;
    if (j < 0 || j >= items.length) { document.activeElement?.blur?.(); return; }
    const next = items[j];
    vl.reveal(next.id);
    const r = vl.el(next.id);
    if (!r) return;
    r._ta.focus({preventScroll: true});
    const n = r._ta.value.length;
    r._ta.setSelectionRange(n, n);
  }

  // ---------- 마우스 ----------
  scroller.addEventListener('click', e => {
    const r = e.target.closest('.pn-sub');
    const s = r && store.item('sub', r.dataset.id);
    if (!s) return;
    const act = e.target.closest('[data-act]')?.dataset.act;
    if (act === 'spk') return cycleSpeaker(s);
    if (act === 'chk') return store.edit([{op: 'put', coll: 'subtitles', item: {id: s.id, check: false}}], '확인');
    if (act === 'unlock') return store.edit([{op: 'put', coll: 'subtitles', item: {id: s.id, locked: false}}], '자동 타이밍');
    if (e.target.tagName === 'TEXTAREA') return;
    store.select('sub', s.id, {seek: true, force: act === 'seek'});
  });

  // ---------- 화자 ----------
  const speakers = () => store.project?.speakers || [];
  const freeLetter = () => LETTERS.find(c => !speakers().some(s => s.id === c));
  function assign(s, spk) {
    const ops = [];
    if (spk && !store.speaker(spk)) ops.push({op: 'put', coll: 'speakers', item: {id: spk}});
    ops.push({op: 'put', coll: 'subtitles', item: {id: s.id, speaker: spk}});
    store.edit(ops, '화자');
  }
  function cycleSpeaker(s) {
    const sp = speakers();
    if (!sp.length) return assign(s, 'A');
    const i = sp.findIndex(x => x.id === s.speaker);
    assign(s, i < 0 ? sp[0].id : i + 1 < sp.length ? sp[i + 1].id : null);
  }
  function addSpeaker() {
    const id = freeLetter();
    if (!id) return toast('화자는 26명까지예요', {kind: 'error'});
    store.edit([{op: 'put', coll: 'speakers', item: {id}}], '화자 추가');
    sync.now();
    const chip = spkRow.querySelector(`.chip[data-id="${id}"]`);
    if (chip) { chip.scrollIntoView({block: 'nearest', inline: 'nearest'}); openSpeaker(chip, id, true); }
  }
  function openSpeaker(anchor, id, fresh = false) {
    const s = store.speaker(id);
    if (!s) return;
    const name = h('input.input.sm', {value: s.name, maxlength: 40, 'aria-label': '화자 이름', autofocus: fresh});
    name.spellcheck = false;
    const saveName = () => {
      const v = name.value.trim(), cur = store.speaker(id);
      if (cur && v && v !== cur.name) store.edit([{op: 'put', coll: 'speakers', item: {id, name: v}}], '화자 이름');
    };
    name.addEventListener('change', saveName);
    name.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); saveName(); closePopover(); } });
    const del = iconBtn('trash', '화자 지우기', () => {
      const ops = (store.project.subtitles || []).filter(t => t.speaker === id).map(t => ({op: 'put', coll: 'subtitles', item: {id: t.id, speaker: null}}));
      ops.push({op: 'del', coll: 'speakers', id});
      closePopover();
      store.edit(ops, '화자 지우기');
    }, {cls: 'ghost sm'});
    const sw = h('div.pn-swatches', {role: 'group', 'aria-label': '색'}, SPEAKER_COLORS.map(c => h('button', {type: 'button', style: {'--c': c},
      'aria-label': c, 'aria-pressed': String(c.toLowerCase() === (s.color || '').toLowerCase()),
      onclick: e => {
        for (const b of sw.children) b.setAttribute('aria-pressed', String(b === e.currentTarget));
        pad.style.setProperty('--c', c);
        store.edit([{op: 'put', coll: 'speakers', item: {id, color: c}}], '화자 색');
      }})));
    const pad = posPad(id, s);
    // 줄마다 따로 놓은 자리가 있을 때만: 그 줄들도 화자 위치로
    const own = (store.project.subtitles || []).filter(t => t.speaker === id && t.pos).length;
    const allBtn = own ? btn(`따로 놓은 ${own}줄도 맞추기`, {icon: 'crosshair', cls: 'sm block', onClick: () => {
      const ops = (store.project.subtitles || []).filter(t => t.speaker === id && t.pos).map(t => ({op: 'put', coll: 'subtitles', item: {id: t.id, pos: null}}));
      if (ops.length) store.edit(ops, '화자 위치 모두');
      allBtn.hidden = true;
      toast(ops.length ? `${ops.length}줄을 화자 위치로` : '모두 화자 위치예요', {icon: 'crosshair'});
    }}) : null;
    popover(anchor, h('div.pn-spkpop', {}, h('div.top', {}, name, del), sw, pad, allBtn),
      {align: 'start', onClose: saveName});
    if (fresh) name.select();
  }
  // 작은 화면판: 지금 장면 위에서 막대(자막 자리)를 끌어 화자의 기본 위치를 정해요 (x=50, y=88/50 에 붙어요 · Alt 는 안 붙음)
  // 다른 화자 자리는 흐린 점 · 좌표는 끄는 동안만 · ↺ 는 기본 위치가 아닐 때만
  function posPad(id, s) {
    const cv = store.project?.canvas || {w: 16, h: 9};
    const gx = h('i.gx'), gy = h('i.gy'), mark = h('i.mark'), xy = h('span.xy');
    const others = speakers().filter(o => o.id !== id).map(o => {
      const p = o.pos || DEFAULT_POS;
      return h('i.other', {style: {left: `${p.x}%`, top: `${p.y}%`, '--c': store.speakerColor(o.id)}});
    });
    const reset = iconBtn('rotate-ccw', '기본 위치로', () => {
      place(DEFAULT_POS);
      store.edit([{op: 'put', coll: 'speakers', item: {id, pos: {...DEFAULT_POS}}}], '화자 위치');
    }, {cls: 'ghost xs reset', size: 14});
    const pad = h('div.pn-pad', {class: cv.h > cv.w ? 'tall' : '', style: {'--c': s.color || SPEAKER_COLORS[0], aspectRatio: `${cv.w} / ${cv.h}`}, tip: '기본 위치'},
      stageFrame(), gx, gy, ...others, mark, xy, reset);
    let pos = {...(s.pos || DEFAULT_POS)};
    const place = p => {
      pos = p;
      mark.style.left = `${p.x}%`;
      mark.style.top = `${p.y}%`;
      xy.textContent = `${Math.round(p.x)}, ${Math.round(p.y)}`;
      reset.hidden = p.x === DEFAULT_POS.x && p.y === DEFAULT_POS.y;
    };
    place(pos);
    pad.addEventListener('pointerdown', e => {
      if (e.button !== 0 || e.target.closest('button')) return;
      e.preventDefault();
      const rc = pad.getBoundingClientRect();
      const onMark = e.target === mark;
      const off = onMark ? {x: e.clientX - (rc.left + rc.width * pos.x / 100), y: e.clientY - (rc.top + rc.height * pos.y / 100)} : {x: 0, y: 0};
      const g = store.gesture();
      pad.setPointerCapture(e.pointerId);
      mark.classList.add('drag');
      pad.classList.add('drag');
      const at = ev => {
        let x = clamp((ev.clientX - off.x - rc.left) / rc.width * 100, 5, 95), y = clamp((ev.clientY - off.y - rc.top) / rc.height * 100, 5, 95);
        let sx = false, sy = false;
        if (!ev.altKey) {
          if (Math.abs(x - 50) < 2.5) { x = 50; sx = true; }
          for (const t of [88, 50]) if (Math.abs(y - t) < 2.5) { y = t; sy = true; gy.style.top = `${t}%`; }
        }
        toggle(gx, 'on', sx); toggle(gy, 'on', sy);
        place({x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10});
        g.update([{op: 'put', coll: 'speakers', item: {id, pos: {...pos}}}]);
      };
      if (!onMark) at(e);
      const move = frameEvent(at);
      const up = () => {
        move.flush();
        pad.removeEventListener('pointermove', move);
        mark.classList.remove('drag');
        pad.classList.remove('drag');
        gx.classList.remove('on'); gy.classList.remove('on');
        g.commit('화자 위치');
      };
      pad.addEventListener('pointermove', move);
      pad.addEventListener('pointerup', up, {once: true});
      pad.addEventListener('pointercancel', up, {once: true});
    });
    return pad;
  }
  // 재생기에 보이는 지금 장면을 판 뒤에 깔아요 (영상이 아직 없으면 빈 판)
  function stageFrame() {
    const v = document.querySelector('video.pl-video.on');
    if (!v || v.readyState < 2 || !v.videoWidth) return null;
    const W = 320, H = Math.round(W * v.videoHeight / v.videoWidth), c = h('canvas.frame', {width: W, height: H});
    try { c.getContext('2d').drawImage(v, 0, 0, W, H); } catch { return null; }
    return c;
  }

  // ---------- 비었을 때: AI로 만들기 · 있을 때: 머리줄 아래 진행 막대 ----------
  const activeJob = (...kinds) => (store.jobs || []).find(j => kinds.includes(j.kind) && (j.state === 'running' || j.state === 'queued'));
  function renderGen() {
    const job = activeJob('transcribe');
    const on = items.length > 0 && (!!job || (genBusy && genP >= 0));
    genEl.hidden = !on;
    if (!on) return;
    const p = job ? +job.progress || 0 : genP;
    setText(genText, job?.state === 'queued' ? '대기' : `인식 중 ${Math.round(p * 100)}%`);
    genBar.set(p);
  }
  function renderAi() {
    const job = activeJob('transcribe', 'analyze');
    const busy = genBusy || !!job;
    const p = job ? +job.progress || 0 : genP;
    const key = `${busy}|${job?.kind}|${Math.round(p * 100)}`;
    if (aiCard._key === key) return;
    aiCard._key = key;
    aiCard.disabled = busy;
    toggle(aiCard, 'busy', busy);
    if (!busy) {
      aiCard.replaceChildren(h('span.ico', {}, icon('mic', 22)), h('span', {}, 'AI로 만들기'), h('small', {}, '음성 인식'));
      return;
    }
    const pr = p >= 0 ? p : -1;
    aiCard.replaceChildren(h('span.ico', {}, ring(pr < 0 ? -1 : Math.max(0.02, pr), 26)),
      h('span', {}, job?.kind === 'analyze' ? '분석 중' : '인식 중'),
      pr >= 0 ? bar(pr) : h('small', {}, '잠깐만요'));
  }
  async function generate(label) {
    if (genBusy) return;
    genBusy = true; genP = -1;
    renderAi();
    const before = (store.project?.subtitles || []).map(plain);
    try {
      const res = await store.action(label, () => api.generateSubs(store.name));
      let undoable = !!res?.changes?.length;
      if (res?.job) {
        genP = 0;
        renderGen();
        await store.waitJob(res.job, j => { genP = +j.progress || 0; renderAi(); renderGen(); });
        await store.reload();
        undoable = recordSwap(label, before);
      }
      const n = store.project?.subtitles?.length || 0;
      toast(n ? `자막 ${n}줄` : '말소리를 찾지 못했어요', {icon: 'captions',
        action: undoable ? {label: '되돌리기', onClick: () => store.undo()} : null});
    } catch (e) {
      toast(e.message, {kind: 'error'});
    } finally {
      genBusy = false; genP = -1;
      aiCard._key = '';
      renderAi();
      renderGen();
    }
  }
  // 인식 작업 끝에 서버가 자막을 통째로 바꿨어요: 전·후 자막을 맞바꾸는 되돌리기를 남겨요
  function recordSwap(label, before) {
    const after = (store.project?.subtitles || []).map(plain);
    if (JSON.stringify(before) === JSON.stringify(after)) return false;
    const swap = (from, to) => [...from.map(s => ({op: 'del', coll: 'subtitles', id: s.id})), ...to.map(item => ({op: 'put', coll: 'subtitles', item}))];
    store.pushUndo({label,
      undo: () => store.edit(swap(after, before), label, {undo: false}),
      redo: () => store.edit(swap(before, after), label, {undo: false})});
    return true;
  }
  // 'AI로 다시 만들기': 지금 줄을 모두 바꾸니 먼저 물어요
  function confirmRegen() {
    const subs = store.project?.subtitles || [];
    if (!subs.length) return generate('AI 자막');
    const fixed = subs.filter(s => s.locked || s.pos).length;
    popover(moreBtn, h('div.pn-confirm', {},
      h('div.pop-title', {}, icon('mic', 16), `자막 ${subs.length}줄을 새로 만들까요?`),
      fixed ? h('div.pn-confirm-note', {}, `고친 줄 ${fixed}개도 바뀌어요`) : null,
      h('div.pop-foot', {},
        btn('취소', {cls: 'ghost sm', onClick: () => closePopover()}),
        btn('새로 만들기', {cls: 'primary sm', onClick: () => { closePopover(); generate('AI로 다시 만들기'); }}))), {align: 'end'});
  }

  // ---------- 대본 불러오기 ----------
  // 파일은 맥 고르기 창 → 경로를 서버가 읽어요 (CP949 대본도 읽혀요). 고르기 창을 못 열면 브라우저 창으로 받아요.
  async function importFile() {
    let paths;
    try { paths = await api.pick('script'); }
    catch {
      const f = await pickFile(SCRIPT_ACCEPT);
      return f ? importText(await f.text(), f.name) : false;
    }
    if (!paths?.length) return false;
    const path = paths[0];
    return importText(null, path.split('/').pop(), path);
  }
  async function importText(text, filename, path) {
    if (!path && !text?.trim()) { toast('대본이 비어 있어요', {kind: 'error'}); return false; }
    try {
      const res = await store.action('대본 불러오기', () => api.importSubs(store.name, path ? undefined : text, {...(filename ? {filename} : {}), ...(path ? {path} : {})}));
      const info = res?.info || {};
      const parts = [`자막 ${info.count ?? store.project?.subtitles?.length ?? 0}줄`];
      if (info.base === 'edit') parts.push('편집본 시간');
      else if (info.base === 'orig') parts.push('원본 시간');
      if (info.unmatched) parts.push(`확인 ${info.unmatched}`);
      toast(parts.join(' · '), {icon: 'captions', timeout: 6000, action: {label: '되돌리기', onClick: () => store.undo()}});
      return true;
    } catch (e) {
      toast(e.message, {kind: 'error'});
      return false;
    }
  }
  function openPaste() {
    const ta = h('textarea.input.pn-paste-area', {'aria-label': '대본', placeholder: '00:12 안녕하세요\n00:15 오늘은 성수동에 왔어요'});
    ta.spellcheck = false;
    const go = btn('불러오기', {cls: 'primary', onClick: submit});
    const file = btn('파일', {icon: 'file-text', cls: 'ghost', onClick: async () => { if (await importFile()) s.close(); }});
    const s = sheet({title: '대본 불러오기', icon: 'file-text', width: 520, body: ta,
      foot: [file, h('span.sp'), btn('취소', {cls: 'ghost', onClick: () => s.close()}), go]});
    const upd = () => { go.disabled = !ta.value.trim(); };
    upd();
    ta.addEventListener('input', upd);
    ta.addEventListener('keydown', e => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); submit(); } });
    ta.focus();
    async function submit() {
      if (!ta.value.trim() || go.disabled) return;
      go.disabled = true;
      if (await importText(ta.value)) s.close(); else go.disabled = false;
    }
  }

  // ---------- 타이밍 (바로 적용 · 한 번에 되돌리기) ----------
  function openTiming() {
    const st = {...SUBS_DEFAULTS, ...(store.project?.settings?.subs || {})}, pending = {};
    let t = 0;
    const flush = () => {
      clearTimeout(t); t = 0;
      if (!Object.keys(pending).length) return;
      const value = {...pending};
      for (const k of Object.keys(pending)) delete pending[k];
      store.edit([{op: 'set', key: 'settings.subs', value}], '자막 타이밍', {merge: 'subs-timing', throttle: true});
    };
    const set = (k, v) => { pending[k] = v; clearTimeout(t); t = setTimeout(flush, 120); };
    const sec = x => `${(+x).toFixed(2)}s`;
    const row = (label, k, min, max, step, format = sec) => sliderRow({label, min, max, step, value: st[k], format, onInput: v => set(k, v)});
    const resplit = btn('다시 나누기', {icon: 'split', cls: 'sm block', onClick: async () => {
      flush();
      closePopover();
      await generate('다시 나누기');
    }});
    popover(timeBtn, h('div', {style: {width: '264px'}},
      h('div.pop-title', {}, '자막 타이밍'),
      h('div.sliders', {},
        row('먼저 띄우기', 'lead', 0, 0.5, 0.05),
        row('늦게 끄기', 'hold', 0, 1.5, 0.05),
        row('최소 길이', 'min', 0.3, 2, 0.1),
        row('틈 메우기', 'bridge', 0, 1, 0.05),
        h('div.hsep', {style: {margin: '2px 0'}}),
        row('한 줄 글자 수', 'max_chars', 8, 30, 1, v => `${v}자`),
        resplit)), {align: 'end', onClose: flush});
  }
  function openMore() {
    const locked = (store.project?.subtitles || []).filter(s => s.locked);
    menu(moreBtn, [
      {label: '대본 다시 불러오기', icon: 'file-text', onClick: () => importFile()},
      {label: '대본 붙여넣기', icon: 'clipboard', onClick: () => openPaste()},
      {label: 'AI로 다시 만들기', icon: 'mic', disabled: genBusy || !!activeJob('transcribe'), onClick: () => confirmRegen()},
      '-',
      {label: '모두 자동 타이밍', icon: 'clock', disabled: !locked.length, onClick: () => {
        store.edit(locked.map(s => ({op: 'put', coll: 'subtitles', item: {id: s.id, locked: false}})), '모두 자동 타이밍');
        toast(`${locked.length}줄 자동 타이밍`, {icon: 'clock', action: {label: '되돌리기', onClick: () => store.undo()}});
      }},
    ], {align: 'end'});
  }

  // 자막 추가: 타임라인이 맡고(T), 아무도 안 받으면 재생 위치에 2초짜리 줄을 직접 넣어요
  function addLine() {
    if (store.command('addSub')) return;
    const t = store.time, end = Math.min(store.total, t + 2);
    if (end - t < 0.2) return;
    const [op] = store.edit([{op: 'put', coll: 'subtitles', item: {start: t, end, text: '', speech: [t, end], locked: true}}], '자막 추가');
    store.select('sub', op.item.id);
  }

  // ---------- 상태 → 화면 ----------
  store.on('project', e => {
    if (e.colls && !['subtitles', 'speakers', 'cuts', 'sources'].some(c => e.colls.has(c))) return;
    sync();
  });
  store.on('sel', (s, prev) => {
    for (const x of [prev, s]) if (x?.kind === 'sub') { const r = vl.el(x.id), it = store.item('sub', x.id); if (r && it) updateRow(r, it); }
    if (s?.kind !== 'sub') return;
    if (!vl.index.has(s.id)) sync.now();
    vl.reveal(s.id);
    const it = store.item('sub', s.id);   // 새 빈 줄이면 바로 쓰게 해요
    if (it && !it.text && store.tab === 'subs' && !isEditable(document.activeElement)) {
      requestAnimationFrame(() => vl.el(s.id)?._ta.focus({preventScroll: true}));
    }
  });
  store.on('time', t => { if (items.length) updateNow(t); });
  store.on('play', v => { if (v) follow(); });
  store.on('jobs', () => { renderGen(); if (!items.length) renderAi(); });
  store.on('external', () => vl.refresh());

  // ---------- 명령 ----------
  store.on('cmd:nav', ctx => {
    if (store.tab !== 'subs' || ctx.handled || !items.length) return;
    ctx.handled = true;
    const cur = store.sel?.kind === 'sub' ? vl.index.get(store.sel.id) : null;
    let j;
    if (cur == null) {   // 고른 줄이 없으면 재생 위치 근처에서 시작해요
      const t = store.time;
      j = ctx.arg > 0 ? items.findIndex(s => s.start >= t - 1e-3) : items.findLastIndex(s => s.start < t - 1e-3);
      if (j < 0) j = ctx.arg > 0 ? items.length - 1 : 0;
    } else j = clamp(cur + (ctx.arg > 0 ? 1 : -1), 0, items.length - 1);
    if (j === cur) return;
    store.select('sub', items[j].id, {seek: true});
  });
  store.on('cmd:speaker', ctx => {
    const s = store.sel?.kind === 'sub' ? store.item('sub', store.sel.id) : null;
    if (!s || ctx.handled) return;
    ctx.handled = true;
    const sp = speakers(), n = ctx.arg;
    let id = sp[n - 1]?.id;
    if (!id) {
      if (n !== sp.length + 1) return toast(`화자 ${sp.length + 1}까지 쓸 수 있어요`, {timeout: 1600});
      id = freeLetter();
      if (!id) return;
    }
    assign(s, s.speaker === id ? null : id);
  });
  store.on('cmd:listen', ctx => {   // Enter: 고른 줄 글 고치기
    if (ctx.handled || store.sel?.kind !== 'sub' || store.tab !== 'subs') return;
    const r = vl.el(store.sel.id);
    if (!r) return;
    ctx.handled = true;
    r._ta.focus({preventScroll: true});
    const n = r._ta.value.length;
    r._ta.setSelectionRange(n, n);
  });

  sync.now();
  if (store.sel?.kind === 'sub') vl.reveal(store.sel.id, 'center');
}

// 포인터 이동을 프레임마다 한 번으로 줄여요 (마지막 이벤트로)
function frameEvent(fn) {
  let last = null, id = 0;
  const f = e => {
    last = e;
    if (!id) id = requestAnimationFrame(() => { id = 0; fn(last); });
  };
  f.flush = () => { if (id) { cancelAnimationFrame(id); id = 0; fn(last); } };
  return f;
}

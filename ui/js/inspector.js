// 속성 (spec 11.4): 선택한 것의 속성을 보여주고 고쳐요 — 컷 · 자막 · 클립 조각(영상) · 메모 · 화자 · 없으면 프로젝트 정보.
// 고치기는 모두 store.edit (같은 칸을 이어서 바꾸면 되돌리기 한 번으로 묶여요). 시간 칸: ←/→(↑/↓) 0.05초, ⇧ 0.5초.
// 자막 시간은 타임라인 축(store.axis)을 따라요: 편집본이면 편집본 시간으로 보이고 입력해요. 컷 시간은 항상 원본이에요.
import { h, btn, tip, toast, clamp, segmented, switchEl, switchRow } from './ui.js';
import { icon } from './icons.js';
import { api } from './api.js';
import { fmt, fmtDur, parseTime } from './time.js';
import { SPEAKER_COLORS, DEFAULT_POS } from './store.js';
import { noFocus } from './panels/common.js';

const TYPE_LABEL = {silence: '무음', long: '긴 무음', filler: '필러', gap: '현장음', ng: 'NG', retake: '재촬영', manual: '요청', user: '직접', clip: '클립'};
const CLAUDE_TYPES = new Set(['ng', 'retake', 'manual']);   // 컷 이유는 Claude 판단만 보여요 (무음·필러의 자동 글은 숨겨요)
const STEP = 0.05, MIN_CUT = 0.05, MIN_SUB = 0.1;
const r3 = x => Math.round(x * 1000) / 1000;
// 시간 표기: 00:12.40 · 1:02:05.40
const tc = t => {
  t = Math.max(0, +t || 0);
  if (t < 3600 - 0.005) return fmt(t, 2);
  const w = Math.floor(t), cs = Math.min(99, Math.round((t - w) * 100));
  return `${fmt(w, 0)}.${String(cs).padStart(2, '0')}`;
};
const num1 = v => String(Math.round(v * 10) / 10);

function loadCss(name) {
  const id = `css-${name}`;
  let link = document.getElementById(id);
  if (!link) { link = h('link', {rel: 'stylesheet', id, href: `/ui/css/${name}.css`}); document.head.append(link); }
  if (link.sheet) return Promise.resolve();
  return new Promise(r => { link.addEventListener('load', r, {once: true}); link.addEventListener('error', r, {once: true}); setTimeout(r, 1500); });
}

export async function mount(el, store) {
  await loadCss('inspector');
  el.classList.add('insp');
  const headIco = h('span.insp-ico'), headTitle = h('span'), headExtra = h('div.insp-extra');
  const head = h('div.phead.insp-head', {}, h('div.title', {}, headIco, headTitle), h('div.sp'), headExtra);
  const body = h('div.pbody.insp-body');
  el.append(head, body);
  // 마우스로 누른 버튼은 포커스를 안 가져가요 (X·V 를 눌러도 테두리가 안 남게). 쓰던 칸은 그 다음에 빠져나와 반영해요
  // (반영하며 다시 그리기 전에 noFocus 가 버튼을 찾도록 순서를 지켜요).
  noFocus(el);
  el.addEventListener('mousedown', e => {
    const a = document.activeElement;
    if (e.defaultPrevented && a && el.contains(a) && a.matches('input,textarea')) a.blur();
  });

  let cur = null;   // {key, refresh(), onPreview?()}
  const VIEWS = {cut: cutView, sub: subView, piece: clipView, source: clipView, comment: commentView, speaker: speakerView};
  const KIND_HEAD = {cut: ['scissors', '컷'], sub: ['captions', '자막'], piece: ['film', '클립'], source: ['film', '클립'],
    comment: ['message-text', '메모'], speaker: ['user', '화자'], none: ['sliders', '프로젝트']};

  function build(force) {
    const sel = store.sel, it = sel ? store.item(sel.kind, sel.id) : null;
    const kind = it && VIEWS[sel.kind] ? sel.kind : 'none';
    const key = kind === 'none' ? 'none' : `${kind}:${sel.id}`;
    if (!force && cur?.key === key) { cur.refresh(); return; }
    const [ic, word] = KIND_HEAD[kind];
    headIco.replaceChildren(icon(ic, 16));
    headTitle.textContent = word;
    headExtra.replaceChildren();
    body.scrollTop = 0;
    const v = kind === 'none' ? factsView() : VIEWS[kind](sel.id);
    body.replaceChildren(v.el);
    cur = {key, ...v};
    cur.refresh();
  }

  // ---------- 공용 칸 ----------
  const sec = (label, ...kids) => h('section.insp-sec', {}, label ? h('div.insp-label', {}, label) : null, kids);
  const kv = (label, value, cls = '') => h('div.insp-kv', {class: cls}, h('span.k', {}, label), h('span.v', {}, value));
  const flash = el => { el.classList.remove('bad'); void el.offsetWidth; el.classList.add('bad'); setTimeout(() => el.classList.remove('bad'), 450); };

  // − 값 + : ←/→ 또는 ↑/↓ 로 한 칸, ⇧ 는 big 배. 버튼을 누르고 있으면 반복해요. set 이 false 를 주면 범위 밖이라 맞춘 거예요 (빨갛게 깜빡).
  function stepper({label, get, set, step = STEP, big = 10, format = tc, parse = parseTime, cls = ''}) {
    const inp = h('input', {type: 'text', spellcheck: false, autocomplete: 'off', role: 'spinbutton', 'aria-label': label});
    const box = h('div.stepper', {class: cls});
    const paint = force => {
      if (!force && document.activeElement === inp) return;
      const v = get();
      const s = v == null || !Number.isFinite(v) ? '' : format(v);
      if (inp.value !== s) inp.value = s;
    };
    const apply = v => { if (set(v) === false) flash(box); paint(true); };
    const nudge = (d, shift) => { const v = get(); if (v != null) apply(v + d * step * (shift ? big : 1)); };
    const commit = () => {
      const v = parse(inp.value);
      if (!Number.isFinite(v)) { flash(box); paint(true); return; }
      const g = get();
      if (g == null || Math.abs(v - g) > 1e-4) apply(v); else paint(true);
    };
    const hold = d => {
      const b = h('button', {type: 'button', tabindex: -1, 'aria-label': `${label} ${d < 0 ? '−' : '+'}`}, icon(d < 0 ? 'minus' : 'plus', 14));
      b.addEventListener('pointerdown', e => {
        if (e.button !== 0) return;
        e.preventDefault();
        const sh = e.shiftKey;
        nudge(d, sh);
        let rep = 0;
        const t1 = setTimeout(() => { rep = setInterval(() => nudge(d, sh), 70); }, 380);
        const stop = () => { clearTimeout(t1); clearInterval(rep); removeEventListener('pointerup', stop); removeEventListener('pointercancel', stop); };
        addEventListener('pointerup', stop);
        addEventListener('pointercancel', stop);
      });
      return b;
    };
    inp.addEventListener('keydown', e => {
      if (e.isComposing || e.keyCode === 229) return;
      const k = e.key;
      if (k === 'ArrowLeft' || k === 'ArrowDown') { e.preventDefault(); nudge(-1, e.shiftKey); }
      else if (k === 'ArrowRight' || k === 'ArrowUp') { e.preventDefault(); nudge(1, e.shiftKey); }
      else if (k === 'Enter') { e.preventDefault(); commit(); inp.select(); }
      else if (k === 'Escape') { e.preventDefault(); e.stopPropagation(); paint(true); inp.blur(); }
    });
    inp.addEventListener('change', commit);
    inp.addEventListener('focus', () => inp.select());
    inp.addEventListener('blur', () => paint(true));
    box.append(hold(-1), inp, hold(1));
    box.refresh = paint;
    box.input = inp;
    return box;
  }
  const field = (label, ctl) => h('div.field', {}, h('label', {}, label), ctl);

  // 글 상자 (입력하는 대로 바로 반영, Enter 는 빠져나오기 · ⇧Enter 줄바꿈 · Esc 처음 글로)
  function textBox({get, set, placeholder = '', rows = 2}) {
    const ta = h('textarea.input.insp-text', {rows, spellcheck: false, placeholder});
    let orig = '';
    const grow = () => { ta.style.height = 'auto'; ta.style.height = `${Math.min(220, ta.scrollHeight + 2)}px`; };
    ta.addEventListener('focus', () => { orig = ta.value; });
    ta.addEventListener('input', () => { grow(); set(ta.value); });
    ta.addEventListener('keydown', e => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ta.blur(); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); if (ta.value !== orig) { ta.value = orig; set(orig); } ta.blur(); }
    });
    ta.refresh = () => {
      if (document.activeElement === ta) return;
      const v = get() ?? '';
      if (ta.value !== v) { ta.value = v; grow(); }
    };
    requestAnimationFrame(grow);
    return ta;
  }

  // ---------- 컷 ----------
  function cutView(id) {
    const item = () => store.item('cut', id);
    const seg = segmented([{value: true, label: '자름', cls: 'cut', tip: '자르기', key: 'x'}, {value: false, label: '살림', cls: 'keep', tip: '살리기', key: 'v'}],
      !!item()?.enabled, v => {
        const c = item();
        if (c) store.edit([{op: 'put', coll: 'cuts', item: {id, enabled: v, reviewed: true}}], v ? '자르기' : '살리기');
      }, {cls: 'insp-seg', label: '자를지'});
    const setT = which => v => {
      const c = item();
      if (!c) return false;
      const lo = which === 'start' ? 0 : c.start + MIN_CUT, hi = which === 'start' ? c.end - MIN_CUT : store.total;
      const x = r3(clamp(v, lo, Math.max(lo, hi)));
      if (Math.abs(x - c[which]) > 1e-4) {
        store.edit([{op: 'put', coll: 'cuts', item: {id, [which]: x}}], which === 'start' ? '컷 시작' : '컷 끝', {merge: `insp:${id}:${which}`, throttle: true});
      }
      return Math.abs(x - r3(v)) < 1e-3;
    };
    const st = stepper({label: '시작', get: () => item()?.start, set: setT('start')});
    const en = stepper({label: '끝', get: () => item()?.end, set: setT('end')});
    const len = h('span.v.tc'), typeEl = h('span.insp-type'), unrev = h('span.insp-unrev', {}, h('i.dot'), '미확인');
    const listen = btn('듣기', {icon: 'headphones', cls: 'block insp-listen', tip: '앞뒤 1.5초 듣기', key: 'enter',
      onClick: () => store.command('listen', id)});
    const reasonTx = h('span');
    const reason = h('div.insp-note', {hidden: true}, icon('pencil', 14), h('span', {}, h('b', {}, 'Claude'), ' · ', reasonTx));
    const words = h('div.insp-words', {hidden: true});
    const el = h('div.insp-stack', {},
      seg,
      sec(h('span', {}, '시간', h('em', {}, '원본')), field('시작', st), field('끝', en),
        h('div.insp-kv', {}, h('span.k', {}, '길이'), len, h('span.sp'), typeEl)),
      listen, reason, words);
    let wkey = '';
    const loadWords = c => {
      const k = `${c.start}:${c.end}:${store.wordsCount}`;
      if (k === wkey) return;
      wkey = k;
      store.words().then(ws => {
        if (wkey !== k) return;
        const inside = ws.filter(w => w.end > c.start + 0.01 && w.start < c.end - 0.01).map(w => w.word);
        words.hidden = !inside.length;
        words.textContent = inside.length > 40 ? `${inside.slice(0, 40).join(' ')} …` : inside.join(' ');
      }).catch(() => { words.hidden = true; });
    };
    const onPreview = () => {
      const on = store.playing && (store.previewing?.id === id);
      if (listen._on === on) return;   // 누르는 중에 안을 바꾸면 click 이 사라질 수 있어요
      listen._on = on;
      listen.replaceChildren(icon(on ? 'pause' : 'headphones', 16), h('span', {}, on ? '멈춤' : '듣기'));
      listen.classList.toggle('on', on);
    };
    return {
      el, onPreview,
      refresh() {
        const c = item();
        if (!c) return;
        seg.set(!!c.enabled);
        st.refresh(); en.refresh();
        len.textContent = fmtDur(c.end - c.start);
        typeEl.dataset.type = c.type;
        typeEl.replaceChildren(h('i'), TYPE_LABEL[c.type] || c.type);
        unrev.hidden = !!c.reviewed;
        if (!headExtra.contains(unrev)) headExtra.replaceChildren(unrev);
        const why = c.reason && (CLAUDE_TYPES.has(c.type) || c.by === 'cli') ? c.reason : '';
        reason.hidden = !why;
        reasonTx.textContent = why;
        loadWords(c);
        onPreview();
      },
    };
  }

  // ---------- 자막 ----------
  function subView(id) {
    const item = () => store.item('sub', id);
    const ax = () => store.axis === 'edit';
    const toAx = t => ax() ? store.toEdited(t) : t;
    const fromAx = x => ax() ? store.toOriginal(x) : x;
    const text = textBox({get: () => item()?.text, placeholder: '자막', set: v => {
      if (item()) store.edit([{op: 'put', coll: 'subtitles', item: {id, text: v}}], '자막 고치기', {merge: `insp:${id}:text`, throttle: true});
    }});
    const setT = which => v => {
      const s = item();
      if (!s) return false;
      let o = fromAx(v);
      const lo = which === 'start' ? 0 : s.start + MIN_SUB, hi = which === 'start' ? s.end - MIN_SUB : store.total;
      const x = r3(clamp(o, lo, Math.max(lo, hi)));
      if (Math.abs(x - s[which]) > 1e-4) {
        store.edit([{op: 'put', coll: 'subtitles', item: {id, [which]: x}}], which === 'start' ? '자막 시작' : '자막 끝', {merge: `insp:${id}:${which}`, throttle: true});
      }
      return Math.abs(x - r3(o)) < 1e-3;
    };
    const st = stepper({label: '시작', get: () => { const s = item(); return s ? toAx(s.start) : null; }, set: setT('start')});
    const en = stepper({label: '끝', get: () => { const s = item(); return s ? toAx(s.end) : null; }, set: setT('end')});
    const axTag = h('em'), len = h('span.v.tc');
    const auto = switchEl({label: '자동 타이밍', onChange: v => { if (item()) store.edit([{op: 'put', coll: 'subtitles', item: {id, locked: !v}}], v ? '자동 타이밍' : '타이밍 고정'); }});
    const autoRow = h('label.switch-row.insp-auto', {}, h('span', {}, icon('clock', 14), '자동 타이밍'), auto);
    const spWrap = h('div.chips.insp-speakers', {role: 'group', 'aria-label': '화자'});
    const px = stepper({label: '가로', step: 1, big: 5, format: num1, parse: parseFloat, get: () => { const s = item(); return s ? store.posOf(s).x : null; }, set: setPos('x'), cls: 'sm'});
    const py = stepper({label: '세로', step: 1, big: 5, format: num1, parse: parseFloat, get: () => { const s = item(); return s ? store.posOf(s).y : null; }, set: setPos('y'), cls: 'sm'});
    const posFrom = h('em');
    const resetBtn = btn('기본 위치', {icon: 'rotate-ccw', cls: 'sm ghost', tip: '화자·기본 위치 따르기', onClick: () => {
      if (item()?.pos) store.edit([{op: 'put', coll: 'subtitles', item: {id, pos: null}}], '기본 위치');
    }});
    const spPosBtn = btn('화자 위치', {icon: 'users', cls: 'sm ghost', tip: '이 위치를 화자의 모든 줄에', onClick: () => {
      const s = item();
      if (!s?.pos || !s.speaker) return;
      store.edit([{op: 'put', coll: 'speakers', item: {id: s.speaker, pos: {...s.pos}}}, {op: 'put', coll: 'subtitles', item: {id, pos: null}}], '화자 위치');
      toast(`${store.speaker(s.speaker)?.name || '화자'} 위치 바꿈`, {icon: 'users', timeout: 1600});
    }});
    const checkBtn = h('button.pill.warn.insp-check', {type: 'button', tip: '확인했어요', onclick: () => {
      if (item()) store.edit([{op: 'put', coll: 'subtitles', item: {id, check: false}}], '확인');
    }}, h('i.check-dot'), '확인 필요');
    const lockIco = h('span.insp-lock', {hidden: true}, icon('lock', 14));
    tip(lockIco, '시간 고정');
    function setPos(k) {
      return v => {
        const s = item();
        if (!s) return false;
        const p = {...store.posOf(s)}, x = Math.round(clamp(v, 0, 100) * 10) / 10;
        if (p[k] === x && s.pos) return x === v;
        p[k] = x;
        store.edit([{op: 'put', coll: 'subtitles', item: {id, pos: p}}], '자막 위치', {merge: `insp:${id}:pos`, throttle: true});
        return Math.abs(x - v) < 0.05;
      };
    }
    let spKey = '';
    const paintSpeakers = s => {
      const list = store.project?.speakers || [];
      const k = list.map(x => `${x.id}:${x.name}:${x.color}`).join('|') + `#${s.speaker}`;
      if (k === spKey) return;
      spKey = k;
      spWrap.replaceChildren(...list.map((sp, i) => {
        const b = h('button.chip', {type: 'button', style: {'--dot': store.speakerColor(sp.id)}, 'aria-pressed': String(s.speaker === sp.id), onclick: () => {
          const now = item();
          if (now) store.edit([{op: 'put', coll: 'subtitles', item: {id, speaker: now.speaker === sp.id ? null : sp.id}}], '화자');
        }}, h('span.dot'), h('span', {}, sp.name || sp.id));
        if (i < 9) tip(b, sp.name || sp.id, String(i + 1));
        return b;
      }), h('button.chip.add', {type: 'button', tip: '화자 추가', onclick: addSpeaker}, icon('plus', 14)));
    };
    function addSpeaker() {
      const list = store.project?.speakers || [], used = new Set(list.map(x => x.id));
      const sid = [...'ABCDEFGHIJKLMNOPQRSTUVWXYZ'].find(c => !used.has(c));
      if (!sid) return;
      const i = sid.charCodeAt(0) - 65;
      store.edit([{op: 'put', coll: 'speakers', item: {id: sid, name: `화자 ${i + 1}`, color: SPEAKER_COLORS[i % 6], pos: {...DEFAULT_POS}}},
        {op: 'put', coll: 'subtitles', item: {id, speaker: sid}}], '화자 추가');
    }
    const el = h('div.insp-stack', {},
      text,
      sec(h('span', {}, '시간', axTag), field('시작', st), field('끝', en), h('div.insp-kv', {}, h('span.k', {}, '길이'), len), autoRow),
      sec('화자', spWrap),
      sec(h('span', {}, '위치', posFrom), h('div.insp-pos', {}, field('X', px), field('Y', py)), h('div.insp-row', {}, resetBtn, spPosBtn)));
    return {
      el,
      refresh() {
        const s = item();
        if (!s) return;
        text.refresh();
        st.refresh(); en.refresh();
        axTag.textContent = ax() ? '편집본' : '원본';
        len.textContent = fmtDur(Math.max(0, toAx(s.end) - toAx(s.start)));
        auto.set(!s.locked);
        lockIco.hidden = !s.locked;
        checkBtn.hidden = !s.check;
        if (!headExtra.contains(checkBtn)) headExtra.replaceChildren(checkBtn, lockIco);
        paintSpeakers(s);
        px.refresh(); py.refresh();
        posFrom.textContent = s.pos ? '직접' : s.speaker && store.speaker(s.speaker)?.pos ? '화자' : '기본';
        resetBtn.disabled = !s.pos;
        spPosBtn.disabled = !s.pos || !s.speaker;
        el.classList.toggle('hidden-sub', store.isHidden(s));
      },
    };
  }

  // ---------- 클립 조각 · 영상 ----------
  function clipView(id) {
    const kind = store.sel.kind;
    const piece = () => kind === 'piece' ? store.item('piece', id) : null;
    const src = () => kind === 'source' ? store.item('source', id) : store.item('source', piece()?.sid);
    const cv = h('canvas.insp-thumb', {width: 272 * 2, height: 153 * 2});
    const name = h('div.insp-name.ellipsis');
    const facts = h('div.insp-kvs');
    const reveal = btn('Finder에서 보기', {icon: 'folder-open', cls: 'block', onClick: () => {
      const s = src();
      if (s?.path) api.reveal(s.path).catch(e => toast(e.message, {kind: 'error'}));
    }});
    const el = h('div.insp-stack', {}, h('div.insp-thumb-box', {}, cv), name, facts, reveal);
    let tkey = '';
    return {
      el,
      refresh() {
        const s = src(), pc = piece();
        if (!s) return;
        name.textContent = s.name || s.id;
        tip(name, s.path || s.name || '');
        const span = store.derived.spans.find(x => x.id === s.id);
        const rows = [kv('해상도', `${s.w || '?'}×${s.h || '?'}`), kv('프레임', s.fps ? `${Math.round(s.fps * 100) / 100}fps` : '—'), kv('파일 길이', tc(s.duration), 'tcv')];
        if (pc) rows.push(kv('구간', `${tc(pc.start)} – ${tc(pc.end)}`, 'tcv'), kv('길이', fmtDur(pc.end - pc.start), 'tcv'));
        if (s.status && s.status !== 'ready') rows.push(kv('상태', s.status === 'error' ? '분석 실패' : s.status === 'queued' ? '대기' : `분석 중 ${Math.round((+s.progress || 0) * 100)}%`));
        facts.replaceChildren(...rows);
        const local = pc && span ? pc.start - span.start : 0, k = `${s.id}:${Math.round(local * 10)}`;
        if (k !== tkey) { tkey = k; drawThumb(cv, s.id, local); }
      },
    };
  }
  const imgs = new Map();
  function drawThumb(cv, sid, local) {
    const a = store.assets?.[sid], ctx = cv.getContext('2d');
    ctx.fillStyle = '#0A0A0C';
    ctx.fillRect(0, 0, cv.width, cv.height);
    if (!a?.thumbs) return;
    let img = imgs.get(a.thumbs);
    if (!img) { img = new Image(); img.src = a.thumbs; imgs.set(a.thumbs, img); }
    const draw = () => {
      const m = a.thumbs_meta || {}, cols = m.cols || 10, count = m.count || cols, tw = m.w || img.naturalWidth / cols, th = m.h || tw * 9 / 16;
      const k = clamp(Math.floor(local / (m.interval || 1)), 0, count - 1), sx = (k % cols) * tw, sy = Math.floor(k / cols) * th;
      const s = Math.min(cv.width / tw, cv.height / th), w = tw * s, hh = th * s;
      ctx.fillRect(0, 0, cv.width, cv.height);
      ctx.drawImage(img, sx, sy, tw, th, (cv.width - w) / 2, (cv.height - hh) / 2, w, hh);
    };
    if (img.complete && img.naturalWidth) draw();
    else img.decode().then(draw, () => {});
  }

  // ---------- 메모 ----------
  function commentView(id) {
    const item = () => store.item('comment', id);
    const at = h('button.insp-at.tc', {type: 'button', onclick: () => { const c = item(); if (c) store.setTime(c.t, 'inspector'); }});
    tip(at, '그 시간으로');
    const text = textBox({get: () => item()?.text, placeholder: '메모', rows: 3, set: v => {
      if (item()) store.edit([{op: 'put', coll: 'comments', item: {id, text: v}}], '메모 고치기', {merge: `insp:${id}:text`, throttle: true});
    }});
    const reply = h('div.insp-note.reply', {hidden: true}, icon('corner-down-left', 14), h('span'));
    const done = switchRow('완료', {onChange: v => { if (item()) store.edit([{op: 'put', coll: 'comments', item: {id, done: v}}], v ? '메모 완료' : '메모 다시'); }});
    const el = h('div.insp-stack', {}, at, text, reply, done);
    return {
      el,
      refresh() {
        const c = item();
        if (!c) return;
        at.replaceChildren(icon('map-pin', 14), h('span', {}, tc(store.toEdited(c.t))), h('em', {}, `원본 ${tc(c.t)}`));   // 재생기·메모 목록처럼 편집본 시간 먼저
        text.refresh();
        reply.hidden = !c.reply;
        reply.lastChild.textContent = c.reply || '';
        done.switch.set(!!c.done);
      },
    };
  }

  // ---------- 화자 ----------
  function speakerView(id) {
    const item = () => store.item('speaker', id);
    const name = h('input.input', {spellcheck: false, 'aria-label': '이름'});
    const commitName = () => {
      const s = item(), v = name.value.trim();
      if (s && v && v !== s.name) store.edit([{op: 'put', coll: 'speakers', item: {id, name: v}}], '화자 이름');
      else if (s) name.value = s.name || '';
    };
    name.addEventListener('change', commitName);
    name.addEventListener('keydown', e => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter') { e.preventDefault(); name.blur(); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); name.value = item()?.name || ''; name.blur(); }
    });
    const sw = h('div.insp-swatches', {role: 'group', 'aria-label': '색'}, SPEAKER_COLORS.map(c => {
      const b = h('button.insp-swatch', {type: 'button', style: {'--c': c}, 'aria-label': c, onclick: () => {
        if (item()) store.edit([{op: 'put', coll: 'speakers', item: {id, color: c}}], '화자 색');
      }});
      return b;
    }));
    const setPos = k => v => {
      const s = item();
      if (!s) return false;
      const p = {...(s.pos || DEFAULT_POS)}, x = Math.round(clamp(v, 0, 100) * 10) / 10;
      p[k] = x;
      store.edit([{op: 'put', coll: 'speakers', item: {id, pos: p}}], '화자 위치', {merge: `insp:${id}:spos`, throttle: true});
      return Math.abs(x - v) < 0.05;
    };
    const px = stepper({label: '가로', step: 1, big: 5, format: num1, parse: parseFloat, get: () => (item()?.pos || DEFAULT_POS).x, set: setPos('x'), cls: 'sm'});
    const py = stepper({label: '세로', step: 1, big: 5, format: num1, parse: parseFloat, get: () => (item()?.pos || DEFAULT_POS).y, set: setPos('y'), cls: 'sm'});
    const lines = h('span.v.tc');
    const el = h('div.insp-stack', {}, field('이름', name), field('색', sw),
      sec('위치', h('div.insp-pos', {}, field('X', px), field('Y', py))), h('div.insp-kv', {}, h('span.k', {}, '자막'), lines));
    return {
      el,
      refresh() {
        const s = item();
        if (!s) return;
        if (document.activeElement !== name) name.value = s.name || '';
        for (const b of sw.children) b.setAttribute('aria-pressed', String(b.style.getPropertyValue('--c').toUpperCase() === String(s.color || store.speakerColor(id)).toUpperCase()));
        px.refresh(); py.refresh();
        lines.textContent = `${(store.project?.subtitles || []).filter(x => x.speaker === id).length}줄`;
      },
    };
  }

  // ---------- 아무것도 선택 안 했을 때: 프로젝트 정보 ----------
  function factsView() {
    const box = h('div.insp-kvs.facts');
    const el = h('div.insp-stack', {}, box);
    let key = '';
    return {
      el,
      refresh() {
        const p = store.project;
        if (!p) return;
        // 컷 수는 컷 패널과 같은 기준 (클립 다듬기 제외). 'n / m' 은 컷 패널의 확인 진행에만 써요.
        const cuts = (p.cuts || []).filter(x => x.type !== 'clip'), unrev = store.derived.unreviewed, on = cuts.filter(x => x.enabled).length;
        const c = p.canvas || {};
        const rows = [
          ['원본', tc(store.total), 'tcv'], ['편집본', tc(store.editedDuration), 'tcv accent'],
          ['컷', `${cuts.length}개 · 자름 ${on}`], ...(unrev ? [['미확인', `${unrev}개`]] : []), ['자막', `${(p.subtitles || []).length}줄`],
          ['클립', `${(p.sources || []).length}개`], ['캔버스', c.w ? `${c.w}×${c.h} · ${c.fps}fps` : '—'],
        ];
        const k = JSON.stringify(rows);
        if (k === key) return;
        key = k;
        box.replaceChildren(...rows.map(([a, b, cls]) => kv(a, b, cls || '')));
      },
    };
  }

  // ---------- 상태 → 화면 ----------
  store.on('sel', () => build());
  store.on('project', ({colls}) => {
    if (!cur) return build();
    const sel = store.sel;
    if (cur.key !== 'none' && !(sel && store.item(sel.kind, sel.id))) return build();
    if (cur.key === 'none' && sel && store.item(sel.kind, sel.id)) return build();
    if (cur.key.startsWith('piece:') && colls && !colls.has('cuts') && !colls.has('sources') && !colls.has('splits')) return;
    cur.refresh();
  });
  store.on('view', keys => { if (keys.includes('axis')) cur?.refresh(); });
  store.on('previewing', () => cur?.onPreview?.());
  store.on('play', () => cur?.onPreview?.());
  build(true);
}

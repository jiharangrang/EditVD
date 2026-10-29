// 편집 화면 틀: 위쪽 막대 · 왼쪽 탭 · 미리보기 · 속성 · 타임라인(높이 조절) · 단축키 · 파일 끌어놓기 (spec 11)
import { store, prefs } from './store.js';
import { api } from './api.js';
import { h, btn, iconBtn, tip, toast, sheet, inlineEdit, fileDrop, fileInfo, kbd, clamp, ring, isTyping, sheetOpen, popoverOpen, debounce, VIDEO_EXT, SCRIPT_EXT } from './ui.js';
import { icon, logo } from './icons.js';
import { fmt } from './time.js';

const MODS = {
  player: './player.js', timeline: './timeline.js', inspector: './inspector.js', export: './export.js',
  media: './panels/media.js', cuts: './panels/cuts.js', subs: './panels/subs.js', memo: './panels/memo.js',
};
export const TAB_DEF = [
  {id: 'media', label: '미디어', icon: 'media'},
  {id: 'cuts', label: '컷', icon: 'scissors'},
  {id: 'subs', label: '자막', icon: 'captions'},
  {id: 'memo', label: '메모', icon: 'message-text'},
];
export const SHORTCUTS = [   // [묶음, [[이름, 키], ...]] — 시트에서 묶음마다 위에서 아래로 읽어요
  ['재생', [['재생 · 멈춤', 'space'], ['1프레임', 'left right'], ['1초', 'shift+left shift+right'], ['이전 · 다음', 'up down'], ['앞뒤로 훑기', '휠']]],
  ['편집', [['되돌리기', 'mod+z'], ['다시 하기', 'mod+shift+z'], ['분할', 'mod+b'], ['앞부분 자르기', 'q'], ['뒷부분 자르기', 'w'], ['삭제', 'delete']]],
  ['컷 확인', [['컷 자르기', 'x'], ['컷 살리기', 'v'], ['듣기', 'enter']]],
  ['자막 · 메모', [['자막 추가', 't'], ['메모 추가', 'm'], ['화자 지정', '1–9']]],
  ['보기', [['확대 · 축소', 'plus minus'], ['휠로 확대', 'shift+휠 mod+휠'], ['끌어서 확대', 'shift+끌기'], ['화면 맞춤', 'shift+z'], ['선택 해제', 'esc'], ['단축키', '?']]],
];
const TL_MIN = 160, TL_DEFAULT = 300;

export async function renderEditor(root, name) {
  root.replaceChildren(h('div.screen-msg', {}, ring(-1, 28)));
  try { await store.open(name); }
  catch (e) {
    root.replaceChildren(h('div.screen-msg', {}, icon('alert', 28),
      h('div', {}, e.status === 404 ? '프로젝트가 없어요' : e.message), btn('홈으로', {icon: 'home', onClick: goHome})));
    return;
  }

  // ---------- 위쪽 막대 ----------
  const titleEl = h('div.tb-title', {tabindex: 0, role: 'button', tip: '이름 바꾸기'});
  const rename = () => inlineEdit(titleEl, {value: store.project.title || store.name, cls: 'tb-title-input',
    onCommit: v => store.edit([{op: 'set', key: 'title', value: v}], '이름 바꾸기')});
  titleEl.addEventListener('click', rename);
  titleEl.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); rename(); } });
  const durEdit = h('b'), durOrig = h('span');
  const saveEl = h('div.save-ind', {role: 'status'}, h('i'), h('span'));
  const topbar = h('header.topbar', {},
    logo(24),
    btn('홈', {icon: 'chevron-left', cls: 'ghost sm', onClick: goHome, tip: '홈으로'}),
    h('span.vsep'),
    titleEl,
    h('div.sp'),
    h('div.tb-dur', {tip: '편집본 · 원본 길이'}, durEdit, durOrig),
    saveEl,
    iconBtn('keyboard', '단축키', showKeys, {key: '?'}),
    btn('내보내기', {icon: 'export', cls: 'primary', onClick: () => store.command('export')}));

  // ---------- 왼쪽 탭 ----------
  const tabs = TAB_DEF.map(t => {
    const b = h('button.ltab', {type: 'button', role: 'tab', id: `tab-${t.id}`, 'aria-controls': `pane-${t.id}`, onclick: () => store.setTab(t.id)},
      icon(t.icon, 22), h('span', {}, t.label));
    b.badge = h('span.badge', {hidden: true});
    b.append(b.badge);
    return b;
  });
  const mediaRing = ring(0, 14);
  mediaRing.hidden = true;
  tabs[0].append(mediaRing);
  const panes = Object.fromEntries(TAB_DEF.map(t => [t.id, h('div.tabpane', {role: 'tabpanel', id: `pane-${t.id}`, 'aria-labelledby': `tab-${t.id}`, dataset: {tab: t.id}})]));
  const tablist = h('nav.ltabs', {role: 'tablist', 'aria-label': '편집 항목'}, tabs);
  noFocus(topbar); noFocus(tablist);
  tablist.addEventListener('keydown', e => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault(); e.stopPropagation();
    const i = TAB_DEF.findIndex(t => t.id === store.tab), j = clamp(i + (e.key === 'ArrowRight' ? 1 : -1), 0, TAB_DEF.length - 1);
    store.setTab(TAB_DEF[j].id);
    tabs[j].focus();
  });
  const left = h('section.panel.ed-left', {}, tablist, h('div.lbody', {}, Object.values(panes)));

  // ---------- 가운데 · 오른쪽 · 아래 ----------
  const playerEl = h('section.panel.ed-player', {'aria-label': '미리보기'});
  const inspEl = h('aside.panel.ed-inspector', {'aria-label': '속성'});
  const tlEl = h('section.panel.ed-timeline', {'aria-label': '타임라인'});
  const exportHost = h('div.export-host');
  const resize = h('div.ed-resize', {role: 'separator', 'aria-orientation': 'horizontal', 'aria-label': '타임라인 높이', tabindex: 0});
  let tlH = clamp(+prefs.get('tlHeight', TL_DEFAULT) || TL_DEFAULT, TL_MIN, 900);
  const app = h('div.app', {style: {'--tl-h': `${tlH}px`}}, topbar, h('main.ed-main', {}, left, playerEl, inspEl), resize, tlEl, exportHost);
  root.replaceChildren(app);

  // ---------- 상태 → 화면 ----------
  const renderProject = () => {
    const p = store.project;
    titleEl.textContent = p.title || store.name;
    document.title = `${p.title || store.name} · EditVDO`;
    durEdit.textContent = fmt(store.editedDuration, 0);
    durOrig.replaceChildren('원본 ', h('span', {}, fmt(store.total, 0)));
    setBadge(tabs[1].badge, store.derived.unreviewed);
    setBadge(tabs[3].badge, store.derived.openComments);
    const busy = p.sources.filter(s => s.status === 'queued' || s.status === 'analyzing');
    mediaRing.hidden = !busy.length;
    if (busy.length) mediaRing.set(busy.reduce((a, s) => a + (+s.progress || 0), 0) / busy.length || 0.02);
    if (busy.length) tabs[0].dataset.tip = '분석 중'; else delete tabs[0].dataset.tip;
  };
  const renderTabs = () => {
    TAB_DEF.forEach((t, i) => {
      tabs[i].setAttribute('aria-selected', String(store.tab === t.id));
      tabs[i].tabIndex = store.tab === t.id ? 0 : -1;
      panes[t.id].hidden = store.tab !== t.id;
    });
  };
  const renderSave = () => {
    const {state, message} = store.save;
    saveEl.className = `save-ind ${state}`;
    saveEl.lastChild.textContent = state === 'saving' ? '저장 중' : state === 'error' ? '저장 안 됨' : '저장됨';
    tip(saveEl, state === 'error' ? (message || '저장하지 못했어요') : state === 'saving' ? '저장하고 있어요' : '모두 저장됐어요');
  };
  renderProject(); renderTabs(); renderSave();
  store.on('project', renderProject);
  store.on('view', keys => { if (keys.includes('tab')) renderTabs(); });
  store.on('save', renderSave);
  store.on('error', e => toast(e?.message || '저장하지 못했어요', {kind: 'error'}));
  store.on('external', ({items, notes}) => {
    const n = items.length + (notes ? 1 : 0);
    toast('Claude가 고쳤어요', {kind: 'claude', icon: 'pencil', timeout: 8000,
      action: n ? {label: n > 1 ? `보기 ${n}` : '보기', keep: n > 1, onClick: () => store.jumpChanged()} : null});
  });
  store.on('job', j => {
    if (j.state === 'error') { if (j.kind !== 'export') toast(j.message || '작업이 실패했어요', {kind: 'error'}); }   // 내보내기 실패는 내보내기 창이 알려요
    else if (j.kind === 'analyze' || j.kind === 'transcribe') toast(j.kind === 'analyze' ? '분석 끝' : '음성 인식 끝', {kind: 'ok', icon: 'check'});
  });

  // ---------- 모듈 ----------
  await Promise.all([['player', playerEl], ['timeline', tlEl], ['inspector', inspEl], ['export', exportHost], ...TAB_DEF.map(t => [t.id, panes[t.id]])]
    .map(([k, el]) => mountModule(k, el)));

  // ---------- 타임라인 높이 ----------
  const setH = (v, save) => {
    tlH = Math.round(clamp(v, TL_MIN, Math.max(TL_MIN + 40, innerHeight - 48 - 300)));
    app.style.setProperty('--tl-h', `${tlH}px`);
    store.emit('layout');
    if (save) prefs.set('tlHeight', tlH);
  };
  resize.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    e.preventDefault();
    resize.setPointerCapture(e.pointerId);
    const y0 = e.clientY, h0 = tlH;
    resize.classList.add('drag');
    document.body.classList.add('resizing');
    const move = ev => setH(h0 + (y0 - ev.clientY));
    const up = () => {
      resize.removeEventListener('pointermove', move);
      resize.classList.remove('drag');
      document.body.classList.remove('resizing');
      prefs.set('tlHeight', tlH);
    };
    resize.addEventListener('pointermove', move);
    resize.addEventListener('pointerup', up, {once: true});
    resize.addEventListener('pointercancel', up, {once: true});
  });
  resize.addEventListener('dblclick', () => setH(TL_DEFAULT, true));
  resize.addEventListener('keydown', e => {
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    e.preventDefault(); e.stopPropagation();
    setH(tlH + (e.key === 'ArrowUp' ? 20 : -20), true);
  });
  addEventListener('resize', debounce(() => { setH(tlH); }, 100));
  setH(tlH);

  // ---------- 단축키 · 끌어놓기 ----------
  bindKeys();
  fileDrop(window, {label: '놓아서 추가', icon: 'plus-circle', onDrop: onDropFiles});
  window.ev = {store, api};
}

// 마우스로 누른 버튼·탭은 포커스를 가져가지 않아요: 다음 단축키에 시안 테두리가 켜지지 않고 ←/→·↵ 가 계속 전체에 가요.
// 쓰던 입력칸은 보통 클릭처럼 놓아요 (고친 글이 저장되고, X·V 가 그 칸에 글자로 들어가지 않게).
function noFocus(root) {
  root.addEventListener('mousedown', e => {
    if (!(e.target instanceof Element) || !e.target.closest('button,[role=button]')) return;
    e.preventDefault();
    if (isTyping()) document.activeElement.blur();
  });
}

function setBadge(el, n) {
  el.hidden = !n;
  el.textContent = n > 99 ? '99+' : String(n || '');
}

async function mountModule(key, el) {
  try {
    const m = await import(MODS[key]);
    const fn = m.mount || m.default;
    if (typeof fn !== 'function') throw new Error(`${key}: mount 가 없어요`);
    await fn(el, store);
  } catch (e) {
    console.error(`[${key}]`, e);
    el.replaceChildren(h('div.stub.mod-error', {}, icon('alert', 20), h('span', {}, '불러오지 못했어요')));
  }
}

async function goHome() {
  await Promise.race([store.flush(), new Promise(r => setTimeout(r, 1500))]);
  location.hash = '';
}

export function showKeys() {
  if (sheetOpen()) return;
  sheet({title: '단축키', icon: 'keyboard', width: 600,
    body: h('div.keys-grid', {}, SHORTCUTS.map(([group, rows]) => h('section.key-group', {}, h('h3', {}, group),
      rows.map(([label, k]) => h('div.key-row', {}, h('span', {}, label), h('span.keys', {}, kbd(k)))))))});
}

function undoRedo(redo) {
  const label = redo ? store.redo() : store.undo();
  if (label === null) return toast(redo ? '다시 할 게 없어요' : '되돌릴 게 없어요', {timeout: 1200});
  toast(`${redo ? '다시 하기' : '되돌리기'}${label ? ` · ${label}` : ''}`, {icon: redo ? 'redo' : 'undo', timeout: 1400});
}

// 전역 단축키 (spec 11.6). 입력 중이거나 시트·팝오버가 열려 있으면 무시해요.
// 셸이 직접 하는 것: 재생, 프레임 이동, 되돌리기/다시 하기, Esc, ? 시트.
// 나머지는 store.command(이름) → 모듈이 store.on('cmd', ctx) 로 받아 처리하고 ctx.handled = true.
function bindKeys() {
  let spaceDown = false;
  addEventListener('keydown', e => {
    if (e.defaultPrevented || e.isComposing || e.keyCode === 229) return;
    if (sheetOpen() || popoverOpen()) return;
    const t = e.target instanceof Element ? e.target : null, code = e.code;
    if (isTyping(t) || t?.closest('.popover,.sheet,[data-keys="own"]')) return;
    if (t?.matches('input[type=range]') && /^(Arrow|Home|End|Page)/.test(e.key)) return;
    if (t?.matches('input[type=checkbox],input[type=radio]') && code === 'Space') return;
    const run = fn => {
      e.preventDefault();
      if (t?.matches('button,[role=button],[role=tab],[role=switch]')) t.blur();   // 클릭으로 남은 버튼 포커스는 놓아요 (단축키가 테두리를 켜지 않게)
      fn();
    };
    const cmd = (name, arg) => run(() => store.command(name, arg, e));
    if (e.metaKey || e.ctrlKey) {
      if (code === 'KeyZ' && !e.altKey) return run(() => undoRedo(e.shiftKey));
      if (code === 'KeyY' && e.ctrlKey) return run(() => undoRedo(true));
      if (code === 'KeyB') return cmd('split');
      return;
    }
    if (e.altKey) return;
    switch (code) {
      case 'Space': spaceDown = true; return run(() => store.togglePlay('key'));
      case 'ArrowLeft': case 'ArrowRight': return run(() => store.step(code === 'ArrowRight' ? 1 : -1, e.shiftKey));
      case 'ArrowUp': case 'ArrowDown': return cmd('nav', code === 'ArrowDown' ? 1 : -1);
      case 'KeyQ': return cmd('trimStart');
      case 'KeyW': return cmd('trimEnd');
      case 'Delete': case 'Backspace': return cmd('delete');
      case 'KeyX': return cmd('cut');
      case 'KeyV': return cmd('keep');
      case 'Enter': case 'NumpadEnter':
        if (t?.closest('button,a,[role=button],[role=tab],[role=menuitem],[role=switch]')) return;
        return cmd('listen');
      case 'KeyT': return cmd('addSub');
      case 'KeyM': return cmd('addMemo');
      case 'KeyZ': return e.shiftKey ? cmd('fit') : undefined;
      case 'Equal': case 'NumpadAdd': return cmd('zoomIn');
      case 'Minus': case 'NumpadSubtract': return cmd('zoomOut');
      case 'Slash': return e.shiftKey ? run(showKeys) : undefined;
      case 'Escape': return run(() => {
        if (store.command('escape', null, e)) return;
        if (store.review) store.setView({review: false});
        else if (store.sel) store.select(null);
        else if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
      });
    }
    if (/^(Digit|Numpad)[1-9]$/.test(code)) return cmd('speaker', +code.slice(-1));
  });
  // 버튼에 포커스가 있을 때 Space 가 버튼을 누르지 않게 해요
  addEventListener('keyup', e => { if (e.code === 'Space' && spaceDown) { spaceDown = false; if (!isTyping(e.target)) e.preventDefault(); } }, true);
}

// 창 위로 끌어놓은 파일: 영상 → 소스 추가, 대본(.srt .vtt .txt) → 자막 불러오기.
// 모듈이 먼저 처리하려면 store.on('cmd:dropFiles', ctx => { ...; ctx.handled = true; })
async function onDropFiles(files) {
  if (store.command('dropFiles', files)) return;
  const vids = files.filter(f => VIDEO_EXT.test(f.name)), scripts = files.filter(f => SCRIPT_EXT.test(f.name));
  try {
    if (!vids.length && scripts.length) {
      const f = scripts[0], text = await readScript(f);
      store.setTab('subs');
      const r = await store.action('대본 불러오기', () => api.importSubs(store.name, text, {filename: f.name}));
      toast(r?.info?.count != null ? `자막 ${r.info.count}줄` : '대본을 불러왔어요', {icon: 'captions', action: {label: '되돌리기', onClick: () => store.undo()}});
      return;
    }
    if (!vids.length) return toast('영상이나 대본 파일만 넣을 수 있어요', {kind: 'error'});
    const paths = await api.resolve(fileInfo(vids));
    if (!paths.length) return toast('파일을 찾지 못했어요', {kind: 'error'});
    await store.flush();
    await api.addSources(store.name, paths);
    await store.reload();
    store.setTab('media');
    toast(`영상 ${paths.length}개 추가`, {kind: 'ok', icon: 'check'});
  } catch (e) { toast(e.message, {kind: 'error'}); }
}

// 대본 파일 글자: UTF-16(BOM) → UTF-8 → CP949 (서버 jobs.read_text 와 같은 순서)
async function readScript(f) {
  const b = new Uint8Array(await f.arrayBuffer());
  if ((b[0] === 0xFF && b[1] === 0xFE) || (b[0] === 0xFE && b[1] === 0xFF)) return new TextDecoder(b[0] === 0xFF ? 'utf-16le' : 'utf-16be').decode(b);
  try { return new TextDecoder('utf-8', {fatal: true}).decode(b); } catch { return new TextDecoder('euc-kr').decode(b); }
}

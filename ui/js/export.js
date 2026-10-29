// 내보내기 시트 (spec 11.8): 캡컷 프로젝트(기본) · 영상 MP4 · 자막 SRT → 진행 막대 → 끝나면 캡컷 열기 / Finder에서 보기.
// 캡컷은 보낼 곳을 골라요: 다른 맥으로(기본, target 'folder' = 영상까지 담은 폴더) · 이 맥 캡컷(target 'capcut').
// 위쪽 막대의 내보내기 버튼이 store.command('export') 를 보내요. 시트를 닫아도 작업은 계속되고, 끝나면 알려 줘요.
// 이 맥 캡컷이 켜져 있어 못 넣으면 그 줄에 알림 + 다시 시도 (창으로 돌아오면 저절로 한 번 더 해요).
import { h, btn, sheet, sheetOpen, toast, bar, segmented, copyText } from './ui.js';
import { icon } from './icons.js';
import { api } from './api.js';
import { prefs } from './store.js';
import { fmt } from './time.js';

const KINDS = [
  {id: 'capcut', icon: 'clapperboard', label: '캡컷 프로젝트'},
  {id: 'mp4', icon: 'film', label: '영상 MP4'},
  {id: 'srt', icon: 'captions', label: '자막 SRT'},
];
const CAPCUT_BUSY = /캡컷.*닫|capcut.*(running|close)/i;
const CAPCUT_DIR = '~/Movies/CapCut/User Data/Projects/com.lveditor.draft';   // 다른 맥에서 폴더를 넣을 곳
const pathOf = r => typeof r === 'string' ? r : r ? (r.path || r.draft || r.dir || r.folder || r.out || r.file || r.mp4 || r.srt || '') : '';

function loadCss(name) {
  const id = `css-${name}`;
  let link = document.getElementById(id);
  if (!link) { link = h('link', {rel: 'stylesheet', id, href: `/ui/css/${name}.css`}); document.head.append(link); }
  if (link.sheet) return Promise.resolve();
  return new Promise(r => { link.addEventListener('load', r, {once: true}); link.addEventListener('error', r, {once: true}); setTimeout(r, 1500); });
}

export async function mount(el, store) {
  await loadCss('export');
  let job = null;    // 진행 중이거나 막 끝난 작업 {id, kind, state, progress, message, path, error}
  let ui = null;     // 열린 시트

  store.on('cmd:export', ctx => { ctx.handled = true; open(); });

  async function run(kind, opts) {
    job = {id: null, kind, target: opts.target || '', state: 'run', progress: 0, message: '', path: '', error: ''};
    const me = job, inCapCut = me.target === 'capcut';
    ui?.paint();
    try {
      await store.flush();
      const r = await api.exportProject(store.name, opts);
      if (!r?.job) throw new Error('작업을 시작하지 못했어요');
      me.id = r.job;
      const j = await store.waitJob(r.job, x => {
        if (me !== job) return;
        me.progress = Math.max(me.progress, +x.progress || 0);
        me.message = x.message || '';
        ui?.paintProgress();
      });
      if (me !== job) return;
      Object.assign(me, {state: 'done', progress: 1, path: pathOf(j.result)});
    } catch (e) {
      if (me !== job) return;
      const busy = inCapCut && (CAPCUT_BUSY.test(e.message || '') || (e.status === 409 && /캡컷|capcut/i.test(e.message || '')));
      Object.assign(me, {state: busy ? 'busy' : 'error', error: e.message || '내보내지 못했어요'});
    }
    if (ui) ui.paint();
    else if (me.state === 'done') toast(inCapCut ? '캡컷에 넣었어요' : '내보냈어요', {kind: 'ok', icon: 'check', timeout: 8000,
      action: inCapCut ? {label: '캡컷 열기', onClick: () => openCapCut()} : me.path ? {label: 'Finder에서 보기', onClick: () => reveal(me.path)} : null});
    else if (me.state === 'busy') toast('캡컷을 닫고 다시 내보내 주세요', {kind: 'error', action: {label: '열기', onClick: open}});
    else toast(me.error, {kind: 'error'});
  }
  const reveal = path => api.reveal(path).catch(e => toast(e.message, {kind: 'error'}));
  const openCapCut = () => api.openApp('CapCut').catch(e => toast(e.message, {kind: 'error'}));

  function open() {
    if (sheetOpen() || !store.project) return;
    const saved = prefs.get('export', {});
    let kind = job && (job.state === 'run' || job.state === 'busy') ? job.kind : KINDS.some(k => k.id === saved.kind) ? saved.kind : 'capcut';
    let target = job && (job.state === 'run' || job.state === 'busy') && job.target ? job.target : saved.target === 'capcut' ? 'capcut' : 'folder';
    if (job && job.state !== 'run') job = null;   // 끝난 결과는 다시 열면 새로 시작해요 (busy 는 알림을 보여요)
    const title = store.project.title || store.name;
    const nameInp = h('input.input', {value: title, spellcheck: false, 'aria-label': '이름', maxlength: 80});
    const check = (label, on) => { const i = h('input', {type: 'checkbox', checked: !!on}); const l = h('label.check', {}, i, h('span', {}, label)); l.input = i; return l; };
    const subsChk = check('자막 포함', saved.include_subs ?? true);
    const burnChk = check('자막 입히기', saved.burn ?? true);
    const targetSeg = segmented([{value: 'folder', label: '다른 맥으로'}, {value: 'capcut', label: '이 맥 캡컷'}], target, v => {
      target = v;
      if (job && job.state !== 'run') job = null;
      paint();
    }, {cls: 'ex-target', label: '보낼 곳'});
    const notice = h('div.ex-notice', {hidden: true}, icon('alert-triangle', 16), h('span', {}, '캡컷을 닫으면 바로 넣어요'),
      btn('다시 시도', {icon: 'refresh', cls: 'xs', onClick: () => go()}));

    // 줄마다 짧은 정보: 클립 수 · 편집본 길이 · 자막 줄 수
    const subsN = (store.project.subtitles || []).filter(s => s.text && !store.isHidden(s)).length;
    const META = {capcut: `클립 ${store.derived.keep.length}`, mp4: fmt(store.editedDuration, 0), srt: `${subsN}줄`};
    const rows = KINDS.map(k => {
      const opts = k.id === 'capcut' ? h('div.ex-opts', {}, targetSeg, nameInp, h('div.ex-checks', {}, subsChk), notice)
        : k.id === 'mp4' ? h('div.ex-opts', {}, h('div.ex-checks', {}, burnChk)) : null;
      const head = h('button.ex-head', {type: 'button', role: 'radio', onclick: () => pick(k.id)},
        h('span.ex-ico', {}, icon(k.icon, 20)), h('span.ex-label', {}, k.label), h('span.ex-meta.tc', {}, META[k.id]), h('span.ex-radio'));
      const row = h('div.ex-row', {dataset: {kind: k.id}}, head, opts);
      row.head = head;
      return row;
    });
    const list = h('div.ex-list', {role: 'radiogroup', 'aria-label': '형식'}, rows);
    list.addEventListener('keydown', e => {
      if (!['ArrowDown', 'ArrowUp'].includes(e.key) || !e.target.matches('.ex-head')) return;
      e.preventDefault();
      const i = KINDS.findIndex(k => k.id === kind), j = (i + (e.key === 'ArrowDown' ? 1 : -1) + KINDS.length) % KINDS.length;
      pick(KINDS[j].id);
      rows[j].head.focus();
    });

    const prog = bar(0, 'lg'), progT = h('span.t'), progP = h('span.p.tc');
    const progBox = h('div.ex-prog', {hidden: true}, h('div.ex-prog-row', {}, progT, progP), prog);
    const donePath = h('span.ex-path.selectable');
    const copyDir = btn('캡컷 폴더 경로 복사', {icon: 'copy', cls: 'xs ghost', tip: CAPCUT_DIR, onClick: async () => {
      if (await copyText(CAPCUT_DIR)) toast('복사됨', {icon: 'check', timeout: 1400});
    }});
    const doneBox = h('div.ex-done', {hidden: true}, h('span.ex-done-ico', {}, icon('check', 18)), h('div.ex-done-t', {}, h('b'), donePath), copyDir);
    const errBox = h('div.ex-err', {hidden: true}, icon('alert', 16), h('span'));

    const closeBtn = btn('취소', {cls: 'ghost', onClick: () => s.close()});
    const revealBtn = btn('Finder에서 보기', {icon: 'folder-open', onClick: () => { if (job?.path) reveal(job.path); }});
    const capBtn = btn('캡컷 열기', {icon: 'external-link', cls: 'primary', onClick: () => { openCapCut(); s.close(); }});
    const goBtn = btn('내보내기', {icon: 'export', cls: 'primary', onClick: () => go()});
    const s = sheet({title: '내보내기', icon: 'export', width: 480, cls: 'ex-sheet',
      body: h('div.ex-body', {}, list, progBox, doneBox, errBox), foot: [closeBtn, revealBtn, capBtn, goBtn],
      onClose: () => { ui = null; removeEventListener('focus', onFocus); }});

    function pick(id) {
      if (job?.state === 'run') return;
      kind = id;
      if (job && job.state !== 'run') job = null;
      paint();
    }
    function go() {
      if (job?.state === 'run') return;
      const opts = {kind};
      if (kind === 'capcut') {
        opts.target = target;
        const nm = nameInp.value.trim();
        if (nm) opts.name = nm;
        opts.include_subs = subsChk.input.checked;
      }
      if (kind === 'mp4') opts.burn = burnChk.input.checked;
      prefs.set('export', {kind, target, include_subs: subsChk.input.checked, burn: burnChk.input.checked});
      run(kind, opts);
    }
    // 캡컷을 닫고 돌아오면 한 번 더
    function onFocus() { if (job?.state === 'busy' && job.kind === kind) go(); }
    addEventListener('focus', onFocus);
    nameInp.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); go(); } });

    function paintProgress() {
      if (!job) return;
      prog.set(job.state === 'done' ? 1 : job.progress);
      progT.textContent = job.message || (job.id ? '내보내는 중' : '준비');
      progP.textContent = `${Math.round((job.state === 'done' ? 1 : job.progress) * 100)}%`;
    }
    function paint() {
      const st = job?.state || 'idle', runK = job?.kind, inCapCut = runK === 'capcut' && job.target === 'capcut', locked = st === 'run' || st === 'done';
      rows.forEach((r, i) => {
        const on = KINDS[i].id === kind;
        r.classList.toggle('on', on);
        r.head.setAttribute('aria-checked', String(on));
        r.head.tabIndex = on ? 0 : -1;
      });
      list.classList.toggle('locked', locked);
      for (const i of list.querySelectorAll('input')) i.disabled = locked;
      targetSeg.set(target);
      for (const b of targetSeg.buttons) b.disabled = locked;
      notice.hidden = !(st === 'busy' && inCapCut && kind === 'capcut' && target === 'capcut');
      progBox.hidden = st !== 'run';
      doneBox.hidden = st !== 'done';
      errBox.hidden = st !== 'error';
      if (st === 'error') errBox.lastChild.textContent = job.error;
      if (st === 'done') {
        doneBox.querySelector('b').textContent = inCapCut ? '캡컷에 넣었어요' : '내보냈어요';
        const parts = (job.path || '').split('/').filter(Boolean);
        donePath.textContent = parts.length > 2 ? `…/${parts.slice(-2).join('/')}` : job.path || '';
        donePath.title = job.path || '';
        donePath.hidden = !job.path;
        copyDir.hidden = !(runK === 'capcut' && !inCapCut);   // 다른 맥으로: 그 맥에서 폴더를 넣을 곳
      }
      paintProgress();
      closeBtn.lastChild.textContent = st === 'idle' || st === 'busy' || st === 'error' ? '취소' : '닫기';
      revealBtn.hidden = st !== 'done' || !job.path;
      revealBtn.classList.toggle('primary', st === 'done' && !inCapCut);
      capBtn.hidden = !(st === 'done' && inCapCut);
      goBtn.hidden = st === 'done';
      goBtn.disabled = st === 'run';
      goBtn.lastChild.textContent = st === 'run' ? '내보내는 중' : st === 'error' ? '다시 시도' : '내보내기';
    }
    ui = {paint, paintProgress};
    paint();
    (rows[KINDS.findIndex(k => k.id === kind)]?.head || goBtn).focus({preventScroll: true});
  }
}

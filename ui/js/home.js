// 홈: 새 프로젝트 카드 + 최근 프로젝트 (spec 11.1)
import { api } from './api.js';
import { h, iconBtn, menu, toast, inlineEdit, fileDrop, fileInfo, VIDEO_EXT } from './ui.js';
import { icon, logo } from './icons.js';
import { fmt, ago } from './time.js';

const open = name => { location.hash = `p=${encodeURIComponent(name)}`; };

export async function renderHome(root) {
  document.title = 'EditVDO';
  let busy = false;
  const label = h('span', {}, '새 프로젝트');
  const plus = h('span.plus', {}, icon('plus', 22));
  const card = h('button.new-card', {type: 'button', onclick: () => pick()}, decoration(), plus, label);
  const count = h('span.n');
  const grid = h('div.pgrid', {}, [0, 1, 2, 3].map(() => h('div.pcard', {}, h('div.cover.skel'), h('div.t', {}, ' '))));
  root.replaceChildren(h('div.home', {},
    h('header.home-top', {}, h('span.logo', {}, logo(26), 'EditVDO')),
    h('main.home-main', {}, card, h('div.sec-head', {}, '프로젝트', count), grid)));

  async function pick() {
    if (busy) return;
    try {
      const paths = await api.pick('video');
      if (paths.length) await create(paths);
    } catch (e) { toast(e.message, {kind: 'error'}); }
  }
  async function create(paths) {
    busy = true;
    plus.replaceChildren(h('span.ring.spin', {style: {width: '20px', height: '20px'}}));
    label.textContent = '만드는 중';
    try { open((await api.create(paths)).name); }
    catch (e) {
      toast(e.message, {kind: 'error'});
      plus.replaceChildren(icon('plus', 22));
      label.textContent = '새 프로젝트';
    } finally { busy = false; }
  }
  fileDrop(window, {label: '놓으면 새 프로젝트', icon: 'plus-circle', onDrop: async files => {
    const vids = files.filter(f => VIDEO_EXT.test(f.name));
    if (!vids.length) return toast('영상 파일만 넣을 수 있어요', {kind: 'error'});
    try {
      const paths = await api.resolve(fileInfo(vids));
      if (!paths.length) return toast('파일을 찾지 못했어요', {kind: 'error'});
      if (paths.length < vids.length) toast(`${vids.length - paths.length}개는 못 찾았어요`);
      await create(paths);
    } catch (e) { toast(e.message, {kind: 'error'}); }
  }});

  let list = [];
  try { list = await api.projects() || []; }
  catch (e) {
    grid.replaceChildren();
    grid.after(h('div.home-empty', {}, icon('alert', 18), e.message));
    return;
  }
  list.sort((a, b) => String(b.updated || '').localeCompare(String(a.updated || '')));
  count.textContent = list.length || '';
  if (!list.length) {
    grid.replaceWith(h('div.home-empty', {}, icon('film', 18), '프로젝트가 없어요'));
    return;
  }
  grid.replaceChildren(...list.map(projectCard));
}

function projectCard(p) {
  const title = h('div.t', {}, p.title || p.name);
  const len = p.edited ?? p.duration;
  const cover = h('div.cover', {}, h('div.ph', {}, icon('film', 24)),
    len ? h('span.tag.br', {}, fmt(len, 0)) : null,
    p.legacy ? h('span.tag.tl', {}, '이전 버전') : null);
  const more = iconBtn('more', '더 보기', e => {
    e.stopPropagation();
    menu(more, [
      {label: '이름 바꾸기', icon: 'pencil', disabled: !!p.legacy, onClick: rename},
      {label: 'Finder에서 보기', icon: 'folder-open', onClick: () => api.reveal(p.path || `projects/${p.name}`).catch(err => toast(err.message, {kind: 'error'}))},
    ], {align: 'end'});
  }, {cls: 'more'});
  cover.append(more);
  const meta = [ago(p.updated), p.sources > 1 ? `클립 ${p.sources}` : null].filter(Boolean);
  const el = h('div.pcard', {tabindex: 0, role: 'link', 'aria-label': p.title || p.name,
    onclick: e => { if (!e.target.closest('.more,.inline-input')) open(p.name); },
    onkeydown: e => { if (e.key === 'Enter' && e.target === el) open(p.name); }},
  cover, title, h('div.m', {}, meta.map((m, i) => [i ? h('span', {}, '·') : null, h('span', {}, m)])));
  function rename() {
    inlineEdit(title, {value: p.title || p.name, onCommit: async v => {
      const old = title.textContent;
      title.textContent = v;
      try { await api.rename(p.name, v); p.title = v; }
      catch (err) { title.textContent = old; toast(err.message, {kind: 'error'}); }
    }});
  }
  if (p.cover) drawCover(cover, p.cover, !!p.legacy);
  return el;
}

// 썸네일 묶음(thumbs.jpg) 에서 앞쪽 한 칸을 잘라 표지로 그려요. single: 한 장짜리 그림(이전 버전 cover.jpg)은 통째로
async function drawCover(box, url, single) {
  let meta = null;
  if (!single) try { const r = await fetch(url.replace(/\.jpg(\?.*)?$/, '.json'), {cache: 'no-store'}); if (r.ok) meta = await r.json(); } catch { /* 없으면 추정 */ }
  const img = new Image();
  img.src = url;
  try { await img.decode(); } catch { return; }
  const cols = single ? 1 : meta?.cols || 10, count = meta?.count || cols;
  const tw = meta?.w || img.naturalWidth / cols, th = meta?.h || (single ? img.naturalHeight : tw * 9 / 16);
  const k = Math.min(count - 1, Math.floor(count * 0.08)), sx = (k % cols) * tw, sy = Math.floor(k / cols) * th;
  const dpr = Math.min(2, devicePixelRatio || 1), W = Math.round((box.clientWidth || 240) * dpr), H = Math.round(W * 9 / 16);
  const c = h('canvas', {width: W, height: H});
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#0A0A0C';
  ctx.fillRect(0, 0, W, H);
  const cover = Math.max(W / tw, H / th), fit = Math.min(W / tw, H / th);
  if (fit < cover * 0.98 && 'filter' in ctx) {   // 세로 영상: 흐린 배경 위에 가운데
    ctx.filter = 'blur(14px) brightness(.45)';
    ctx.drawImage(img, sx, sy, tw, th, (W - tw * cover) / 2, (H - th * cover) / 2, tw * cover, th * cover);
    ctx.filter = 'none';
  }
  const s = fit < cover * 0.98 ? fit : cover;
  ctx.drawImage(img, sx, sy, tw, th, (W - tw * s) / 2, (H - th * s) / 2, tw * s, th * s);
  box.querySelector('.ph')?.remove();
  box.prepend(c);
}

// 카드 아래쪽에 옅은 파형 장식
function decoration() {
  let d = '';
  for (let i = 0; i < 100; i++) {
    const a = Math.abs(Math.sin(i * 0.41) * 0.55 + Math.sin(i * 0.13 + 1.3) * 0.45);
    const len = 6 + a * 46, x = 8 + i * 12;
    d += `M${x} ${(60 - len / 2).toFixed(1)}V${(60 + len / 2).toFixed(1)}`;
  }
  const t = document.createElement('template');
  t.innerHTML = `<svg class="deco" viewBox="0 0 1200 120" preserveAspectRatio="xMidYMax slice" aria-hidden="true"><path d="${d}" stroke="#fff" stroke-width="3.2" stroke-linecap="round" fill="none"/></svg>`;
  return t.content.firstElementChild;
}

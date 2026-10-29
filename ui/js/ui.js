// 작은 UI 도구: h() 로 요소 만들기, 툴팁·팝오버·메뉴·토스트·시트·슬라이더·스위치·세그먼트·칩.
import { icon } from './icons.js';

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
export const isMac = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent);

// ---------- 요소 만들기 ----------
// h('button.btn.primary#go', {onclick, 'aria-label': '..', style: {'--p': .5}, dataset: {id}}, '글자', node, [배열], null)
export function h(sel, attrs, ...kids) {
  if (attrs == null || typeof attrs !== 'object' || attrs instanceof Node || Array.isArray(attrs)) {
    if (attrs != null) kids.unshift(attrs);
    attrs = {};
  }
  const m = /^([\w-]*)(.*)$/.exec(sel);
  const el = document.createElement(m[1] || 'div');
  for (const part of m[2].match(/[.#][\w-]+/g) || []) part[0] === '.' ? el.classList.add(part.slice(1)) : (el.id = part.slice(1));
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class' || k === 'className') { for (const c of String(v).split(/\s+/)) if (c) el.classList.add(c); }
    else if (k === 'style') {
      if (typeof v === 'string') el.style.cssText += v;
      else for (const [sk, sv] of Object.entries(v)) if (sv != null) sk.startsWith('--') ? el.style.setProperty(sk, sv) : (el.style[sk] = sv);
    }
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'ref') v(el);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'tip') { el.dataset.tip = v; if (!el.hasAttribute('aria-label')) el.setAttribute('aria-label', v); }
    else if (k === 'key') el.dataset.key = v;
    else if (k.includes('-') || !(k in el)) el.setAttribute(k, v === true ? '' : v);
    else el[k] = v;
  }
  append(el, kids);
  return el;
}

export function append(el, kids) {
  for (const k of kids) {
    if (k == null || k === false || k === true) continue;
    if (Array.isArray(k)) append(el, k);
    else el.append(k instanceof Node ? k : String(k));
  }
  return el;
}

// 모듈 전용 CSS 를 한 번만 넣어요: css('timeline', `.tl{...}`)
const injected = new Set();
export function css(id, text) {
  if (injected.has(id)) return;
  injected.add(id);
  document.head.append(h('style', {dataset: {mod: id}}, text));
}

// ---------- 단축키 표기 ----------
const KEYS = {mod: isMac ? '⌘' : 'Ctrl', cmd: '⌘', ctrl: 'Ctrl', shift: '⇧', alt: isMac ? '⌥' : 'Alt', opt: '⌥', space: 'Space',
  enter: '↵', esc: 'Esc', del: 'Delete', delete: 'Delete', backspace: '⌫', left: '←', right: '→', up: '↑', down: '↓', tab: 'Tab', plus: '+', minus: '−'};
// 'mod+shift+z' → '⇧⌘Z' (맥) · 'q' → 'Q' · 'left right' → '← →'
export function keyLabel(k) {
  return String(k).split(' ').map(combo => {
    const parts = combo.split('+').filter(Boolean).map(p => KEYS[p.toLowerCase()] ?? (p.length === 1 ? p.toUpperCase() : p));
    if (isMac) parts.sort((a, b) => order(a) - order(b));
    return parts.join(isMac ? '' : '+');
  }).join(' ');
}
const order = p => ({'⌃': 0, '⌥': 1, '⇧': 2, '⌘': 3}[p] ?? 9);
export const kbd = k => keyLabel(k).split(' ').map(x => h('kbd', {}, x));

// ---------- 툴팁 (400ms, 이름 + 단축키) ----------
let tipEl = null, tipTimer = 0, tipFor = null;
export function tip(el, label, key) {
  el.dataset.tip = label;
  if (key) el.dataset.key = key; else delete el.dataset.key;
  if (!el.hasAttribute('aria-label') && !el.textContent.trim()) el.setAttribute('aria-label', label);
  if (tipFor === el && tipEl) showTip(el);
  return el;
}
function showTip(el) {
  if (!el.isConnected || !el.dataset.tip) return hideTip();
  tipEl ||= h('div.tooltip', {role: 'tooltip'});
  tipEl.replaceChildren(h('span', {}, el.dataset.tip), ...(el.dataset.key ? [h('span.keys', {}, kbd(el.dataset.key))] : []));
  document.body.append(tipEl);
  const r = el.getBoundingClientRect(), t = tipEl.getBoundingClientRect();
  let top = r.top - t.height - 8;
  if (el.dataset.tipPlace === 'bottom' || top < 6) top = r.bottom + 8;
  if (top + t.height > innerHeight - 6) top = r.top - t.height - 8;
  const left = clamp(r.left + r.width / 2 - t.width / 2, 6, innerWidth - t.width - 6);
  tipEl.style.left = `${left}px`;
  tipEl.style.top = `${top}px`;
}
export function hideTip() {
  clearTimeout(tipTimer);
  tipFor = null;
  tipEl?.remove();
}
function initTips() {
  const from = e => e.target instanceof Element ? e.target.closest('[data-tip]') : null;
  document.addEventListener('pointerover', e => {
    const el = from(e);
    if (el === tipFor) return;
    hideTip();
    if (!el || e.pointerType === 'touch') return;
    tipFor = el;
    tipTimer = setTimeout(() => showTip(el), 400);
  });
  document.addEventListener('pointerout', e => {
    if (tipFor && !(e.relatedTarget instanceof Node && tipFor.contains(e.relatedTarget))) hideTip();
  });
  document.addEventListener('focusin', e => {
    const el = from(e);
    if (!el || !el.matches(':focus-visible')) return;
    hideTip();
    tipFor = el;
    tipTimer = setTimeout(() => showTip(el), 400);
  });
  document.addEventListener('focusout', hideTip);
  document.addEventListener('pointerdown', hideTip, true);
  document.addEventListener('keydown', hideTip, true);
  document.addEventListener('wheel', hideTip, {passive: true, capture: true});
}
initTips();

// ---------- 버튼 ----------
// iconBtn('scissors', '분할', fn, {key: 'mod+b', cls: 'ghost', size: 18, pressed: false})
export function iconBtn(name, label, onClick, {key, cls = 'ghost', size = 18, pressed, disabled} = {}) {
  const b = h('button.btn.icon', {type: 'button', class: cls, 'aria-label': label, onclick: onClick, disabled}, icon(name, size));
  b.dataset.tip = label;
  if (key) b.dataset.key = key;
  if (pressed != null) b.setAttribute('aria-pressed', String(!!pressed));
  return b;
}
// btn('내보내기', {icon: 'export', cls: 'primary', onClick, tip, key})
export function btn(label, {icon: ic, cls = '', onClick, tip: t, key, disabled, size = 16} = {}) {
  const b = h('button.btn', {type: 'button', class: cls, onclick: onClick, disabled}, ic ? icon(ic, size) : null, label ? h('span', {}, label) : null);
  if (t) { b.dataset.tip = t; if (key) b.dataset.key = key; }
  return b;
}

// ---------- 팝오버 · 메뉴 ----------
let openPop = null;
export const popoverOpen = () => !!openPop;
export function closePopover() { openPop?.close(false); }

// popover(기준요소 또는 {x,y}, 내용, {place:'bottom'|'top'|'right'|'left', align:'start'|'center'|'end', cls, onClose})
// 같은 기준 요소로 다시 부르면 닫혀요 (토글). 바깥 클릭 · Esc 로 닫혀요.
export function popover(anchor, content, {place = 'bottom', align = 'start', cls = '', onClose, offset = 6, focus = true, toggle = true} = {}) {
  if (openPop && toggle && anchor instanceof Element && openPop.anchor === anchor) { openPop.close(); return null; }
  closePopover();
  const el = h('div.popover', {class: cls, role: 'dialog'}, content);
  const prev = document.activeElement;
  document.body.append(el);
  const api = {el, anchor, close, reposition};
  const outside = e => {
    if (el.contains(e.target) || (anchor instanceof Element && anchor.contains(e.target))) return;
    if (e.target instanceof Element && e.target.closest('.popover,.tooltip')) return;
    close(false);
  };
  const onKey = e => {
    if (e.key === 'Escape' && !e.isComposing) { e.preventDefault(); e.stopPropagation(); close(); }
  };
  function reposition() {
    const r = anchor instanceof Element ? anchor.getBoundingClientRect() : {left: anchor.x, right: anchor.x, top: anchor.y, bottom: anchor.y, width: 0, height: 0};
    const w = el.offsetWidth, ht = el.offsetHeight;
    let x, y;
    if (place === 'right' || place === 'left') {
      x = place === 'right' ? r.right + offset : r.left - w - offset;
      if (x + w > innerWidth - 8) x = r.left - w - offset;
      if (x < 8) x = r.right + offset;
      y = align === 'end' ? r.bottom - ht : align === 'center' ? r.top + r.height / 2 - ht / 2 : r.top;
    } else {
      y = place === 'top' ? r.top - ht - offset : r.bottom + offset;
      if (y + ht > innerHeight - 8) y = r.top - ht - offset;
      if (y < 8) y = Math.min(r.bottom + offset, innerHeight - ht - 8);
      x = align === 'end' ? r.right - w : align === 'center' ? r.left + r.width / 2 - w / 2 : r.left;
    }
    el.style.left = `${clamp(x, 8, Math.max(8, innerWidth - w - 8))}px`;
    el.style.top = `${clamp(y, 8, Math.max(8, innerHeight - ht - 8))}px`;
  }
  function close(restore = true) {
    if (!el.isConnected) return;
    el.remove();
    document.removeEventListener('pointerdown', outside, true);
    document.removeEventListener('keydown', onKey, true);
    removeEventListener('resize', reposition);
    if (anchor instanceof Element) anchor.removeAttribute('aria-expanded');
    if (openPop === api) openPop = null;
    onClose?.();
    if (restore && prev instanceof HTMLElement && prev.isConnected) prev.focus({preventScroll: true});
  }
  reposition();
  if (anchor instanceof Element) anchor.setAttribute('aria-expanded', 'true');
  document.addEventListener('pointerdown', outside, true);
  document.addEventListener('keydown', onKey, true);
  addEventListener('resize', reposition);
  openPop = api;
  if (focus) (el.querySelector('[autofocus]') || el.querySelector('input,textarea,select,button,[tabindex]:not([tabindex="-1"])'))?.focus({preventScroll: true});
  return api;
}

// menu(기준, [{label, icon, onClick, key, danger, disabled}, '-', ...], 팝오버옵션)
export function menu(anchor, items, opts = {}) {
  const list = h('div.menu', {role: 'menu'});
  let pop = null;
  for (const it of items) {
    if (!it) continue;
    if (it === '-') { list.append(h('div.menu-sep', {role: 'separator'})); continue; }
    list.append(h('button.menu-item', {
      type: 'button', role: 'menuitem', class: it.danger ? 'danger' : '', disabled: it.disabled,
      onclick: () => { pop?.close(); it.onClick?.(); },
    }, it.icon ? icon(it.icon, 16) : null, h('span', {}, it.label), it.key ? kbd(it.key) : null));
  }
  list.addEventListener('keydown', e => {
    const all = $$('.menu-item:not(:disabled)', list), i = all.indexOf(document.activeElement);
    const go = j => { e.preventDefault(); all[(j + all.length) % all.length]?.focus(); };
    if (e.key === 'ArrowDown') go(i + 1);
    else if (e.key === 'ArrowUp') go(i - 1);
    else if (e.key === 'Home') go(0);
    else if (e.key === 'End') go(all.length - 1);
  });
  pop = popover(anchor, list, {cls: 'menu-pop', ...opts});
  return pop;
}

// ---------- 토스트 (아래 가운데 알약) ----------
let toastWrap = null;
// toast('복사됨', {icon: 'check', kind: 'ok'|'error'|'claude', action: {label, onClick, keep}, timeout: 3200})
export function toast(msg, {action, kind = '', icon: ic, timeout = 3200} = {}) {
  toastWrap ||= document.body.appendChild(h('div.toast-wrap', {role: 'status', 'aria-live': 'polite'}));
  if (!toastWrap.isConnected) document.body.append(toastWrap);
  for (const old of toastWrap.children) old.remove();
  const text = h('span.msg', {}, msg);
  const el = h('div.toast', {class: kind}, ic || kind === 'error' ? icon(ic || 'alert', 16) : null, text);
  if (action) el.append(h('button.btn', {type: 'button', onclick: () => { action.onClick?.(); if (!action.keep) close(); }}, action.label));
  toastWrap.append(el);
  let t = 0;
  const arm = ms => { clearTimeout(t); if (timeout) t = setTimeout(close, ms); };
  el.addEventListener('pointerenter', () => clearTimeout(t));
  el.addEventListener('pointerleave', () => arm(1600));
  arm(timeout);
  function close() {
    clearTimeout(t);
    if (!el.isConnected) return;
    el.classList.add('out');
    setTimeout(() => el.remove(), 150);
  }
  return {el, close, set: m => { text.textContent = m; }, action: el.querySelector('.btn')};
}

// ---------- 시트 (모달) ----------
export const sheetOpen = () => !!document.querySelector('.sheet-backdrop');
// sheet({title, icon, body, foot, width: 480, onClose}) → {el, body, foot, close}
export function sheet({title = '', icon: ic, body, foot, width = 480, onClose, cls = ''} = {}) {
  closePopover();
  const prev = document.activeElement;
  const bodyEl = h('div.sheet-body', {}, body);
  const footEl = foot ? h('div.sheet-foot', {}, foot) : null;
  const box = h('div.sheet', {role: 'dialog', 'aria-modal': 'true', 'aria-label': title, class: cls, style: {width: `min(${width}px, calc(100vw - 32px))`}},
    h('div.sheet-head', {}, ic ? icon(ic, 18) : null, h('span.title', {}, title), iconBtn('x', '닫기', () => close(), {key: 'esc'})),
    bodyEl, footEl);
  const bd = h('div.sheet-backdrop', {}, box);
  bd.addEventListener('pointerdown', e => { if (e.target === bd) close(); });
  const onKey = e => {
    if (!bd.isConnected || popoverOpen()) return;
    if (e.key === 'Escape' && !e.isComposing) { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (e.key === 'Tab') {
      const f = $$('button:not(:disabled),input:not(:disabled),textarea,select,[tabindex]:not([tabindex="-1"])', box).filter(x => x.offsetParent);
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    }
  };
  document.addEventListener('keydown', onKey, true);
  document.body.append(bd);
  // 테두리는 키보드로 열었을 때만 보여요 (마우스로 열면 닫기 버튼에 시안 테두리가 뜨지 않게)
  (box.querySelector('[autofocus]') || bodyEl.querySelector('input,textarea,select,button') || box.querySelector('.sheet-foot .primary') || box.querySelector('button'))
    ?.focus({preventScroll: true, focusVisible: prev instanceof Element && prev.matches(':focus-visible')});
  function close() {
    if (!bd.isConnected) return;
    bd.remove();
    document.removeEventListener('keydown', onKey, true);
    onClose?.();
    if (prev instanceof HTMLElement && prev.isConnected) prev.focus({preventScroll: true});
  }
  return {el: box, body: bodyEl, foot: footEl, close};
}

// ---------- 입력 도구 ----------
// sliderRow({label, min, max, step, value, format: v => `${v}s`, onInput, onChange}) → 요소 (.set(v), .input)
export function sliderRow({label, min = 0, max = 1, step = 0.01, value = 0, format = v => v, onInput, onChange} = {}) {
  const out = h('output', {}, format(+value));
  const r = h('input.range', {type: 'range', min, max, step, value, 'aria-label': label});
  const paint = () => {
    r.style.setProperty('--p', `${((+r.value - min) / (max - min || 1)) * 100}%`);
    out.textContent = format(+r.value);
  };
  r.addEventListener('input', () => { paint(); onInput?.(+r.value); });
  r.addEventListener('change', () => onChange?.(+r.value));
  paint();
  const row = h('div.slider-row', {}, h('label', {}, label), out, r);
  row.set = v => { r.value = v; paint(); };
  row.get = () => +r.value;
  row.input = r;
  return row;
}

// switchEl({checked, onChange, label}) → button[role=switch] (.set(v), .get())
export function switchEl({checked = false, onChange, label, disabled} = {}) {
  const b = h('button.switch', {type: 'button', role: 'switch', 'aria-checked': String(!!checked), 'aria-label': label, disabled});
  b.addEventListener('click', () => { const v = !b.get(); b.set(v); onChange?.(v); });
  b.set = v => b.setAttribute('aria-checked', String(!!v));
  b.get = () => b.getAttribute('aria-checked') === 'true';
  return b;
}
export function switchRow(label, opts = {}) {
  const sw = switchEl({...opts, label});
  const row = h('label.switch-row', {}, h('span', {}, label), sw);
  row.switch = sw;
  return row;
}

// segmented([{value, label, icon, cls, tip}], 값, onChange) → .seg (.set(v), .get())
export function segmented(options, value, onChange, {cls = '', label} = {}) {
  return pressGroup('div.seg', options, value, onChange, cls, label, null);
}
// chips([{value, label, n, dot, icon}], 값, onChange) → .chips
export function chips(options, value, onChange, {cls = '', label} = {}) {
  return pressGroup('div.chips', options, value, onChange, cls, label, 'chip');
}
function pressGroup(sel, options, value, onChange, cls, label, itemCls) {
  const el = h(sel, {class: cls, role: 'group', 'aria-label': label});
  const btns = options.map(o => {
    const b = h('button', {type: 'button', class: [itemCls, o.cls].filter(Boolean).join(' '), 'aria-pressed': String(o.value === value),
      onclick: () => set(o.value, true)},
      o.dot ? h('span.dot', {style: {'--dot': o.dot}}) : null, o.icon ? icon(o.icon, 16) : null,
      o.label != null ? h('span', {}, o.label) : null, o.n != null ? h('span.n', {}, o.n) : null);
    if (o.tip) { b.dataset.tip = o.tip; if (o.key) b.dataset.key = o.key; }
    if (!o.label && o.tip) b.setAttribute('aria-label', o.tip);
    return b;
  });
  el.append(...btns);
  el.addEventListener('keydown', e => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault(); e.stopPropagation();
    const i = btns.indexOf(document.activeElement), j = clamp(i + (e.key === 'ArrowRight' ? 1 : -1), 0, btns.length - 1);
    btns[j].focus();
    set(options[j].value, true);
  });
  function set(v, fire) {
    value = v;
    btns.forEach((b, i) => b.setAttribute('aria-pressed', String(options[i].value === v)));
    if (fire) onChange?.(v);
  }
  el.set = v => set(v, false);
  el.get = () => value;
  el.buttons = btns;
  return el;
}

// 진행 표시: ring(0.4) 원형, bar(0.4) 막대 — 둘 다 .set(p) (p<0 이면 계속 도는 모양)
export function ring(p = 0, size = 20) {
  const el = h('span.ring', {style: {width: `${size}px`, height: `${size}px`}});
  el.set = v => { el.classList.toggle('spin', v < 0); el.style.setProperty('--p', clamp(v, 0, 1)); };
  el.set(p);
  return el;
}
export function bar(p = 0, cls = '') {
  const el = h('div.progress', {class: cls, role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100}, h('i'));
  el.set = v => { el.style.setProperty('--p', clamp(v, 0, 1)); el.setAttribute('aria-valuenow', Math.round(clamp(v, 0, 1) * 100)); };
  el.set(p);
  return el;
}

// 제자리 이름 바꾸기: 요소를 잠깐 입력칸으로 바꿔요. Enter 저장 · Esc 취소 · 바깥 클릭 저장.
export function inlineEdit(target, {value, onCommit, onCancel, cls = '', placeholder = ''} = {}) {
  const inp = h('input.input.inline-input', {value: value ?? target.textContent, class: cls, placeholder});
  const old = value ?? target.textContent;
  inp.spellcheck = false;
  target.hidden = true;
  target.after(inp);
  inp.focus();
  inp.select();
  let done = false;
  const finish = ok => {
    if (done) return;
    done = true;
    const v = inp.value.trim();
    inp.remove();
    target.hidden = false;
    if (ok && v && v !== old) onCommit?.(v); else onCancel?.();
  };
  inp.addEventListener('keydown', e => {
    e.stopPropagation();
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  inp.addEventListener('blur', () => finish(true));
  return inp;
}

// 파일 끌어놓기 덮개: fileDrop(window, {label, icon, onDrop: files => ...}) → 해제 함수
export function fileDrop(target, {label = '놓아서 추가', icon: ic = 'plus-circle', onDrop} = {}) {
  const ov = h('div.dropzone', {}, h('div.box', {}, icon(ic, 30), h('span', {}, label)));
  document.body.append(ov);
  let depth = 0;
  const has = e => [...(e.dataTransfer?.types || [])].includes('Files');
  const enter = e => { if (!has(e)) return; e.preventDefault(); depth++; ov.classList.add('on'); };
  const over = e => { if (!has(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; };
  const leave = e => { if (!has(e)) return; if (--depth <= 0) { depth = 0; ov.classList.remove('on'); } };
  const drop = e => { if (!has(e)) return; e.preventDefault(); depth = 0; ov.classList.remove('on'); onDrop?.([...e.dataTransfer.files]); };
  target.addEventListener('dragenter', enter);
  target.addEventListener('dragover', over);
  target.addEventListener('dragleave', leave);
  target.addEventListener('drop', drop);
  return () => {
    target.removeEventListener('dragenter', enter); target.removeEventListener('dragover', over);
    target.removeEventListener('dragleave', leave); target.removeEventListener('drop', drop);
    ov.remove();
  };
}
// /api/resolve 에 보낼 파일 정보
export const fileInfo = files => files.map(f => ({name: f.name, size: f.size, mtime: f.lastModified / 1000}));
export const VIDEO_EXT = /\.(mp4|mov|m4v|mkv|avi|mts|m2ts|webm|3gp)$/i;
export const SCRIPT_EXT = /\.(srt|vtt|txt)$/i;

// ---------- 그 밖 ----------
export function debounce(fn, ms) {
  let t = 0, args = null;
  const f = (...a) => { args = a; clearTimeout(t); t = setTimeout(() => { t = 0; fn(...args); }, ms); };
  f.flush = () => { if (t) { clearTimeout(t); t = 0; fn(...args); } };
  f.cancel = () => { clearTimeout(t); t = 0; };
  return f;
}
export function throttle(fn, ms) {
  let last = 0, t = 0, args = null;
  return (...a) => {
    args = a;
    const wait = ms - (Date.now() - last);
    if (wait <= 0) { last = Date.now(); fn(...args); }
    else if (!t) t = setTimeout(() => { t = 0; last = Date.now(); fn(...args); }, wait);
  };
}
export async function copyText(s) {
  try { await navigator.clipboard.writeText(s); return true; }
  catch {
    const ta = h('textarea', {value: s, style: 'position:fixed;opacity:0;pointer-events:none'});
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  }
}
// 입력 중인지 (단축키를 무시할 때)
export function isTyping(el = document.activeElement) {
  if (!(el instanceof HTMLElement)) return false;
  if (el.isContentEditable) return true;
  if (el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true;
  return el.tagName === 'INPUT' && !/^(button|checkbox|radio|range|color|file|submit|reset)$/i.test(el.type);
}
// Claude 가 고친 항목 반짝임: glow(el, store, id) — 남은 시간만큼 이어서 보여요
export function glow(el, store, id) {
  const left = store.glowLeft?.(id) || 0;
  el.classList.toggle('glow', left > 0);
  el.style.animationDelay = left > 0 ? `${left - 8000}ms` : '';
}

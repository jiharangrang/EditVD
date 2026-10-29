// 캔버스 그리기: 눈금자 · 필름스트립 · 파형. 보이는 범위(뷰포트 폭)만 그려요.
// o = {W, H, X0, pps, D, dpr}: X0 = 왼쪽 끝의 내용 좌표(px), 시간 t 의 화면 x = round(t*pps) - X0
import { fmt } from '../time.js';
import { peakMax } from './assets.js';

const STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
const FONT = '-apple-system,BlinkMacSystemFont,"SF Pro Text","Apple SD Gothic Neo",sans-serif';
const MONO = '"SF Mono",ui-monospace,Menlo,monospace';
export const COLORS = {clip: '#0F5A63', clipLine: '#1FA4B3', wave: '#56C8D4', waveBg: '#0D3439', waveLine: '#16707A', busy: '#26262B'};

// 눈금 간격: 큰 눈금은 글자가 겹치지 않게 (≥ 84px), 작은 눈금은 7px 이상
export function rulerStep(pps, minPx = 84) {
  for (const s of STEPS) if (s * pps >= minPx) return s;
  return STEPS[STEPS.length - 1];
}

function prep(ctx, o) {
  ctx.setTransform(o.dpr, 0, 0, o.dpr, 0, 0);
  ctx.clearRect(0, 0, o.W, o.H);
}

export function drawRuler(ctx, o) {
  prep(ctx, o);
  const {W, H, X0, pps, D} = o;
  const endX = Math.round(D * pps) - X0;
  if (endX < W) { ctx.fillStyle = 'rgba(0,0,0,.22)'; ctx.fillRect(Math.max(0, endX), 0, W, H); }
  const major = rulerStep(pps);
  let n = 1;
  for (const k of [10, 5, 2]) if ((major / k) * pps >= 7) { n = k; break; }
  const minor = major / n;
  const i0 = Math.max(0, Math.floor(X0 / pps / minor)), t1 = (X0 + W) / pps;
  const labels = [];
  ctx.beginPath();
  for (let i = i0; i * minor <= t1 + minor; i++) {
    const x = Math.round(i * minor * pps) - X0 + 0.5, big = i % n === 0;
    ctx.moveTo(x, big ? H - 9 : H - 4);
    ctx.lineTo(x, H);
    if (big) labels.push([x, i * minor]);
  }
  ctx.strokeStyle = '#4A4A54';
  ctx.lineWidth = 1;
  ctx.stroke();
  ctx.font = `11px ${MONO}`;
  ctx.textBaseline = 'top';
  ctx.fillStyle = '#7C7C86';
  for (const [x, t] of labels) ctx.fillText(fmt(t, major < 1 ? 1 : 0), x + 4, 4);
}

function rrect(ctx, x, y, w, h, r) {
  r = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.roundRect ? ctx.roundRect(x, y, w, h, r) : ctx.rect(x, y, w, h);
}

// 클립 화면 범위 (양쪽 1px 안쪽 → 이웃 클립과 2px 틈)
function span(o, c) {
  const x0 = Math.round(c.a * o.pps) - o.X0 + 1, x1 = Math.round(c.b * o.pps) - o.X0 - 1;
  return [x0, Math.max(x0 + 1, x1)];
}

function chip(ctx, text, x, y, maxW) {
  ctx.font = `500 11px ${FONT}`;
  let w = ctx.measureText(text).width;
  if (w + 10 > maxW) return;
  ctx.fillStyle = 'rgba(8,10,12,.62)';
  rrect(ctx, x, y, w + 10, 17, 4);
  ctx.fill();
  ctx.fillStyle = '#F2F2F5';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, x + 5, y + 9);
}

// clips: [{sid, a, b, o, label?}] (a,b 축 시간, o = a 의 원본 시간) · src(sid) → {offset, thumbs, busy}
export function drawFilm(ctx, o, clips, src) {
  prep(ctx, o);
  const {W, H, pps} = o;
  for (const c of clips) {
    const [x0, x1] = span(o, c);
    if (x1 <= 0 || x0 >= W) continue;
    const w = x1 - x0, s = src(c.sid), th = s?.thumbs, ready = th?.state === 'ready';
    rrect(ctx, x0, 0, w, H, 4);
    ctx.fillStyle = s?.busy ? COLORS.busy : COLORS.clip;
    ctx.fill();
    if (ready && w > 2) {
      const m = th.meta, tw = H * m.w / m.h, end = Math.min(x1, W), lim = c.o + (c.b - c.a);
      ctx.save();
      ctx.clip();
      for (let k = Math.max(0, Math.floor(-x0 / tw)); x0 + k * tw < end; k++) {
        const xl = x0 + k * tw;
        const t = Math.min(lim, c.o + (xl + tw / 2 - x0) / pps) - s.offset;
        const i = Math.min(m.count - 1, Math.max(0, Math.floor(t / m.interval)));
        ctx.drawImage(th.img, (i % m.cols) * m.w, Math.floor(i / m.cols) * m.h, m.w, m.h, Math.round(xl), 0, Math.ceil(tw) + 1, H);
      }
      ctx.restore();
    }
    if (w > 3) {
      rrect(ctx, x0 + 0.5, 0.5, w - 1, H - 1, 4);
      ctx.strokeStyle = s?.busy ? '#3A3A42' : COLORS.clipLine;
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    const lx = Math.max(x0, 0) + 4;
    if (s?.busy) chip(ctx, s.busy, lx, 4, x1 - lx - 4);
    else if (c.label) chip(ctx, c.label, lx, 4, x1 - lx - 4);
  }
}

export function drawWave(ctx, o, clips, src) {
  prep(ctx, o);
  const {W, H, X0, pps} = o, cy = H / 2, amp = H / 2 - 4;
  for (const c of clips) {
    const [x0, x1] = span(o, c);
    if (x1 <= 0 || x0 >= W) continue;
    const w = x1 - x0, s = src(c.sid), pk = s?.peaks;
    rrect(ctx, x0, 0, w, H, 4);
    ctx.fillStyle = s?.busy ? COLORS.busy : COLORS.waveBg;
    ctx.fill();
    if (pk?.state === 'ready' && !s.busy) {
      const per = pk.perSec, step = per / pps, base = c.o - c.a - s.offset;
      ctx.beginPath();
      for (let x = Math.max(x0, 0), e = Math.min(x1, W); x < e; x++) {
        const s0 = ((X0 + x) / pps + base) * per;
        const v = peakMax(pk, s0, s0 + step), hh = Math.max(0.5, (v / 255) * amp);
        ctx.rect(x, cy - hh, 1, hh * 2);
      }
      ctx.fillStyle = COLORS.wave;
      ctx.fill();
    } else {
      ctx.fillStyle = COLORS.waveLine;
      ctx.fillRect(Math.max(x0, 0), cy, Math.min(x1, W) - Math.max(x0, 0), 1);
    }
  }
}


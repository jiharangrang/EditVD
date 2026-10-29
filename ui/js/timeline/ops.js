// 타임라인 편집 연산 (순수 함수): 다듬기·잘라 내기·되살리기·분할. 시간은 모두 원본 축(초)이에요.
// 돌려주는 것은 store.edit / gesture.commit 에 넣을 ops 목록이에요.
export const EPS = 1e-3;
const r3 = x => Math.round(x * 1000) / 1000;
const put = item => ({op: 'put', coll: 'cuts', item});

export const userCut = (start, end) => put({start: r3(start), end: r3(end), type: 'user', enabled: true, reviewed: true, reason: ''});
export const toggleCut = c => [put({id: c.id, enabled: !c.enabled, reviewed: true})];
// 컷 지우기 = 그 부분을 살려요 (직접 만든 컷은 아예 지워요)
export const deleteCut = c => c.type === 'user' ? [{op: 'del', coll: 'cuts', id: c.id}] : [put({id: c.id, enabled: false, reviewed: true})];

// [a,b] 를 다시 살려요: 걸친 켜진 컷은 줄이고, 거의 다 덮이면 꺼요 (사용자 컷은 지워요)
export function restoreOps(cuts, a, b, minLen = 1 / 30) {
  const ops = [];
  for (const c of cuts) {
    if (!c.enabled || c.type === 'clip' || c.end <= a + EPS || c.start >= b - EPS) continue;
    const left = c.start < a ? a - c.start : 0, right = c.end > b ? c.end - b : 0;
    if (left < minLen && right < minLen) ops.push(...deleteCut(c));
    else if (left >= minLen) {
      ops.push(put({id: c.id, end: r3(a), reviewed: true}));
      if (right >= minLen) ops.push(userCut(b, c.end));   // 가운데만 살리면 오른쪽은 새 컷으로
    } else ops.push(put({id: c.id, start: r3(b), reviewed: true}));
  }
  return ops;
}

// 조각 끝 x 에 붙어 있는 켜진 컷 (key: 'end' 면 x 에서 끝나는 컷, 'start' 면 x 에서 시작하는 컷)
function adjacent(cuts, x, key) {
  let best = null;
  for (const c of cuts) {
    if (!c.enabled || c.type === 'clip' || Math.abs(c[key] - x) >= EPS) continue;
    if (!best || c.end - c.start > best.end - best.start) best = c;
  }
  return best;
}

// 조각(piece) 한쪽 끝을 원본 시간 t 로 옮겨요. side: 'start' | 'end'
// 안쪽으로 → 이웃 컷을 늘리거나 (이웃 컷이 없으면) 사용자 컷, 바깥쪽으로 → 잘린 부분을 되살려요.
export function trimOps(cuts, piece, side, t, minLen = 1 / 30) {
  if (side === 'start') {
    if (t > piece.start + EPS) {
      const c = adjacent(cuts, piece.start, 'end');
      return c ? [put({id: c.id, end: r3(t), reviewed: true})] : [userCut(piece.start, t)];
    }
    return t < piece.start - EPS ? restoreOps(cuts, t, piece.start, minLen) : [];
  }
  if (t < piece.end - EPS) {
    const c = adjacent(cuts, piece.end, 'start');
    return c ? [put({id: c.id, start: r3(t), reviewed: true})] : [userCut(t, piece.end)];
  }
  return t > piece.end + EPS ? restoreOps(cuts, piece.end, t, minLen) : [];
}

// 다듬기로 옮길 수 있는 범위 [min, max] (원본). 바깥쪽은 붙은 잘린 구간까지 (같은 영상 안), 안쪽은 1프레임 남기고.
// iv: 합친 잘린 구간 [[s,e]], span: 조각이 속한 영상 {start,end}
export function trimBounds(iv, span, piece, side, minLen = 1 / 30) {
  if (side === 'start') {
    const reg = iv.find(([, e]) => Math.abs(e - piece.start) < EPS);
    return [reg ? Math.max(reg[0], span?.start ?? 0) : piece.start, piece.end - minLen];
  }
  const reg = iv.find(([s]) => Math.abs(s - piece.end) < EPS);
  return [piece.start + minLen, reg ? Math.min(reg[1], span?.end ?? Infinity) : piece.end];
}

// 조각 지우기 → 그 구간을 사용자 컷으로 (경계에 남는 분할 표시는 같이 지워요)
export function deletePiece(splits, piece) {
  const ops = [userCut(piece.start, piece.end)];
  const keep = (splits || []).filter(t => t < piece.start - EPS || t > piece.end + EPS);
  if (keep.length !== (splits || []).length) ops.push({op: 'set', key: 'splits', value: keep});
  return ops;
}

// t 를 품은 조각 (경계에서 minLen 안쪽만)
export function pieceAt(pieces, t, minLen = 0) {
  for (const p of pieces) if (t >= p.start + minLen - 1e-9 && t <= p.end - minLen + 1e-9 && t < p.end) return p;
  return null;
}

// 영상 분할 표시 넣기. 조각 안쪽이 아니면 null
export function splitOps(splits, pieces, t, minLen = 1 / 30) {
  const p = pieceAt(pieces, t, minLen);
  if (!p || (splits || []).some(x => Math.abs(x - t) < minLen)) return null;
  return [{op: 'set', key: 'splits', value: [...(splits || []), r3(t)].sort((a, b) => a - b)}];
}

// Q: 조각 처음 ~ t, W: t ~ 조각 끝 → 사용자 컷. 못 하면 null
export function trimToOps(pieces, t, side, minLen = 1 / 30) {
  const p = pieceAt(pieces, t, minLen);
  if (!p) return null;
  return [side === 'start' ? userCut(p.start, t) : userCut(t, p.end)];
}

// 잘린 구간 [a,b] 를 대표하는 컷 (가장 많이 겹치는 켜진 컷) + 아직 안 본 컷이 있는지
export function regionCut(cuts, a, b) {
  let best = null, bestOv = 0, unrev = false;
  for (const c of cuts) {
    if (!c.enabled || c.end <= a + EPS || c.start >= b - EPS) continue;
    const ov = Math.min(b, c.end) - Math.max(a, c.start);
    if (ov > bestOv) { best = c; bestOv = ov; }
    if (!c.reviewed && c.type !== 'clip') unrev = true;
  }
  return {cut: best, unrev};
}

// 시간순으로 겹치지 않는 틈들의 대표 컷을 한꺼번에 찾아요. 같은 겹침 길이면 기존 목록의 앞 컷을 남겨요.
export function regionCuts(cuts, regions) {
  const out = regions.map(() => ({cut: null, unrev: false}));
  const best = new Float64Array(regions.length);
  for (const c of cuts) {
    if (!c.enabled) continue;
    let lo = 0, hi = regions.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (regions[m].b <= c.start + EPS) lo = m + 1; else hi = m; }
    for (let i = lo; i < regions.length && regions[i].a < c.end - EPS; i++) {
      const {a, b} = regions[i];
      // 컷과 틈이 겹치는 길이를 구해요: $$\ell=\min(b,c_e)-\max(a,c_s)$$
      const overlap = Math.min(b, c.end) - Math.max(a, c.start);
      if (overlap > best[i]) { best[i] = overlap; out[i].cut = c; }
      if (!c.reviewed && c.type !== 'clip') out[i].unrev = true;
    }
  }
  return out;
}

// 시간 경계가 바뀔 때만 모델을 다시 만들고 글·색·메모는 보이는 항목만 갱신해요.
export function modelChange(event) {
  if (!event.colls || !event.ops) return 'model';
  let result = 'none';
  for (const op of event.ops) {
    if (op.op === 'set') {
      if (['sources', 'canvas', 'splits'].includes(op.key)) return 'model';
      if (op.key === 'title' || op.key === 'notes' || op.key.startsWith('settings.')) continue;
      return 'model';
    }
    if (op.coll === 'cuts' || op.coll === 'sources') return 'model';
    if (op.coll === 'subtitles') {
      if (op.op !== 'put' || 'start' in op.item || 'end' in op.item) return 'model';
    } else if (!['comments', 'speakers'].includes(op.coll)) return 'model';
    result = 'items';
  }
  return result;
}

// ---------- 자체 점검 ----------
// node -e "import('./ui/js/timeline/ops.js').then(m => console.log(m.selfTest()))"
export function selfTest() {
  const same = (a, b, msg) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${msg}: ${JSON.stringify(a)} != ${JSON.stringify(b)}`); };
  const cuts = [
    {id: 'a', start: 2, end: 4, type: 'silence', enabled: true, reviewed: false},
    {id: 'b', start: 3.5, end: 5, type: 'ng', enabled: true, reviewed: true},
    {id: 'c', start: 8, end: 9, type: 'user', enabled: true, reviewed: true},
    {id: 'd', start: 11, end: 12, type: 'gap', enabled: false, reviewed: false},
  ];
  const P = {start: 5, end: 8, sid: 's1'};
  // 안쪽으로 다듬기: 붙은 컷을 늘려요
  same(trimOps(cuts, P, 'start', 5.5), [put({id: 'b', end: 5.5, reviewed: true})], 'trim in start');
  same(trimOps(cuts, P, 'end', 7.25), [put({id: 'c', start: 7.25, reviewed: true})], 'trim in end');
  // 붙은 컷이 없으면 사용자 컷
  same(trimOps(cuts, {start: 9, end: 15}, 'end', 14), [userCut(14, 15)], 'trim in no neighbour');
  same(trimOps(cuts, {start: 0, end: 2}, 'start', 0.5), [userCut(0, 0.5)], 'trim at 0');
  // 바깥으로: 되살리기 (줄이기 · 끄기 · 사용자 컷 지우기)
  same(trimOps(cuts, P, 'start', 4.2), [put({id: 'b', end: 4.2, reviewed: true})], 'extend start shrink');
  same(trimOps(cuts, P, 'start', 3), [put({id: 'a', end: 3, reviewed: true}), put({id: 'b', enabled: false, reviewed: true})], 'extend start over two');
  same(trimOps(cuts, P, 'end', 9), [{op: 'del', coll: 'cuts', id: 'c'}], 'extend end deletes user cut');
  same(trimOps(cuts, P, 'end', 8.5), [put({id: 'c', start: 8.5, reviewed: true})], 'extend end shrink');
  same(trimOps(cuts, P, 'start', 5), [], 'no-op');
  // 가운데만 살리기
  same(restoreOps([{id: 'x', start: 0, end: 10, type: 'long', enabled: true}], 4, 6), [put({id: 'x', end: 4, reviewed: true}), userCut(6, 10)], 'restore middle');
  // 범위
  const iv = [[2, 5], [8, 9]];
  same(trimBounds(iv, {start: 0, end: 10}, P, 'start', 0.1), [2, 7.9], 'bounds start');
  same(trimBounds(iv, {start: 0, end: 10}, P, 'end', 0.1), [5.1, 9], 'bounds end');
  same(trimBounds(iv, {start: 0, end: 8.5}, P, 'end', 0.1), [5.1, 8.5], 'bounds end clipped to source');
  same(trimBounds(iv, {start: 0, end: 10}, {start: 9, end: 10}, 'end', 0.1), [9.1, 10], 'bounds no region');
  // 조각 지우기 · 분할 · Q/W
  same(deletePiece([5, 6, 8, 12], P), [userCut(5, 8), {op: 'set', key: 'splits', value: [12]}], 'delete piece');
  const pieces = [{start: 0, end: 2}, {start: 5, end: 8}];
  same(splitOps([6], pieces, 7), [{op: 'set', key: 'splits', value: [6, 7]}], 'split');
  same(splitOps([6], pieces, 6), null, 'split dup');
  same(splitOps([], pieces, 3), null, 'split in cut');
  same(trimToOps(pieces, 6, 'start'), [userCut(5, 6)], 'Q');
  same(trimToOps(pieces, 6, 'end'), [userCut(6, 8)], 'W');
  same(trimToOps(pieces, 3, 'end'), null, 'W in cut');
  same(regionCut(cuts, 2, 5).cut.id, 'a', 'region cut');
  same(regionCut(cuts, 2, 5).unrev, true, 'region unrev');
  same(deleteCut(cuts[0]), [put({id: 'a', enabled: false, reviewed: true})], 'delete auto cut');
  return true;
}

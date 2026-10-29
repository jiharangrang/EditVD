// 타임라인의 부분 갱신·편집 경계 검색·서버 응답·되돌리기를 확인해요.
import test from 'node:test';
import assert from 'node:assert/strict';
import {Store, changeOp} from '../ui/js/store.js';
import {regionCut, regionCuts, modelChange, selfTest as checkOps} from '../ui/js/timeline/ops.js';
import {selfTest as checkTime} from '../ui/js/time.js';

test('기존 시간 변환과 편집 연산 자체 검사를 통과해요', () => {
  assert.equal(checkTime(), true);
  assert.equal(checkOps(), true);
});

test('틈을 한꺼번에 검색해도 겹친 컷·동률·확인 표시가 같아요', () => {
  const regions = Array.from({length: 100}, (_, i) => ({a: i * 3, b: i * 3 + 2}));
  const cuts = Array.from({length: 300}, (_, i) => ({id: `c${i}`, start: (i * 17 % 300) - 1,
    end: (i * 17 % 300) + (i % 9) + 0.001, enabled: i % 5 !== 0, reviewed: i % 3 === 0, type: i % 7 ? 'silence' : 'clip'}));
  cuts.push({id: 'tie1', start: 0, end: 2, enabled: true}, {id: 'tie2', start: 0, end: 2, enabled: true});
  assert.deepEqual(regionCuts(cuts, regions), regions.map(({a, b}) => regionCut(cuts, a, b)));
  assert.deepEqual(regionCuts(cuts, []), []);
  assert.deepEqual(regionCuts([], regions), regions.map(() => ({cut: null, unrev: false})));
});

test('컷이 많아도 모든 틈에서 전체 컷을 반복 검색하지 않아요', () => {
  let reads = 0;
  const regions = Array.from({length: 3000}, (_, i) => ({a: i * 3, b: i * 3 + 1}));
  const cuts = regions.map((r, i) => ({id: `c${i}`, get enabled() { reads++; return true; }, start: r.a, end: r.b}));
  assert.equal(regionCuts(cuts, regions).filter(r => r.cut).length, 3000);
  assert.ok(reads <= 3000);
});

test('시간 변경은 전체 모델을, 표시 변경은 항목만 갱신해요', () => {
  const event = op => ({colls: new Set([op.coll || op.key]), ops: [op]});
  for (const item of [{id: 't1', text: '글'}, {id: 't1', pos: {x: 20, y: 80}}, {id: 't1', locked: true}]) {
    assert.equal(modelChange(event({op: 'put', coll: 'subtitles', item})), 'items');
  }
  for (const item of [{id: 't1', start: 1}, {id: 't1', end: 2}]) {
    assert.equal(modelChange(event({op: 'put', coll: 'subtitles', item})), 'model');
  }
  assert.equal(modelChange(event({op: 'del', coll: 'subtitles', id: 't1'})), 'model');
  assert.equal(modelChange(event({op: 'put', coll: 'comments', item: {id: 'm1', t: 1}})), 'items');
  assert.equal(modelChange(event({op: 'set', key: 'notes', value: '메모'})), 'none');
  assert.equal(modelChange({colls: null}), 'model');
});

test('서버의 전체 자막 응답에서도 실제 변경 필드와 객체 참조를 보존해요', () => {
  const store = new Store();
  const original = {id: 't1', start: 1, end: 3, text: '원래 글', locked: false, rev: 1, by: 'ui'};
  store.project = {subtitles: [original], cuts: [], sources: [], speakers: [], comments: []};
  const events = [];
  store.on('project', e => events.push(e));
  store._applyServer({rev: 2, changes: [{coll: 'subtitles', item: {...original, text: '새 글', rev: 2}}]});
  assert.equal(store.project.subtitles[0], original);
  assert.deepEqual(events[0].ops, [{op: 'put', coll: 'subtitles', item: {id: 't1', text: '새 글'}}]);
  assert.equal(modelChange(events[0]), 'items');
  store._applyServer({rev: 3, changes: [{coll: 'subtitles', item: {...original, end: 4, rev: 3}}]});
  assert.equal(modelChange(events[1]), 'model');
  assert.equal(changeOp(store.project, {coll: 'subtitles', item: {...original, rev: 99}}), null);
});

test('부분 갱신으로 자막을 고친 뒤에도 되돌리기와 다시 하기가 유지돼요', () => {
  const store = new Store();
  store._send = () => {};
  store.project = {subtitles: [{id: 't1', start: 1, end: 3, text: '원래 글', locked: false}],
    cuts: [], sources: [], speakers: [], comments: []};
  store.edit([{op: 'put', coll: 'subtitles', item: {id: 't1', text: '새 글'}}], '글 고치기');
  assert.equal(store.project.subtitles[0].text, '새 글');
  store.undo();
  assert.equal(store.project.subtitles[0].text, '원래 글');
  store.redo();
  assert.equal(store.project.subtitles[0].text, '새 글');
  assert.deepEqual([store.project.subtitles[0].start, store.project.subtitles[0].end], [1, 3]);
});

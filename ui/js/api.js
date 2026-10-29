// 서버 API v2 호출. 실패하면 서버가 보낸 한국어 메시지로 Error 를 던져요 (err.status, err.data).
const enc = encodeURIComponent;
const P = n => `/api/p/${enc(n)}`;

export async function req(method, url, body, opts = {}) {
  let r;
  try {
    r = await fetch(url, {
      method, cache: 'no-store', keepalive: !!opts.keepalive,
      headers: body === undefined ? undefined : {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    const e = new Error('서버에 연결할 수 없어요');
    e.status = 0;
    throw e;
  }
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* JSON 이 아니면 아래에서 처리 */ }
  if (!r.ok || (data && !Array.isArray(data) && typeof data === 'object' && data.error)) {
    const e = new Error((data && data.error) || `요청이 실패했어요 (${r.status})`);
    e.status = r.status;
    e.data = data;
    throw e;
  }
  return data;
}
const get = url => req('GET', url);
const post = (url, body = {}, opts) => req('POST', url, body, opts);

export const api = {
  projects: () => get('/api/projects'),                                     // [{name,title,updated,duration,edited,sources,cover,legacy?}]
  create: (paths, title) => post('/api/projects', title ? {title, paths} : {paths}),   // {name, job}
  pick: (kind = 'video') => post('/api/pick', {kind}).then(d => d?.paths || []),     // 취소 → []
  resolve: files => post('/api/resolve', {files}).then(d => d?.paths || []),
  project: n => get(P(n)),                                                  // {project, words_count, assets}
  rev: n => get(`${P(n)}/rev`),                                             // {rev, by, jobs}
  words: (n, from, to) => get(`${P(n)}/words?${new URLSearchParams({from: from ?? 0, to: to ?? 1e9})}`),
  ops: (n, rev, ops, opts) => post(`${P(n)}/ops`, {rev, ops}, opts),        // {rev, changes}
  sources: (n, body) => post(`${P(n)}/sources`, body),                      // {rev, job?}
  addSources: (n, paths) => post(`${P(n)}/sources`, {add: paths}),
  orderSources: (n, order) => post(`${P(n)}/sources`, {order}),
  removeSource: (n, sid) => post(`${P(n)}/sources`, {remove: sid}),
  detectCuts: (n, settings) => post(`${P(n)}/cuts/detect`, settings ? {settings} : {}),   // {rev, changes}
  generateSubs: n => post(`${P(n)}/subs/generate`, {}),                     // {rev, changes} | {job}
  importSubs: (n, text, opts = {}) => post(`${P(n)}/subs/import`, {text, ...opts}),     // {rev, changes, info}
  splitSub: (n, id, t) => post(`${P(n)}/subs/split`, {id, t}),
  mergeSubs: (n, ids) => post(`${P(n)}/subs/merge`, {ids}),
  exportProject: (n, opts) => post(`${P(n)}/export`, opts),                 // {job}  opts 는 그대로 보내요: {kind:'capcut'|'mp4'|'srt', name?, burn?, include_subs?, copy_media?, target?}
  job: id => get(`/api/jobs/${enc(id)}`),                                   // {id, project, kind, state, progress, message, result}
  reveal: path => post('/api/open', {path}),                                // Finder 에서 보기
  openApp: (app = 'CapCut') => post('/api/open', {app}),
  rename: async (n, title) => { const {rev} = await get(`${P(n)}/rev`); return post(`${P(n)}/ops`, {rev, ops: [{op: 'set', key: 'title', value: title}]}); },
};

export const media = {
  preview: n => `/media/${enc(n)}/preview.mp4`,
  source: (n, sid, file) => `/media/${enc(n)}/sources/${enc(sid)}/${file}`,
  exported: (n, dir, file) => `/media/${enc(n)}/exports/${enc(dir)}/${enc(file)}`,
};

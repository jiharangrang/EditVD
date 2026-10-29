// 시작점: #p=<이름> 이면 편집 화면, 아니면 홈. 화면이 바뀌면 페이지를 새로 불러와요 (모듈 정리가 깔끔해요).
import { renderHome } from './home.js';
import { renderEditor } from './shell.js';
import { store } from './store.js';

const route = () => { const m = /(?:^#|&)p=([^&]+)/.exec(location.hash); return m ? decodeURIComponent(m[1]) : null; };
const first = route();

addEventListener('hashchange', async () => {
  if (route() === first) return;
  await Promise.race([store.flush(), new Promise(r => setTimeout(r, 1500))]);
  location.reload();
});

const root = document.getElementById('app');
if (first) renderEditor(root, first); else renderHome(root);

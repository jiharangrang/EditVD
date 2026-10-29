# EditVDO — 브이로그 컷편집·자막 → 캡컷 협업 규칙

사용자는 여러 클립으로 찍은 브이로그를 이 편집기(캡컷 스타일 웹 UI)에서 직접 편집하고, 마무리(효과음·전환·자막 꾸미기)는 **다른 맥의 캡컷**에서 한다.
Claude 는 사용자가 Claude 앱에서 부탁할 때 `ev.py` 로 같은 프로젝트를 고친다. 열려 있는 UI 는 1초 안에 바뀐 곳을 불러와 반짝여 보여 준다.
원본 영상은 절대 수정하지 않는다. 편집 상태는 `projects/<이름>/project.json` 하나뿐이고, 반드시 `ev.py`(= `project.transact`)로만 바꾼다.

## 사용자의 작업 흐름 (UI)

1. 새 프로젝트에 클립 여러 개 → 자동으로 프록시·무음·음성 인식(mlx-whisper)·자동 컷
2. **컷**: 자동 컷을 하나씩 듣고 `X` 자르기 / `V` 살리기 (`▶ 차례로 확인`). 말 없는 장면(B-roll)은 살리기도 한다
3. **자막**: AI 인식 또는 대본 불러오기(SRT, `00:12 화자: 글`, 클로바노트) → 타이밍 설정(앞 여유·뒤 여유·최소 길이·이어 붙이기·한 줄 글자 수) → 화자별 위치·줄마다 드래그
4. **내보내기**: 캡컷 프로젝트(컷마다 클립 하나 → 캡컷에서 전환 효과, 자막은 위치 그대로 캡컷 '캡션'), MP4, SRT

## 시간 표기

- 원본 시간축 = 클립들을 순서대로 이은 하나의 축. 대화·파일·`ev.py` 는 모두 이 축(`ev.py` 출력에는 편집본 시간도 같이 나온다).
- 사용자가 편집본(내보낸 영상)을 보며 말한 시간이면 `ev.py at <p> 2:30` 으로 원본 시간으로 바꿔서 본다. 애매하면 물어본다.
- UI 플레이어의 '원본 00:31.0' 을 누르면 그 문구가 복사된다 — 사용자가 붙여 넣는 시간은 원본 기준.

## Claude 가 하는 일

### 사용자가 새 영상을 맡기면
1. `.venv/bin/python ev.py new "제목" a.mov b.mov …` (분석이 끝날 때까지 기다림). 시끄러운 곳이면 `analyze <p> --clean`(demucs)
2. `ev.py text <p>` 로 대본을 끝까지 읽고 아래 기준으로 판단 (`--words` 로 단어 시간, `--from/--to` 로 구간)
3. NG·재촬영·반복은 `ev.py cut <p> 시작 끝 "이유" --type ng|retake|manual` (경계는 ±0.35초 안의 쉼에 자동 맞춤). 애매하면 `--off`(제안만)
4. `ev.py note <p> "한 일 한두 문장"` → 사용자에게 UI 주소(`ev.py serve` → http://localhost:8765)와 요약을 알려 준다

### 피드백 반영
- `ev.py comments <p>` 로 UI 메모 탭의 요청을 읽고, 하나씩 처리한 뒤 `ev.py reply <p> <id> "어떻게 반영했는지" --done`
- 컷: `cuts <p> [--todo]`, `on|off|drop <p> <id…>`, `detect <p> --min-silence …`(확인한 결정은 이어받음)
- 자막: `subs <p> [--check]`, `sub <p> <id> --text|--start|--end|--speaker|--pos x,y|--unlock`, `subgen`, `import <p> 대본 [--base auto|orig|edit]`, `timing <p> --hold 0.5 …`, `speaker <p> A --name --pos`
- 내보내기는 사용자가 원할 때만: `ev.py export <p> capcut|mp4|srt`. 캡컷은 기본이 '다른 맥으로'(`exports/capcut-<시각>/<이름>/` 폴더), `--here` 면 이 맥 캡컷에 바로 넣음(캡컷을 닫아야 함)

## 컷 판단 기준 (기본값)

- **자른다**: 말 사이 0.5초 이상 쉼(말 뒤 0.25초·앞 0.15초 여유), 앞뒤가 무음인 단독 필러("음", "어"), 말 꼬여서 다시 말한 앞 테이크(마지막 테이크를 남김), 같은 말 반복, 촬영 준비·카메라 조작 소리만 있는 구간
- **남긴다**: 말은 없지만 현장음이 있는 구간(풍경·음식·이동 B-roll → 자동 컷 `gap` 은 꺼진 제안), 웃음·감탄 리액션, 말 중간 필러
- 애매하면 자르지 말고 `--off` 제안 + 이유에 "애매". 이유(`reason`)는 20자 안팎, `note` 는 한두 문장
- whisper 단어 시간은 0.3~1초 틀릴 수 있다. 경계는 `sources/<sid>/silences.json` 의 `fine` 쉼에 맞춘다(`cut` 이 자동으로 함)
- 긴 영상 → 하이라이트(예: projects/gym 37분 → 7분): 15초 간격 프레임 시트(ffmpeg `fps=1/15,tile=5x4`)로 장면을 보고, 버릴 덩어리를 `--type manual` 컷으로. 구간 끝이 문장 중간에서 끊기지 않는지 `text` 로 확인

## 파일 (projects/<이름>/)

| 파일 | 내용 |
|---|---|
| `project.json` | 전부: `sources[]`(id s1…, path, offset, duration, status), `settings.cut/subs/asr`, `cuts[]`, `subtitles[]`, `speakers[]`, `splits[]`, `comments[]`, `notes`, `rev`, `by` |
| `sources/<sid>/` | `audio.wav`, `peaks.json`, `thumbs.jpg/json`, `silences.json`, `transcript.json`(단어), `proxy.mp4`(크롬이 못 여는 형식일 때만) |
| 재생 | 크롬이 여는 형식(mp4/mov, H.264·HEVC, AAC)은 원본을 그대로(`source.play: "original"`, `/media/<n>/sources/<sid>/original`), 나머지만 변환본. 플레이어는 클립마다 파일을 따로 열어요 |
| `history/` | 큰 변경(자동 컷 다시 찾기·자막 다시 만들기 등) 전 스냅샷 |
| `exports/` | MP4·SRT·캡컷 대비책 |

- 컷 `type`: `silence` 쉼, `long` 긴 무음, `filler`, `gap` 말 없는 장면(꺼진 제안), `ng`, `retake`, `manual`(사용자 요청), `user`(UI에서 자름), `clip`(클립 통째로). `enabled` = 자름, `reviewed` = 사용자가 확인함
- 자막: `start/end` 는 실제 표시 시간(원본 축), `speech` 는 말소리 구간, `locked` 면 타이밍 설정이 건드리지 않음, `pos` {x,y}(% 중심) 없으면 화자 위치 → 기본 50/88, `check` = 주황 점(확인 필요). 다른 화자끼리는 겹쳐도 된다
- v1 프로젝트(meta.json + edit.json + review.json)는 UI 로 열거나 `ev.py migrate` 하면 v2 로 옮겨진다(v1 파일은 그대로)

## 캡컷

- 캡컷 9.6 은 프로젝트를 평문 JSON 으로 저장한다. `editvdo/capcut.py` 는 설치된 캡컷이 직접 쓴 초안(`editvdo/capcut_seed/`)을 복제해 id·시간·경로·글자만 바꾼다. 캡컷이 업데이트되면 새 초안으로 seed 를 다시 떠야 할 수 있다
- 영상은 초안 폴더 `media/` 에 APFS 복제(`cp -c`, 용량 안 늘어남)하고 경로를 `##_draftpath_placeholder_0E685133-18CE-45ED-8CB8-2904A212EC80_##/media/<파일>` 로 쓴다 → 폴더째 다른 맥의 캡컷 프로젝트 폴더(`~/Movies/CapCut/User Data/Projects/com.lveditor.draft`)에 넣으면 그대로 열린다
- 이 맥 캡컷에 바로 넣을 때는 캡컷이 꺼져 있어야 한다(켜져 있으면 "캡컷을 먼저 닫아 주세요"). 사용자 캡컷 프로젝트(1029 등)는 절대 건드리지 않는다
- 자막은 캡컷 `subtitle` 타입(가져온 캡션과 같은 group_id)이라 캡컷에서 한 번에 꾸밀 수 있다. 위치: `x = 2·cx − 1`, `y = 1 − 2·cy`

## 구조

- Python(표준 라이브러리 서버): `project.py` 데이터·잠금·rev·ops·시간 변환·v1 이전 · `media.py` ffprobe/프록시/파형/썸네일 · `analyze.py` 무음·인식·자동 컷 · `subs.py` 줄 나누기·타이밍(retime)·대본 파싱·정렬 · `capcut.py` · `render.py`(MP4/SRT, 자막은 Pillow PNG overlay — 이 Mac ffmpeg 엔 libass 가 없음) · `jobs.py` · `server.py` · `ev.py`
- UI(빌드 없는 ES 모듈): `ui/js/store.js`(상태·ops 일괄 저장·되돌리기·/rev 1초 폴링) · `time.js`(project.py 의 시간 변환과 같은 규칙 — 한쪽을 바꾸면 다른 쪽도) · `shell.js`(레이아웃·단축키) · `timeline.js` · `player.js`(video 두 개로 컷 건너뛰기) · `inspector.js` · `export.js` · `panels/{media,cuts,subs,memo}.js` · 디자인 토큰은 `app.css`
- UI 글은 짧게: 아이콘 + 1~3 단어, 설명 문장 금지(사용자가 글 많고 AI 같은 디자인을 싫어함)
- 테스트: `.venv/bin/python -m unittest discover -s tests` · 화면 확인은 `ev.py serve --port <다른 포트>` + 헤드리스 크롬 스크린샷
- 이 Mac 에선 다른 도구가 8765 포트를 쓸 수 있다. 막혀 있으면 `serve --port 8766`

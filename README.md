# EditVDO

여러 클립으로 찍은 브이로그를 컷편집하고 자막을 달아 **캡컷 프로젝트**로 내보내는 로컬 편집기예요.
Claude 도 같은 프로젝트를 `ev.py` 로 고치고, 열려 있는 화면은 1초 안에 새로 불러와요. 원본 영상은 건드리지 않아요.

## 설치 (Apple Silicon 맥)

```
brew install ffmpeg python@3.12
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

음성 인식 모델은 처음 인식할 때 자동으로 받아요 (약 1.6GB).

## 켜기

```
.venv/bin/python ev.py serve        # → http://localhost:8765  (포트가 막혀 있으면 --port 8766)
```

## 화면에서

1. **새 프로젝트** → 클립 고르기(또는 창에 끌어다 놓기) → 변환·무음 찾기·음성 인식이 자동으로 돌아요
2. **컷** 탭: `▶ 차례로 확인` 으로 컷마다 듣고 `X` 자르기 / `V` 살리기
3. **자막** 탭: AI로 만들기 또는 대본 불러오기(SRT·VTT·시간+글·클로바노트) → 타이밍·화자 위치 조정
4. **메모** 탭: 시점마다 요청을 남기면 Claude 가 읽고 답해요
5. **내보내기**: 캡컷 프로젝트 · MP4 · SRT
   - 캡컷 프로젝트는 기본으로 `exports/capcut-<시각>/<이름>/` 에 영상까지 담은 폴더를 만들어요.
     폴더째 다른 맥 캡컷의 초안 폴더에 넣으면 열려요.
   - 이 맥 캡컷에 바로 넣을 수도 있어요 (캡컷을 닫고, `ev.py export <p> capcut --here`).

## Claude 에게 맡길 때 (ev.py)

시간은 모두 원본 시간(클립을 순서대로 이은 축)이고, 출력에는 편집본 시간도 같이 나와요.

```
ev.py new "성수 브이로그" a.mov b.mov     만들고 분석 (끝날 때까지 기다려요)
ev.py text <p> [--words] [--from 1:00 --to 2:00]   말·자막·컷을 시간순으로
ev.py cuts <p> --todo · cut <p> 1:02 1:05 "말 꼬임" · on|off|drop <p> <id…>
ev.py subs <p> · sub <p> <id> --text "…" · import <p> 대본.txt · timing <p> --hold 0.5
ev.py comments <p> · reply <p> <id> "반영했어요" --done · note <p> "이번에 한 일"
ev.py at <p> 2:30        편집본 시간 → 원본 시간
ev.py export <p> capcut|mp4|srt [--here]   캡컷: 기본은 exports/ 폴더, --here 는 이 맥 캡컷
```

이 맥 캡컷 초안 폴더 대신 다른 곳에 넣으려면 `EDITVDO_CAPCUT_ROOT=<폴더>` 를 지정해요.

모든 명령은 `ev.py <명령> -h` 로 볼 수 있어요. 예전(v1) 프로젝트는 화면에서 열거나 `ev.py migrate <p>` 로 옮겨요.

## 파일

`projects/<이름>/project.json` 하나가 편집 상태 전부예요. 큰 변경 전 상태는 `history/`, 내보낸 MP4·SRT·캡컷 폴더는 `exports/` 에 있어요.

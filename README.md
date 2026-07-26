# youtube-script

YouTube 영상을 **전사(whisper.cpp) → 요약(Claude CLI) → 키프레임 리포트**로 만드는 로컬 Flask 앱.
웹 UI에서 URL/파일을 큐에 넣으면 SSE로 진행상황을 스트리밍하고, 결과는 SQLite(FTS5) 이력으로 검색한다.

## 구성

| 파일 | 역할 |
|------|------|
| `app.py` | Flask 라우트·SSE 스트림·작업(jobs) 관리·전사/요약 오케스트레이션 |
| `db.py` | SQLite + FTS5 이력 인덱스(upsert/검색/reindex). `PRAGMA user_version` 마이그레이션 |
| `keyframe_report.py` | 영상 다운로드(yt-dlp) → ffmpeg 프레임 추출 → Claude 비전 분류 → 요약 md에 캡처 스트립 주입 |
| `templates/` | `index.html`(데스크톱) · `mobile.html` · `summary.html` |
| `static/js/common.js` | 공유 모듈(`window.YS`): 마크다운 렌더·이스케이프·이력 API·키프레임 라이트박스 |
| `static/js/index.js` | 데스크톱 UI 로직(큐·SSE·모달·이력) |
| `static/css/index.css` | 데스크톱 스타일 |
| `prompt.txt` | 요약 프롬프트(반드시 `{transcript}` 플레이스홀더 포함) |

## 데이터 흐름

```
URL/파일 → yt-dlp(오디오) → whisper.cpp(--output-json) → 전사 .md(res/{date}/)
        → Claude CLI 요약 → 요약 .md(res/summary/{date}/) → db.upsert(인덱싱)
        → (옵션) keyframe_report → res/summary/{date}/{stem}.frames/*.jpg + 요약 md에 스트립 주입
```

- **생성 데이터(`res/`, `index.db`)는 git 비추적**(`.gitignore`). 저장소엔 코드/설정만 커밋.
- URL 작업의 다운로드 오디오는 전사 후 삭제(`KEEP_AUDIO=1`이면 보존). 업로드 파일은 보존.

## 실행 / 운영

launchd 에이전트 `com.yhandhs.youtube-script`(KeepAlive)로 상시 구동 — `.venv/bin/python app.py`, 포트 **5001**.

```bash
# 재기동(KeepAlive라 pkill은 무효 — 반드시 kickstart)
launchctl kickstart -k gui/$(id -u)/com.yhandhs.youtube-script
```

- 정적 프런트(html/js/css)는 no-store → **새로고침만으로 반영**. `app.py`/`db.py`/`keyframe_report.py` 변경은 재기동 필요.
- 로그: `~/Library/Logs/hermes/youtube-script.{out,err}.log`
- 원격 접속(Tailscale 등)은 **이력 모드** — 이력 조회·요약/전사 보기·읽음 표시·삭제는 허용하되, **새 전사·프롬프트 편집·키프레임 생성·로컬 파일 탐색은 차단**(허용 목록은 `app.py`의 `_REMOTE_DATA_ALLOWED`). 사설망(Tailscale, 본인 기기) 전제.

## 주요 환경변수

| 변수 | 기본 | 설명 |
|------|------|------|
| `KEEP_AUDIO` | (off) | `1`이면 전사 후 다운로드 오디오 보존 |
| `MAX_CONCURRENT_TRANSCRIBE` | 1 | 동시 전사 수 |
| `MAX_CONCURRENT_KEYFRAMES` | 1 | 동시 키프레임 처리 수(세마포어 직렬화) |
| `CLAUDE_BIN` | (자동탐색) | claude CLI 경로(미설정 시 호출 때마다 최신 설치본 탐색) |
| `VISION_MODEL` | opus | 키프레임 분류·캡션 모델 (Claude) |
| `VIDEO_MAXH` / `FRAME_WIDTH` | 1080 / 1708 | 다운로드 최대 높이 / 프레임 가로폭 |
| `SCENE_THRESHOLD` | 0.3 | ffmpeg 장면전환 임계값([0,1] clamp) |
| `MAX_CANDIDATES` / `MIN_GAP` | 40 / 6 | 후보 프레임 상한 / 근접 중복 제거 간격(초) |
| `PROBE_TIMEOUT` / `FFMPEG_TIMEOUT` / `VISION_TIMEOUT` | 30 / 600 / 300 | subprocess 타임아웃(초) |

## 키프레임 추출 개요

1. yt-dlp로 저해상도 영상 다운로드(임시 디렉터리).
2. **하이브리드 후보 추출** — 균등 간격(`fps=1/N`, 전 구간 커버) + 장면전환(`select='gt(scene,…)'`).
3. **근접 중복 제거**(6초) — 장면전환 프레임은 보존, 인접 균등 프레임만 솎음. 상한 40.
4. **비전 분류** — Claude CLI(최대 3회, 한도/인증 systemic이면 조기 중단). 실패 시 `vision_failed`(캡처만 생략, 요약은 유지).
5. 살아남은 프레임을 시간 라벨(`[mm:ss]`) 기준 섹션에 배정해 요약 md 소제목 직후에 가로 스크롤 스트립으로 주입.

## 의존성

`requirements.txt` 참고(Flask, yt-dlp[default], python-dotenv 등). 외부 바이너리: **ffmpeg/ffprobe**, **whisper.cpp**, **claude CLI**, **grok CLI**(요약 폴백), **deno**(yt-dlp의 YouTube JS 챌린지 해독용 — 없으면 포맷 누락·다운로드 실패 발생).

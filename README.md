# youtube-script

YouTube 영상을 **전사(whisper.cpp) → LLM 요약 → 키프레임 리포트**로 만드는 로컬 Flask 앱.
웹 UI에서 URL/파일을 큐에 넣으면 SSE로 진행상황을 스트리밍하고, 결과는 SQLite(FTS5) 이력으로 검색한다.

## 구성

| 파일 | 역할 |
|------|------|
| `app.py` | Flask 라우트·SSE 스트림·작업(jobs) 관리·전사/요약 오케스트레이션 |
| `db.py` | SQLite + FTS5 이력 인덱스(upsert/검색/reindex). `PRAGMA user_version` 마이그레이션 |
| `keyframe_report.py` | 영상 다운로드(yt-dlp) → ffmpeg 프레임 추출 → LLM 비전 분류 → 요약 md에 캡처 스트립 주입 |
| `templates/` | `index.html`(데스크톱) · `mobile.html`(모바일 이력) |
| `static/js/common.js` | 공유 모듈(`window.YS`): 마크다운 렌더·이스케이프·이력 API·키프레임 라이트박스 |
| `static/js/index.js` | 데스크톱 UI 로직(큐·SSE·모달·이력) |
| `static/css/index.css` | 데스크톱 스타일 |
| `prompt.txt` | 요약 프롬프트(반드시 `{transcript}` 플레이스홀더 포함) |
| `humanize_korean.py` | 요약 저장 직전 im-not-ai 결정적 윤문(연결어미 쉼표·`것이다` 등). `HUMANIZE_SUMMARY=0`이면 생략 |

## 데이터 흐름

```
URL/파일 → yt-dlp(오디오) → whisper.cpp(--output-json) → 전사 .md(res/{date}/)
        → Opus 5/GPT-6 Astra/Grok-4.5 순차 요약 → humanize_korean → 요약 .md(res/summary/{date}/) → db.upsert(인덱싱)
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

### PO 토큰 공급자 (다운로드 403 방지)

유튜브가 PO 토큰 없는 세션을 봇차단 실험 버킷(SABR-only·DRM·403)에 무작위 배정한다.
걸린 세션은 `HTTP Error 403: Forbidden`으로 다운로드가 막히며, **`player_client`를 바꿔도
우회되지 않는다**(android→SABR, tv→DRM). 이 저장소 밖 구성이라 여기 절차를 남긴다.

```bash
# 1) yt-dlp 플러그인 (이 프로젝트 venv에)
.venv/bin/pip install bgutil-ytdlp-pot-provider

# 2) 토큰 서버 (플러그인과 버전을 맞춘다 — 아래는 1.3.1 기준)
cd ~/projects && git clone --depth 1 --branch 1.3.1 \
  https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git bgutil-pot-provider
cd bgutil-pot-provider/server && npm ci && npx tsc     # build/main.js 생성

# 3) 상시 구동: launchd 에이전트 com.yhandhs.bgutil-pot (KeepAlive, 포트 4416)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.yhandhs.bgutil-pot.plist

# 4) 확인
curl -s http://127.0.0.1:4416/ping                     # {"server_uptime":...,"version":"1.3.1"}
.venv/bin/yt-dlp -v "<영상URL>" --simulate 2>&1 | grep "PO Token Providers"
#   → bgutil:http-1.3.1 (external) 이 보이면 정상 (script-node/deno는 unavailable이어도 무관)
```

- 서버는 Node(`~/.local/bin/node`)로 돌고 로그는 `~/Library/Logs/hermes/bgutil-pot.{out,err}.log`.
- venv를 재생성하면 **1)을 다시 설치**해야 한다. 플러그인만 있고 서버가 죽으면 토큰을 못 받아 403이 다시 는다.
- 토큰이 있어도 간헐적으로 걸릴 수 있어, 403이면 큐 지연 전에 **새 세션으로 즉시 1회 재시도**한다(`app.py` 전사 / `keyframe_report.py` 캡처).

## 주요 환경변수

| 변수 | 기본 | 설명 |
|------|------|------|
| `KEEP_AUDIO` | (off) | `1`이면 전사 후 다운로드 오디오 보존 |
| `MAX_CONCURRENT_TRANSCRIBE` | 1 | 동시 전사 수 |
| `MAX_CONCURRENT_SUMMARIZE` | 1 | 동시 요약 수 |
| `MAX_CONCURRENT_KEYFRAMES` | 1 | 동시 키프레임 처리 수(세마포어 직렬화) |
| `CLAUDE_BIN` | (자동탐색) | claude CLI 경로(미설정 시 호출 때마다 최신 설치본 탐색) |
| `CLAUDE_TIMEOUT` | 900 | 요약 Claude CLI wall-clock 제한(초) |
| `CODEX_BIN` / `GPT_MODEL` / `GPT_TIMEOUT` | (자동탐색) / gpt-6-astra / 900 | GPT 폴백용 Codex CLI 경로·모델·제한(초) |
| `VISION_MODEL` | opus | 키프레임 분류·캡션 모델 (Claude) |
| `GROK_MODEL` | (빈 값) | Grok 폴백 모델. **비우면 `-m` 없이 grok CLI 기본 모델**을 쓴다(CLI 업데이트를 자동으로 따라감) |
| `GROK_VISION_FALLBACK` / `GROK_VISION_MODEL` | 1 / (빈 값) | 비전 Claude 3회 실패 시 Grok 폴백(모델은 요약과 동일 기조 — 비우면 CLI 기본) |
| `HUMANIZE_SUMMARY` | 1 | `0`이면 요약 저장 직전 한글 윤문(im-not-ai C-11/I-3)을 건너뛴다 |
| `VIDEO_MAXH` / `FRAME_WIDTH` | 1080 / 1708 | 다운로드 최대 높이 / 프레임 가로폭 |
| `SCENE_THRESHOLD` | 0.3 | ffmpeg 장면전환 임계값([0,1] clamp) |
| `MAX_CANDIDATES` / `MIN_GAP` | 40 / 6 | 후보 프레임 상한 / 근접 중복 제거 간격(초) |
| `PROBE_TIMEOUT` / `FFMPEG_TIMEOUT` / `VISION_TIMEOUT` | 30 / 600 / 300 | subprocess 타임아웃(초) |
| `MONITOR_STALE_PROCESSING_SEC` | 10800 | 중단된 자동 처리 claim 회수 기준(초) |
| `MONITOR_MAX_PIPELINE_ATTEMPTS` | 3 | 자동 처리 최대 시도 횟수 |
| `MONITOR_RETRY_BASE_SEC` / `MONITOR_RETRY_MAX_SEC` | 1800 / 21600 | 재시도 지수 백오프 시작 간격 / 상한(30분→1h→2h, 최대 6h) |

## 키프레임 추출 개요

1. yt-dlp로 저해상도 영상 다운로드(임시 디렉터리).
2. **하이브리드 후보 추출** — 균등 간격(`fps=1/N`, 전 구간 커버) + 장면전환(`select='gt(scene,…)'`).
3. **근접 중복 제거**(6초) — 장면전환 프레임은 보존, 인접 균등 프레임만 솎음. 상한 40.
4. **비전 분류** — 자동모니터 팝업에 저장한 Opus 5/GPT-6 Astra/Grok-4.5 순서대로 폴백. 수동 캡처는 기존 Opus 5 → Grok-4.5 정책을 유지한다. 모든 모델이 실패해야 `vision_failed`(캡처만 생략, 요약은 유지).
5. 살아남은 프레임을 시간 라벨(`[mm:ss]`) 기준 섹션에 배정해 요약 md 소제목 직후에 가로 스크롤 스트립으로 주입.

## 의존성

`requirements.txt` 참고(Flask, yt-dlp[default], python-dotenv 등). 외부 바이너리: **ffmpeg/ffprobe**, **whisper.cpp**, **claude CLI**, **Codex CLI**(GPT), **grok CLI**, **deno**(yt-dlp의 YouTube JS 챌린지 해독용 — 없으면 포맷 누락·다운로드 실패 발생).

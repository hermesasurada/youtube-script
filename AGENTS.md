# youtube-script 작업 규칙

YouTube 영상을 전사(whisper.cpp)하고 한국어로 요약·캡처해 서재(5001)에 쌓고 블로그로 발행하는 서비스다.
이 문서에는 **지금 지켜야 할 규칙**만 둔다. 도입 계기 영상·실측 수치·소급 건수·백업 이름·뒤집힌 옛 결정은 `docs/HISTORY.md`, 구성 표·환경변수 표·PO 토큰 절차는 `README.md`를 본다.

- 서버는 launchd `com.yhandhs.youtube-script`(KeepAlive, `.venv/bin/python app.py`, 포트 5001)다. `.py`를 고치면 `launchctl kickstart -k gui/$(id -u)/com.yhandhs.youtube-script`, 정적 프런트는 새로고침만으로 반영된다.
- 채널 감시는 launchd `com.yhandhs.youtube-monitor`(10분 주기, 2026-11-18 사용자 결정 — plist `StartInterval`과 `yt_queue.QUEUE_CYCLE_SEC`를 함께 바꾼다. 403은 간격과 무관함을 로그로 확인)다. 서버 로그는 `~/Library/Logs/hermes/youtube-script.{out,err}.log`.
- 비상 스위치 환경변수는 launchd plist에 넣고 kickstart한다.

## 코드 구조 (2026-11-16~17 리팩토링)

- **원칙**: 동작은 바꾸지 않고 옮기기·합치기만 한다. HTTP 라우트 주소·응답은 그대로 둔다(채널 모니터·일괄 재요약이 HTTP로 부른다). wm과 글자 단위로 같아야 하는 파일(`static/js/model-selector.js`·`static/css/model-selector.css`·`llm_catalog_fallback.py`)은 손대지 않는다.
- **LLM 실행은 `llm_runners.py` 한 곳**(1단계)
  - `run_text(계열, 프롬프트, model·effort·purpose·title·timeout, postprocess)`가 Claude(json)·Codex·Grok(--prompt-file)·Gemini(API) 단일턴을 실행하고 이력(llm_log)을 남긴다.
  - `postprocess`를 주면(요약) 다듬은 결과가 비었을 때 '빈 응답' 실패로 기록한다.
  - Claude stream-json 요약은 `stream_claude`가 ("model", id)·("text", 조각)을 내보내고 `StreamOutcome`을 돌려준다(모니터 슬롯 경로·수동 경로가 함께 쓴다).
  - app 쪽은 계열별 기본 모델·실행 파일을 채우는 `yt_models._llm_text`와 얇은 래퍼(`_summarize_with_gpt/grok/gemini`, 제목 번역·한눈 요약용 `_TITLE_TR_PROVIDERS`)만 남는다 — 새 계열은 `llm_runners.run_text`에 분기 하나와 `_llm_text` 기본 모델 한 줄로 붙인다.
  - Grok 문체 보정(`_GROK_STYLE_NOTE`)은 `_summarize_with_grok` 래퍼가 붙인다.
  - 이미지를 보는 호출은 `run_vision(계열, 프롬프트, model·images·title·timeout, finish, …)`이 맡는다(캡처). 응답 해석과 실패 기록은 호출처가 `finish`로 넘긴다. 추론 수준·CLI 최소 구성 인자는 붙이지 않는다(Read 도구가 필요하다).
- **app.py는 진입점만**(2단계)
  - 기능별 모듈로 나눴다 — `yt_web`(Flask 앱·원격 접근 제한 `_REMOTE_DATA_ALLOWED`·압축), `yt_core`(설정·jobs·md 도구), `yt_transcribe`, `yt_jobs`, `yt_models`, `yt_summary`, `yt_titles`, `yt_queue`, `yt_blog`, 라우트 `routes_main/summary/queue/channels/history/media`.
  - 라우트 모듈은 `from yt_web import app`으로 `@app.route`를 그대로 쓰므로 엔드포인트 이름이 예전과 같다(Blueprint 아님). app.py가 라우트 모듈을 가져와야 라우트가 붙는다(`ROUTE_MODULES`).
  - 다른 모듈의 이름은 `yt_core.SUMMARY_DIR`처럼 **모듈 이름을 붙여** 부른다 — 시험에서 `monkeypatch.setattr(yt_core, "GROK_BIN", …)`처럼 그 이름이 사는 모듈을 바꿔 끼우면 모든 호출처에 먹는다(`app.X`로 바꾸면 안 먹는다).
  - 새 기능은 맞는 모듈에 넣고 라우트 모듈끼리는 서로 가져오지 않는다(공용 도우미는 `yt_*`로).
  - 문자열 배선 검사 테스트는 `tests/test_regression._server_src()`(app.py+yt_*+routes_* 합본)를 읽는다.
  - 분할은 일회용 도구로 했고 옮긴 구문 342개가 모듈 한정자만 빼면 원본과 같음을 대조했다.
- **프런트 공용화**(3단계)
  - 모바일 화면(`/m`)의 스크립트·스타일은 `static/js/mobile.js`·`static/css/mobile.css`다(템플릿 `templates/mobile.html`은 마크업과 자원 목록만, 같은 자리에서 일반 `<script>`/`<link>`로 실어 실행 순서·전역 함수가 예전과 같다).
  - PC(`index.js`)와 모바일에 같은 글로 두 벌 있던 계산·API는 `common.js`의 `YS.fmtUnreadDur`·`YS.hms`·`YS.thumbFallback`·`YS.termSenseTitle`·`YS.uploaderSort`·`YS.queueApi`로 모았고, 양쪽 옛 함수 이름은 한 줄 래퍼로 남아 onclick·템플릿 문자열 호출처가 그대로다.
  - 같은 이름이어도 상태 배열(`_historyItems`↔`ALL`)·DOM이 다른 함수(toggleRead·deleteCard 등)는 억지로 합치지 않았다.
  - 새 기능이 양쪽에 필요하면 계산·API는 `YS`에, 그리기만 각 화면에 둔다.
  - 문자열 배선 테스트는 모바일을 `tests/test_regression._read_src`(html+mobile.js+mobile.css 합본)로 읽는다.
- **DB 영역 분리**(4단계)
  - `db.py`는 연결(`_conn`·`close_conn`)·스키마(`_SCHEMA`)·공용 도우미(`_now`·`_after`·`_json_list/_dict`)·경로 상수만 두고, 쿼리는 `db_items`·`db_channels`·`db_terms`·`db_queue`로 나눴다.
  - **호출은 계속 `db.이름`** — db.py 끝에서 네 모듈의 이름을 다시 내보낸다. 영역 모듈 안에서도 db의 다른 이름은 `db.`를 붙여 부르므로(같은 모듈 안 호출도) 시험의 `monkeypatch.setattr(db, …)`·`importlib.reload(db)`가 예전처럼 먹는다.
  - 새 쿼리 함수는 맞는 영역 모듈에 넣고 db.py의 다시 내보내기 목록에 이름을 더한다(안 더하면 `db.새이름`이 없다).
  - 마이그레이션은 `db_migrations.MIGRATIONS`(번호별 함수, **역사적 순서 그대로 — v20이 v19보다 앞**)이고 `db.init()`은 시작 때 읽은 user_version보다 큰 번호만 목록 순서대로 돌린다.
  - 분리 때 마이그레이션 26개·쿼리 구문 전부가 `db.` 접두어만 빼면 원본과 같고, 새 DB 스키마가 글자까지 같음을 대조했다.
- **설정 위치**(4단계)
  - 서버 환경변수는 대부분 `yt_core`의 설정 블록 한 곳이다.
  - 도메인에 붙은 값(whisper 세부값 `yt_transcribe`, 제목 번역 기본 모델 `yt_titles`, 큐 주기 `yt_queue`, 한눈 요약 생략 길이 `summary_policy`)은 그 모듈에 두고 README '주요 환경변수'에 위치를 적었다 — 옮기면 시험의 패치 대상만 바뀌고 얻는 게 없어 그대로 뒀다.

- **다운로드 재시도는 `yt_download.py` 한 곳**(11-17)
  - 음성(`yt_jobs.run_job`)과 영상(`kf_frames.download_video`)이 `download_with_retry`를 함께 쓴다: 3번 시도, 1번 실패 → 곧바로 2번, 2번 실패 → 75초 뒤 3번.
  - **대체 클라이언트를 쓰는 시도와 재시도 조건은 호출처가 넘긴다** — 음성은 3번째에 대체·403만 재시도, 영상은 1번째에 대체·빈 파일·포맷 없음·SABR까지 재시도. 실측으로 따로 맞춘 값이라 하나로 합치지 않는다.
  - 스트림 잘림 감지(`after_download`)·취소 확인(`should_stop`)·알림 문구(`on_retry`)는 호출처가 넘긴다.
  - **403이 잦아지면 먼저 yt-dlp 버전을 본다**(2026-11-19 실측): 2026.7.4(넉 달 묵음)에서 기본 클라이언트(android_vr)가 모든 영상에서 403이라 3번째 대체 시도에만 기대고 있었고, 그 시도까지 막히면 통째로 실패했다. 2026.10.26으로 올리자 기본·대체 모두 한 번에 받았다. 올릴 때는 임시 venv에서 기본·대체 다운로드를 먼저 시험하고, `requirements.txt` 핀도 함께 바꾼 뒤 서버를 재시작한다. bgutil 플러그인은 실행 중인 토큰 서버(4416)와 같은 1.3.1을 유지한다.
- **캡처는 `kf_*` 네 모듈 + 진입점**(11-17)
  - `kf_base`(설정값·ffmpeg 경로·`_hms`) / `kf_targets`(요약·전사에서 캡처 대상 고르기, 오프닝 제외) / `kf_frames`(영상 받기·후보 프레임 추출) / `kf_vision`(비전 판정 `classify_and_assign`, 실패 사유 `_LAST_VISION_ERR`).
  - `keyframe_report.py`에는 진입점 `generate_keyframes`와 요약 md에 끼워 넣는 코드만 남는다.
  - 캡처 설정은 `kf_base` 한 곳이고 테스트는 `monkeypatch.setattr(kf_base, "FRAME_WIDTH", …)`처럼 거기 값을 바꾼다.
- **채널 감시는 `cm_*` 네 모듈 + 진입점**(11-17)
  - `cm_base`(설정값·알림용 환경 파일 읽기·재시도 간격) / `cm_health`(요약 모델 상태 점검 `summarizer_gate`) / `cm_notify`(텔레그램 알림) / `cm_detect`(신규 영상 감지·걸러내기·큐 적재 `poll_channels`, `_defer_policy`).
  - **텔레그램은 실패·경고만 보낸다**(2026-11-19 사용자 지시): 자동 요약 완료·캡처 재시도 성공·캡처 재시도 예약은 알리지 않고 로그(`youtube-monitor.out.log`)에만 남긴다. 캡처가 다시 해 볼 수 없게 실패하면 '⚠️ 캡처 실패 — 요약은 완료'로 알린다. 성공 알림을 되살리지 말 것.
  - `channel_monitor.py`에는 영상 처리(`process_video`)·큐 비우기(`drain`)·`main`만 남는다. launchd가 부르는 파일 이름은 그대로다.
  - **`cm_base`가 환경 파일을 읽은 뒤에 토큰·기본값을 잡는다** — 감시기의 다른 모듈은 `cm_base`를 먼저 불러온다. 서버(`routes_channels`)는 채널 ID 확인에 `cm_detect`만 불러온다.
- **전사 번역**(11-17)
  - 이력 기록은 `llm_gateway`가 맡는다(`llm_track`·`local_host_label`·`llm_record_openai`) — `transcript_translator`·`term_senses`에 따로 두지 않는다.
  - 번역 결과 검사(반복 붕괴·한자 누출, LLM을 부르지 않는 순수 함수)는 `translation_guards.py`. 교정을 모델에 다시 시키는 `_repair_leaks`와 파일 처리 흐름은 `transcript_translator.py`.
- **PC 화면 스크립트는 `index.js` + `index-*.js`**(11-17)
  - 읽는 순서: `index.js`(상태·프롬프트·설정 창) → `index-history.js`(이력·검색조건·채널 팝업) → `index-queue.js`(진행 표시·처리 큐 팝업) → `index-run.js`(전사 시작·요약 생성·파일 창) → `index-urlqueue.js`(주소 대기열) → `index-summary.js`(요약 창·글꼴·전사 번역) → `index-boot.js`.
  - 전역 범위를 함께 쓰는 일반 스크립트라 읽는 순서가 실행 순서다. **페이지가 열릴 때 바로 도는 호출(`loadHistory()` 등)은 `index-boot.js`에만 둔다** — 앞 파일에 두면 뒤 파일이 읽히기 전에 응답이 돌아와 함수를 못 찾을 수 있다.
  - 파일을 더하면 `templates/index.html`과 `tests/test_regression._DESKTOP_JS`에 넣는다. 문자열 배선 테스트는 `_read_src(root, "static/js/index.js")`가 `index*.js` 전부를 합쳐 읽는다.
  - `common.js`는 한 IIFE라 글자 그대로는 나눌 수 없어 그대로 뒀다.
- **일회용 스크립트는 `tools/`**: `batch_resummarize.py`·`backfill_compress.py`·`backfill_audio_archive.py`. 저장소 루트를 경로에 넣고 돌므로 어디서 실행해도 된다.
- **옮긴 뒤 대조**: 글자 그대로 옮긴 것은 구문 단위로(모듈 한정자만 빼고) 원본과 대조하고, 동작이 바뀔 수 있는 통합(다운로드 재시도·비전 호출·번역 이력)은 가짜 실행기로 전후의 명령·대기·이력 기록을 떠서 비교했다. 다음에 옮길 때도 같은 방식으로 한다.

## 모델 설정

- 모델 목록·표시 이름·추론 단계는 공용 LLM 카탈로그(`~/projects/hermes-llm-log/llm_catalog.py`, `~/.hermes/data/llm_catalog.json`)에서 오고 포탈 모델 관리 화면에서 관리한다. 화면에 독립된 선택 목록을 만들지 않는다.
- 선택·슬롯 수·순번·폴백 순서는 yt에 둔다. 저장된 선택은 카탈로그 장애·모델 비활성화 때도 보존하고(신규 선택만 막는다), 새로 발견된 모델은 수동으로 켜기 전까지 후보다.
- 과거 요약의 실제 모델 ID는 바꾸지 않는다. 카탈로그를 읽는 코드가 포탈 가동 여부에 기대거나 추론 호출을 일으키면 안 된다.
- **카탈로그는 부가 정보다 — 못 불러와도 본업이 멈추면 안 된다.** import는 `try/except Exception`으로 감싸고, 실패하면 같은 API를 흉내 내는 `llm_catalog_fallback.py`(wm과 같은 내용)로 대신한다. 카탈로그 경로는 `sys.path.append`로 **뒤에** 붙인다.
- 대체 모드의 선택지는 무엇이든 '포함'으로 답하므로, 모델 계열은 선택지 소속(`in …_CHOICES`)이 아니라 `llm_catalog.resolve()`의 provider → 모델 ID 접두어 순으로 판단한다. 검증은 `tests/test_catalog_isolation.py`.
- 요약 선택지는 `llm_gateway.SUMMARY_MODEL_CHOICES` 하나다(Claude·Codex·Grok·Gemini 실행기의 카탈로그 text 모델). Grok 구체 모델 ID도 받는다. `opus` 별칭은 Claude CLI의 최신 Opus로 풀린다.
- **요약 슬롯**: 1~5개, 슬롯마다 구체 모델+추론. 큐 영상마다 시작 슬롯을 한 칸씩 넘기고, 실패하면 같은 순환의 다음 슬롯이 받는다. 같은 모델을 여러 칸에 두는 것은 시작 비중을 높이는 용도다(사용자 결정).
- 실패 폴백(`yt_summary._summary_attempts`)은 모델별 첫 등장만 남겨 방금 실패한 모델을 다시 부르지 않는다(추론만 다른 같은 모델도 한 번).
- 저장은 `monitor_settings.summary_slots`·`summary_cursor`, 원자적 예약은 `db.reserve_monitor_summary_slots`. 게이트(`summarizer_gate`)는 계열 단위라 슬롯 계열 순서로 묻고, 쓸 수 있는 계열의 첫 슬롯부터 `/summarize`에 `slots`로 넘긴다.
- 수동 요약처럼 모델 하나만 필요한 경로는 `yt_models._model_versions()`(계열별 첫 슬롯)를 쓴다.
- **Gemini는 구독 CLI가 아니라 API 키**로 부른다(계열 키 `gemini`, `hermes-llm-log/gemini_api.py`, 키 `~/.hermes/gemini.env`). 요약 슬롯·제목 번역·한눈 요약에 쓰고 실패·한도 초과는 다음 순번이 받는다. 옛 3계열 순번(`MODEL_KEYS`·`SLOT_COUNT`)에는 넣지 않으며 게이트는 키 유무만 본다. **캡처(비전)·인명 검색에는 쓰지 않는다**(이미지·웹 검색 러너 없음).
- **제목 번역 모델**: 저장값 `monitor_settings.title_model/title_effort`(`yt_titles._title_tr_setting`)가 먼저 맡고, 실패한 묶음만 요약 슬롯 순번(추론 기본값)으로 넘긴다. 저장값이 비면 `TITLE_TR_MODEL`·`TITLE_TR_REASONING`. 로컬 모델은 러너가 없어 서버가 400으로 거부한다.
- **인명 확인 모델**: 판별·검색(웹)을 따로 고른다(`monitor_settings.name_detect_model/effort`·`name_lookup_model/effort`, 빈 값 = `name_resolver.default_choice()`). 웹 검색 도구가 있는 Claude·Codex만 허용한다. `name_fix`가 매 실행마다 DB에서 읽어 재시작이 필요 없고, 못 읽으면 기본값으로 돈다.
- **모델 선택기는 wm과 같은 디자인**이다. `static/js/model-selector.js`·`static/css/model-selector.css`는 wm 사본과 글자 단위로 같아야 한다(`tests/test_model_versions.py`가 대조). yt는 '슬롯 모드', wm은 '계열 모드'를 쓴다.
- 선택기 상태·저장은 `common.js`의 `YS.monitorModels`가 맡고 PC·모바일이 같이 쓴다. 구성은 요약 슬롯·인명 확인·'예외 규칙'(`/channels`의 `model_notes`, 읽기 전용)이고 바꾸면 바로 저장한다. '다음' 글자 없이 번호 배지 강조(`is-next`)로만 다음 차례를 보인다.
- **모델 라벨은 Claude 접두어 없이 등급+버전**이다(`Opus 5.5`·`GPT 6 Astra`). 정본은 `yt_models._model_label`이고 두 조각 ID(`claude-opus-5`)도 `Opus 5`로 바꾼다. 별칭만 알 때는 응답으로 확인된 마지막 버전, 그 전에는 `Opus`.
- 옛 요약 파일의 모델 표기는 **파일을 고치지 않고** 화면에서 `YS.normalizeModelLabel`로 바꿔 보인다. 모델 칩은 블로그 본문에도 들어가므로 표기 규칙을 바꾸면 `BLOG_RENDER_VERSION`도 올린다.
- **CLI 최소 구성**(wm·yt·sa·td 공통): 텍스트 전용 Claude·Codex 호출(요약·제목 번역)에는 `hermes-llm-log/cli_minimal.py` 인자를 붙인다. 요약의 `_SUMMARY_SYS`는 교체한 시스템 프롬프트 뒤에 붙인다. 모듈이 없으면 예전 구성으로 돈다. 비상 스위치 `HERMES_CLI_MINIMAL=0`.

## 요약 프롬프트

`prompt.txt`와 `prompt_default.txt`는 같은 글로 유지한다.

- **프롬프트 효과는 3회 이상 돌려 판정한다.** 1회 통과 뒤 운영에서 재발한 적이 있다.
- **요약 파일을 다시 쓰면 mtime이 바뀌어 발행된 글이 '수정 가능'이 된다.** 규칙을 바꿔도 기존 요약은 소급하지 않는 것이 기본이다.
- **원문 표기**: 회사·브랜드·제품·모델·기관명은 요약·제목 번역·전사 번역 모두 원문 표기다(NVIDIA·Google — 엔비디아·구글도 쓰지 않는다, 사용자 결정). 인명도 예외 없이 원문 철자다(Elon Musk 포함). 지명은 널리 알려진 곳만 한글. Neocloud는 음차하지 않는다.
- **달러 금액은 `$`+축약 단위**(`$9.25B`·`$500M`, wm·sa와 통일). 화자가 한국어로 말한 '억·조'와 다른 통화는 환산하지 않는다.
- 세 프롬프트(`prompt.txt`, `yt_titles._TITLE_TR_PROMPT`, `transcript_translator.SYSTEM_PROMPT`)를 같은 표기 기준으로 유지한다.
- **1인 영상은 주체를 세우지 않는다**: 화자가 한 명인 영상은 `진행자는/그는/영상은 ~ 짚는다·전한다` 같은 전달 문장을 쓰지 않는다. 사건·수치·경과는 주체 없이 직설, 의견·당위·전망은 주어 없이 `~로 본다`. '누가·어떤 형식으로 진행하는 영상인지' 소개하는 도입 문장도 쓰지 않는다.
- 인터뷰·대담은 대상이 아니다 — 의견 단락 첫머리에 발언자 이름을 밝힌다.
- 이 규칙은 **프롬프트 맨 앞 '원칙'에 둬야 지켜진다**(표기 규칙 목록 안쪽에 두면 모델이 어긴다). `~로 본다` 빈도를 줄이는 지시는 넣지 않는다(사용자 결정).
- 원칙을 바꿀 때는 표기 규칙·Grok 문체 보정·한눈 요약 프롬프트(`brief_rebuild.PROMPT`)까지 같은 기준인지 함께 본다.
- **소제목 개수**는 `영상 분수 ÷ 6 내외, 최소 3개·최대 10개`이고 예시 개수를 함께 적는다.
- **섹션 깊이는 소제목이 다루는 영상 시간에 비례한다**: 4분 미만 1단락 3~5문장, 4~8분 1~2단락, 8분 이상 2단락 이상(중심 주제 최대 4단락). 논쟁 대목은 발언자별 주장과 근거를 각각 남긴다. 고정 분량('섹션당 3~5문장')은 긴 영상에서 하한으로 읽혀 과압축된다.
- **볼드는 각 단락 안에서 중요한 내용에만** 쓴다. 밑줄 강조는 쓰지 않는다.
- **순번 나열은 불릿으로**: `첫째·둘째`, `첫 번째 ~은`, `세 가지를 꼽는다. A, B, C이다`는 한 줄 도입문 뒤 `-` 불릿으로 나눈다. 순번어는 다시 쓰지 않고, 근거·수치는 그 불릿 안에, 결론은 목록 뒤 단락에 둔다.
- **번역·재게시 영상**(비즈카페 등)은 그 영상을 그대로 전사·요약하고 원본 영상은 찾지도 링크를 달지도 않는다. 설명문 링크 읽기·검색 추정·원본으로 갈아타 전사하기는 되살리지 않는다(`tests/test_original_video.py`).
- **Grok 문체 보정**(`yt_summary._GROK_STYLE_NOTE`)은 Grok 호출에만 프롬프트 맨 앞에 붙는다(종결어미·한 문장 한 동작·주어 생략 금지·영어 조각 금지·표기 통일·불릿 1~2문장).

## 한눈 요약

- **본문을 다 쓴 뒤 별도 호출로 만든다**(사용자 결정). 첫 요약 프롬프트는 `## 2. 한눈 요약` 머리글만 비워 두고, `yt_summary._rebuild_brief`가 같은 모델(추론 기본값)을 한 번 더 불러 `## 3. 핵심 내용`만 근거로 **소제목 하나당 불릿 하나, 소제목 순서대로** 채운다.
- 프롬프트·검증·교체는 `brief_rebuild.py`다. 키프레임·각주 묶음은 입력에서 빼고, 머리글이 없으면 핵심 내용 앞에 넣는다. 이력 purpose `brief`.
- 호출 실패·불릿 수 불일치·문자 누출이면 **한 번만** 다시 시도하고, 그래도 안 되면 빈 머리글만 걷어 내고 저장한다(저장은 막지 않는다).
- 불릿이 있는 옛 형식(사용자 프롬프트 등)은 그대로 둔다. 요약 경로 4곳(모니터 슬롯 opus·gpt/grok, 수동 Claude·Grok 폴백)이 모두 거친다.
- 비상 스위치 `YT_BRIEF_REBUILD=0`은 한눈 요약 없이 저장한다. 2절이 없을 수 있으므로 `name_fix`는 `## 2.`가 없으면 `## 3.`부터 본다.
- **6분 이하 영상은 한눈 요약만 만들지 않는다**(사용자 결정 — 본 요약·캡처는 평소대로). 규칙은 `summary_policy.skip_brief` 한 곳: 전사 md의 `duration`이 360초 이하면 `/summarize`가 `brief=False`를 넘긴다. 구간 전사(`clip_start`)와 길이를 모르는 md는 만든다. 조정 `YT_BRIEF_MIN_SECONDS`(0 = 모든 영상).
- 각주 대상이 아니다 — 별표도 `term-notes`도 넣지 않는다(`brief_rebuild.PROMPT` + 렌더의 `_stripBriefMarks`).
- 카드(`.ys-tldr`)는 **소제목과 다른 모양**을 유지한다: 사방 테두리 카드, 채운 배지 '✦ 한눈 요약', '요점 N개', 번호 원 요점. 왼쪽 막대+그라데이션 띠는 h3 전용이다. 기본은 접힘.

## 각주(용어 해설)

- `## 3. 핵심 내용`에서 일반 독자에게 생소한 약어·기관·시장/기술 용어는 **첫 등장에만** `\*`를 붙이고 정의만 담은 짧은 해설을 둔다. 각주를 단 용어에는 반드시 `\*`를 붙인다. 개수 상한은 없다.
- **알려진 용어라 각주를 달지 않는 것**: Codex·ASIC·HBM·open-weight(오픈웨이트)·FSD·ETF·FDA·AGI·Neocloud·bay(베이)·토큰(token)·API·LLM·GPU·CPU·SaaS·클라우드·데이터센터·반도체·오픈소스·스타트업·벤치마크·프롬프트·에이전트·강화학습·휴머노이드·피지컬 AI·샌드박스·KV 캐시·MoE·RAG·어텐션·chain of thought(CoT)·컨텍스트 창.
- 본문에 풀네임·역할이 나와도 이름만으로 무엇인지 알 수 없으면 해설한다 — 각주는 '무엇인지'를 알려 주는 자리다. 낯선 영문 약어인 기관·프로젝트·임무명도 첫 등장 단락 뒤에 풀네임과 짧은 각주를 붙인다(전사의 음차를 영문으로 교정한 경우에도).
- 영문 약어는 확실히 특정되는 표준 풀네임을 `약어 (Full Name)`으로 함께 쓰되 추측하지 않는다. 명시적 제외 목록이 우선이고 용어당 1회다.
- 영문 용어의 각주 제목은 원어만 쓴다(본문에서 음차·번역어로 썼어도). 화면 `common.js`가 `한글 (English)` 제목을 영어만 남기므로 프롬프트도 같은 기준이어야 한다.
- 본문 반복·추가 배경·함의는 넣지 않고, 회사·제품명과 일반어까지 남발하지 않는다.
- 일상 한국어 단어의 결합만으로 뜻이 드러나는 합성어(언어 모델, 코딩 에이전트)는 대상이 아니다. 한국어여도 분야를 모르면 알 수 없는 말(레티클 한계, 투기적 디코딩)은 대상이다.
- **형식**: 용어가 처음 나온 단락 바로 다음에 `<div class="term-notes">`로 묶고 용어마다 `<p class="term-note">…</p>` 한 행. 다른 단락의 각주를 섹션 끝으로 합치지 않는다. 설명은 마침표 없는 명사형 문구로 끝낸다.
- **표시**: PC·모바일·블로그 모두 묶음 하나에만 인용선, 행 사이는 줄바꿈만, 본문보다 작은 회색 글꼴. 블로그에는 인라인 스타일로 보존한다.
- **표식과 각주의 짝은 요약 원본에서 맞춘다 — 렌더링에서 맞추지 않는다**(사용자 결정). `term_marks.fix`가 저장 직전(`yt_summary._polish_summary`)과 일괄 교정(`python term_marks.py --apply --backup-dir DIR`)에서 같은 규칙으로 고친다.
  - 소제목·한눈 요약의 표식은 지운다. 각주 묶음과 그 앞 단락을 한 단위로 글자가 맞는 것부터 짝짓고, 남은 수가 같으면 순서대로 짝짓는다.
  - 짝 없는 표식은 지우고(한 글자 이름 뒤 `A\*`는 보존), 표식 없는 각주는 용어 첫 등장 뒤에 표식을 더한다. 본문에 용어가 없는 각주는 그대로 둔다.
  - 순서로 짝지을 때 다른 각주와 글자가 맞는 표식은 뺀다. 순서로 짝지은 각주의 용어가 본문에 있으면 표식을 그 용어 뒤로 옮긴다.
  - **두 번 돌려도 같은 결과여야 한다**(`tests/test_term_marks.py`).
- **각주 묶음 위치**(`term_marks._relocate_notes`): 각주가 놓인 소제목(제목 포함)에 그 용어가 **한 번도 나오지 않을 때만** 용어가 처음 표식된 단락 뒤로 옮긴다. 소제목 주제가 그 용어이면 자리를 존중한다.
- **이미지 캡션(alt)은 각주 대상이 아니다**: 비전 판정 지시문이 별표를 금지하고, `_caption_text`가 저장 전에 별표를 지우며, `term_marks`는 `<`로 시작하는 HTML 줄을 건드리지 않는다.
- **각주 제외**: 뷰어 각주 행의 용어(굵은 글자)를 누르면 확인 뒤 그 용어를 `term_exclusions`에 넣는다(2026-11-19 사용자 지시로 ✕ 버튼 삭제 — 단어 클릭 `.term-note-word`, 본문 단어에는 달지 않음, 블로그·HTML 파일에선 `_stripTermNoteWords`로 표시만 뗌). 제외 목록은 화면 렌더(`applyTermExclusions`)·블로그 발행 HTML·새 요약 프롬프트(`yt_summary._inject_term_exclusions`)에 모두 적용된다. API는 `/terms/excluded`(원격 허용).
- **제외는 뜻 단위다**(사용자 결정 — AI '하네스'를 뺐더니 차량 배선 하네스 각주까지 사라졌다). 용어 클릭은 각주 설명문을 `term_exclusions.senses`에 뜻으로 기록하고, 같은 표기의 다른 각주는 로컬 Qwen(`term_senses.py`, Spark→oMLX)이 같은 뜻인지 판정해 `term_sense_checks`에 캐시한다.
  - 화면은 '다른 뜻' 판정(`keep`)만 남기고 판정 전·실패는 뺀다. 판정은 요약 저장(`_reindex_summary`)·요약 열기·뜻 추가 때 백그라운드로 돈다.
  - 프롬프트에는 `용어(뜻 앞 40자)`로 싣는다. 설정 탭에서 직접 입력한 용어(뜻 없음)는 모든 뜻을 뺀다.
  - **같은 뜻 여부를 글자 유사도로 가리지 않는다.** 전수 판정은 `.venv/bin/python term_senses.py --backfill [--dry-run]`.
- 제외 용어의 본문 별표는 `_stripExcludedMarks`가 지운다. 원어 병기의 닫는 괄호를 허용하고, 볼드로 노드가 갈려도 그 별표가 든 노드만 고친다. **단어 경계를 지킨다**(`(?<![A-Za-z가-힣])`, 숫자로 시작하지 않는 용어는 앞 숫자 허용) — 없으면 'PR'이 CRISPR\*의 별표를 지운다.
- 본문 별표는 렌더 마지막(`_supTermMarks`)에 파란 굵은 윗첨자 `<sup class="term-mark">*</sup>`로 감싼다(2026-11-18, sa·td 용어 툴팁·wm과 같은 모양). 앞 단계 규칙은 모두 글자 `*` 기준이라 반드시 맨 마지막에 두고, 감싼 뒤에도 글자는 `*`라 용어 클릭 제외 직후 `_stripExcludedMarks`가 그대로 통한다. 블로그는 `_BL.mark` 인라인 스타일.

## 요약 후처리와 문자 누출 게이트

- **문자 누출**(`yt_summary._script_leaks`): 한국어 요약에 섞인 중국어 단어, 한글 사이 한 글자 한자, 아랍·키릴 문자를 찾는다. 한글 뒤 괄호 병기(반(反))와 한 글자 단독 사용(前 CEO·전년比)은 정상이다.
- **영어 조각**(`_latin_fragments`): 소문자 4자 이상 영어 낱말이 ① 앞에 공백 두 칸 ② 한글 낱말 안 ③ 한글 조사가 바로 붙음 중 하나이고, 전사·제목에 어간이 없고(`_stem`), 요약 안에 3회 미만일 때 누출이다. 영문 제목·메타 표·캡션·각주·괄호 병기·코드·수식은 빼고, 띄어 쓴 일반 영어 용어는 둔다.
- `_summarize_ordered`의 GPT/Grok 경로는 누출이 있으면 교정 지시(`_SCRIPT_RETRY_NOTE`)를 붙여 1회 재요청하고, 남으면 실패로 처리해 다음 모델로 넘긴다. Claude 스트리밍 경로는 `_warn_script_mix` 경고만 남긴다.
- **누출 규칙을 바꾸면 기존 요약 전체(`SUMMARY_DIR`)로 오탐을 다시 보정한다.**
- **표기 치환**(`notation.py`): 저장 직전에 흔한 한글 브랜드 표기를 원문으로 바꾸고 영문 이름 뒤 조사(Anthropic가→Anthropic이)를 고친다. td `~/projects/telegram-digest/notation.py`와 바이트 동일 — 고치면 양쪽을 맞춘다.
  - 회사명 말고도 쓰이는 이름(아마존·애플·오라클·테슬라)은 **자동 치환하지 않는다.**
  - 코드블록·여러 줄 인용·태그·링크 주소·확장자가 있는 경로·H1·메타 표는 **글 전체 기준으로** 보호한다(줄 단위로 자르면 여러 줄 보호가 깨진다). 밑줄 볼드 `__X__`는 경로로 보지 않는다. 한국어 원제 영상의 제목도 건드리지 않는다.

## 인명 원문 표기·소개

- 요약 저장 직후 `name_fix.schedule`이 뒤에서 공용 `~/projects/hermes-llm-log/name_resolver.py`로 인명을 찾고, 처음 보는 인물은 웹 검색으로 영문 철자·소개를 확인한다. 끄려면 `NAME_FIX=0`.
- **출처 사이트 2곳 이상의 인용에 그 철자가 있을 때만** 한글 음차를 바꾼다(성만 쓴 곳은 성만). 바꾸면 제목 바로 앞에 `<!--NAME_FIX: 원래→바뀐-->` 흔적을 남긴다.
- 중국·일본 인명도 로마자로 바꾸고 외국인 한글 유지 예외는 없다. 한국인은 소개만 붙인다.
- 결과는 `~/.hermes/data/person_names.db`(yt·td 공유)에 쌓인다. **확인 실패는 영구다 — 다시 검색하지 않는다**(사용자 결정). 다시 등장한 인물의 소개만 30일마다 재확인하고(철자 고정), 소개 창·블로그 팝업에 'YYYY.MM 확인'을 붙인다.
- **증권사·리서치 애널리스트는 국적과 무관하게 인명 소개에 넣지 않는다**(`persons.analyst=1`, 사용자 결정). 한국인 애널리스트는 검색도 하지 않고, 외국인은 철자 교정을 위해 검색은 한다.
- **제목·메타정보는 교정 대상이 아니다**(yt·td 공통): `name_fix.process`는 `## 2.`(없으면 `## 3.`) 앞은 그대로 두고 본문만 바꾼다. 파일 쓰기는 캡처 주입과 같은 잠금(`document_io.summary_lock`)을 쓴다.
- **화면 소개**(`YS.decoratePeople`, `/summary/content`의 `people`): 한 글에서 같은 인물은 첫 등장 한 번만 점선 밑줄, 누르면 그 낱말 곁에 어두운 소개 창.
- 소개 링크가 본문 첫 등장에 붙도록 다음은 건너뛴다: h1, 메타정보 표(`data-person-skip`), 제목 머리 영역(`.sum-head-sticky`·`.sum-title-orig`·`.ys-meta`), 한눈 요약, 소제목 h2~h6(`_PERSON_SKIP`, 화면·블로그 공통).
- 창의 '인명 소개 제외'는 그 인물을 yt·td 소개에서 모두 뺀다(`POST /people/excluded`, `persons.excluded`, 되돌리기 가능 — 철자 교정은 그대로).

## 전사

- **whisper 1차 기본값**(`yt_transcribe._run_whisper`): 컨텍스트 `_FIRST_CONTEXT`(64)·`_NO_SPEECH_THOLD`(0.6)·VAD(`whisper.cpp/ggml-silero-v6.2.0.bin`, 파일이 없으면 VAD 없이 돈다).
- 1차가 붕괴하면 컨텍스트 재전사를 건너뛰고 바로 구간 분할로 간다. **구간 분할은 VAD를 끈다**(`--offset-t`/`--duration`이 VAD로 줄인 타임라인에 적용되기 때문).
- VAD 결과는 무음만큼 `_coverage_ratio`가 낮지만, 커버리지 판정은 붕괴 재전사 결과(VAD 없음)에만 쓰여 조정이 필요 없다. 알려진 한계: 음악 위로 읽는 크레딧을 VAD가 자를 수 있다.
- 비상 스위치(예전 경로): `WHISPER_VAD=0`·`WHISPER_FIRST_CONTEXT=-1`·`WHISPER_NO_SPEECH=0.8`. 다른 VAD 모델은 `WHISPER_VAD_MODEL`.
- **반복 붕괴 판정**(`_looks_collapsed`) — 하나라도 걸리면 재전사한다. 붕괴가 그대로 저장되면 중복 제거 때문에 본문이 몇 줄로 줄어든 채 '정상 완료'로 보인다.
  - 같은 문장 연속 50회, 또는 연속 120초(`_COLLAPSE_MIN_SECS` — 세그먼트가 길면 횟수로는 못 잡는다).
  - 같은 문장 연속 반복 안에 25초 이상 세그먼트가 2번 이상(`_COLLAPSE_LONG_SEG`·`_COLLAPSE_LONG_REPS`).
  - 서로 다른 2~6문장 묶음이 그대로 되풀이(`_repeat_cycle`)되기를 20회 이상·60초 이상(시각 없는 출력은 50회). 맞장구 사이 문장이 매번 다르면 묶음이 아니다.
  - 무음·음악 정형 환각(`_HALLUCINATION_LINE_RE`·음악 표시·`_SUBTITLE_CREDIT_RE`)은 세지 않는다.
- **말 없는 영상 거르기**(`yt_transcribe._speech_body`): 전사 글자수 기준(분당 `MIN_CHARS_PER_MIN` 40자)을 셀 때 시각 마커, 음악·효과음 표시, whisper가 무음 구간에 지어내는 정형 문장 줄(줄 전체가 그것뿐일 때만)을 뺀다.
- 1분 미만 영상은 실제 말이 `MIN_SHORT_CHARS`(10자) 미만이면 실패 처리한다. 사유에 '음성 없는 영상'을 넣어 채널 모니터가 영구 실패로 보고 재시도하지 않는다.
- **전사 모델은 벤치마크 순위만 보고 바꾸지 않는다.** 수동 자막이 있는 실제 yt 영상으로 CER·WER을 같은 하네스(`~/projects/llm-ab-harness/tools/asr_*.py`)로 잰다. Qwen3-ASR·Nemotron Transcribe는 이렇게 재서 보류했다(한국어 열세).
- **다운로드 오디오 90일 보관**(사용자 결정): 전사 성공 후 mp3를 `res/audio_archive/YYYYMMDD/`로 옮기고(`yt_jobs._archive_audio`) 그때마다 90일 지난 파일을 지운다(`_prune_audio_archive`). 기한은 전사 시각 기준이다(옮길 때 파일 시각을 지금으로 맞춘다).
  - 실패한 잡의 오디오는 계속 지운다. `/files` 목록은 res 아래 한 단계만 훑어 보관 파일이 섞이지 않는다.
  - 조정: `AUDIO_RETENTION_DAYS`(0 = 즉시 삭제)·`AUDIO_ARCHIVE_DIR`·`KEEP_AUDIO=1`(원래 자리에 영구 보존). 보관 전 영상은 `tools/backfill_audio_archive.py --days N`으로 채운다.
  - whisper 설정 A/B는 이 보관본으로 한다.
- **전사 번역**(`transcript_translator`, 검사 함수는 `translation_guards`)은 청크마다 두 가드를 거친다. 교정 호출이 실패해도 번역은 그대로 저장한다.
  - **줄 반복 루프**: 같은 긴 줄(15자+)이 5회 이상이면 폭주로 보고 재시도한다(원문 자체가 반복이면 아니다). 재시도도 루프면 반복 적은 쪽을 고르고 연속 반복 줄을 접는다.
  - **중국어·가나 누출**(`han_leaks`): 섞인 줄만 모아 한 번 교정한다(`purpose=repair`). 원문에 있던 조각·한글 뒤 괄호 병기·한글 사이에 끼지 않은 한 글자는 정상이다.
  - 교정본은 한자가 없고 **감지한 한자 구간과 그 뒤 한글 어미·괄호 풀이·경계 띄어쓰기 밖이 한 글자도 안 바뀌었을 때만** 받는다(`_only_leaks_replaced`). 앞말은 넓히지 않는다(가나만 예외).
  - 단순 삭제는 거부하고 원래 줄을 유지한다(대체어는 한글이 늘어야 한다). 원래 구간에 없던 숫자·영문·문장부호를 들여와도 거부한다. 대체어의 뜻이 맞는지는 코드로 판정하지 못한다.
- 전사 번역은 앞 청크의 영문 고유명사 목록(최대 40개)을 다음 청크에 물려 표기를 잇되, 이번 원문에 더 분명한 철자가 있으면 원문을 우선한다.

## 캡처(키프레임)

- **캡처 전용 모델 설정은 없다**(사용자 결정). `/keyframes`가 순서를 받지 않으면 `yt_models._capture_order`가 요약 파일의 `SUMMARY_MODEL` 마커로 그 영상을 요약한 모델을 먼저 쓰고, 실패하면 요약 슬롯 순서(중복 제거, vision 가능 모델만)로 넘긴다. 자동모니터·수동 캡처·재요약 배치가 모두 이 경로다.
- `monitor_settings.capture_models` 열은 남아 있지만 읽지 않는다. **캡처에는 추론 수준을 두지 않는다**(판정은 같고 시간만 는다).
- **비전에서 Claude Opus는 Sonnet으로 바꿔 부른다**: `_capture_order`가 Opus를 `VISION_OPUS_TO`(env `YT_VISION_OPUS_TO`, 기본 claude-sonnet-5-5)로 바꾼다. 순서를 못 받았거나 요약 슬롯에 vision 모델이 없으면 `VISION_MODEL`(claude-sonnet-5-5). 되돌리기는 `YT_VISION_OPUS_TO=`(빈 값)·`VISION_MODEL=opus`.
- 비전 Claude CLI 호출은 `DISABLE_PROMPT_CACHING=1`·`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` 환경으로 돈다(`_vision_claude_env`). 이미지 Read 도구가 필요해 cli_minimal 인자는 붙이지 못한다. 비상 스위치 `HERMES_CLI_NO_CACHE=0`·`HERMES_CLI_AUTO_MEMORY=1`. 이미지를 `-i`로 첨부하는 Codex 호출에는 최소 구성을 붙인다.
- 비전 입력 토큰은 이미지가 대부분이다. 모델 간 비교는 input+cache_read+cache_write 합으로 한다.
- **장면전환 프레임은 장면이 끝나기 직전(다음 장면 0.7초 전) 프레임으로 바꾼다**(`_settle_scene_frames`) — 슬라이드는 제목만 띄운 채 시작해 채워진다. 2초 미만 장면은 그대로, 끄려면 `SCENE_SETTLE=0`. 교체는 **후보 상한 뒤에 뽑힌 장면에만** 한다(전 장면을 select 식 하나에 넣으면 ffmpeg가 프레임을 하나도 내지 않는다).
- **장표 완성 시점으로 옮기기**(`_settle_unfinished_frames`): 영상 전체를 1초 간격으로 훑어 정지 구간을 찾고, 다음 정지 화면에 기존 내용이 80% 이상 남으면 같은 장표로 보고 최대 `HOLD_MAX_WAIT`(75초)까지 따라가 그 끝 프레임으로 바꾼다.
  - 움직이는 화면에서 첫 정지 화면까지 3초를 넘으면 그 둘도 80% 이상 겹쳐야 옮긴다(없으면 인물 프레임이 다른 슬라이드로 바뀐다).
  - 같은 정지 구간의 프레임은 75초 간격으로 한 장만 남긴다. 분석 실패 시 원래 후보를 쓰고, 끄려면 `HOLD_SETTLE=0`.
  - 알려진 한계: 단계마다 내용이 바뀌는 장표는 중간 단계 화면이 다음 단계로 밀릴 수 있다.
- 균등 후보는 `fps=1/N`이 아니라 `select`+`showinfo`로 실제 시각을 읽는다(`_uniform_select`). 같은 초 프레임은 점수 높은 한 장만 남긴다(`_dedupe_same_second`).
- 시각 단서 정규식 오탐(발표·목표·`look at`)은 고치지 않기로 했다.
- 소제목당 캡처 상한은 `MAX_FRAMES_PER_SECTION`(4)이다. 소제목 개수 규칙을 바꾸면 영상당 캡처 수가 줄지 않게 함께 본다.
- **캡처에는 캡션을 달지 않는다**(사용자 결정). 캡처 스트립(`_augment_summary_md`)은 `figcaption` 없이 비전 모델의 설명을 `alt`로만 남긴다. 옛 요약의 캡션은 공용 CSS(`.kf-strip figcaption`)로 숨기고 라이트박스에도 띄우지 않는다.
- **다운로드 재시도 틀은 `yt_download.py` 한 곳**이고 순서는 호출처가 정한다: 캡처 영상은 대체 클라이언트(tv_simply·web_embedded) → 기본 → 75초 뒤 기본, 전사 오디오는 기본 → 새 세션 → 대체. 따로 실측해 맞춘 값이라 합치지 않는다.
- 캡처 이미지(`/sframe/`)는 로드에 실패하면 `common.js`가 0.8·1.6·2.4초 간격으로 3번까지 다시 받는다(원격에서 여러 장을 동시에 받다 실패하면 브라우저는 다시 받지 않는다).

## 채널 감시와 큐

- **중복 정리는 요약까지 끝난 이력에만 적용한다**(`db.has_summary_for_yt_id`). 전사만 남은 반쪽 이력을 중복으로 닫으면 요약이 영영 생기지 않는다.
- 요약 대기시간은 `MONITOR_SUMM_TIMEOUT`(기본 1800초, 사용자 결정)이고 영상 처리 주기 10분과는 별개다.
- **광고 영상은 건너뛴다**: 새 영상 제목에 광고 표기 태그가 있으면 `skipped`(사유 `ad`)로 둔다(`is_ad_title` — 괄호로 감싼 AD·광고·유료광고·Sponsored, 해시태그 `#광고`; 'ADR'·'AD램'은 걸리지 않는다). 수동 큐는 막지 않는다.
- **라이브는 건너뛴다**: 자동 감지분은 예정·진행 중·끝난 다시보기(`is_live`·`post_live`·`was_live`)를 모두 `skipped`(사유 `live_skip:…`, error_kind `live_skipped`)로 확정 제외한다(`filter_verdict`·`_defer_policy`). 필요한 라이브는 수동 등록한다.
- 수동 등록분(`channel_id='manual'`)은 방송이 끝나기를 기다렸다가 다시보기를 처리한다. 프리미어는 라이브가 아니라 공개를 기다린 뒤 처리한다.
- **자동수집을 껐다 켜면 그동안 쌓인 영상은 제외한다**: `db.set_channel_enabled`가 `channels.resumed_at`을 남기고, `poll_channels`는 그보다 이른 미처리 영상을 `skipped`(사유 '수집 재개 전 영상')로 기록만 한다. 끄기 전에 이미 `pending`이던 영상은 그대로 처리된다.
- **RSS 대체**(`yt_listing.py`): YouTube RSS가 실패한 채널만 `/channel/<UC>/videos`를 Accept-Language ko로 읽어 대신한다. **이미 아는 영상 앞까지만** 새 영상으로 본다(`until_known`, 기준이 없으면 넣지 않음 — 백필 방지). 게시일은 근사값이고 Shorts 탭은 읽지 않는다.
- **한국어 제목**: 외국어 영상의 `title_ko`는 이 목록의 한국어 제목이 1순위이고, 없을 때만 LLM으로 번역한다(`yt_titles.translate_pending_titles`). 번역에 한글이 없거나 원제와 같으면 원제만 쓰고, 실패하면 NULL로 둬 다음에 다시 한다. 원제 `title`은 바꾸지 않는다. 한국어판 썸네일이 목록에 있으면 우선 저장한다.
- **채널명**: `channels.title`은 한국 시청자가 보는 이름(Accept-Language ko의 og:title)에 맞춘다. `items.uploader`와 다를 수 있으므로 **이름이 아니라 채널 주소로 잇는다**. `handle`은 @ 없이 저장한다.
- 목록·필터·검색의 채널명은 `/history` 응답에서 UC id로 등록 채널을 찾아 `channels.title`로 바꿔 보인다(`db._apply_channel_title`). DB의 `items.uploader`는 그대로 둔다(블로그 라벨·증류 제외 매칭이 쓴다).
- **채널 추가**: 설정 > 채널 탭 → `POST /channels {handle}`(@핸들·채널 주소·UC… ID). 핸들은 `resolve_channel_id`(채널 페이지 canonical 우선 — 추천 채널 ID 오인 방지)로 찾는다. 없는 채널 404, 이미 있으면 409.
- 새 채널은 첫 확인 때 지금 피드를 '본 것'으로만 기록한다(백필 방지). 채널별 수집 길이 하한은 `min_duration`(기본 180초)이다.
- **수동 큐 등록은 같은 채널에 지난번 넣은 캡처·증류 설정을 기본값으로 따른다**(`db.last_queue_prefs_for_channel`): 캡처는 최근 큐 항목의 명시값, 증류는 `items.distill` → 큐 값 → 모니터 채널 기본 순, 이력이 없으면 둘 다 켜짐. `/queue/preview`가 `prefs`로 내려주고, `/queue/items` POST도 값이 없으면 서버가 같은 규칙으로 채운다.

## 블로그 발행

- **발행 메뉴**(`YS.openBlogMenu`): 발행된 요약의 버튼은 `발행됨`이다. 머리 한 줄 `MM.DD HH:MM 게시`, 항목은 '블로그에서 보기', '지금 내용으로 수정'(꼬리표 `MM.DD HH:MM 변경`·`형식 변경`, 바뀐 것이 없으면 비활성), '삭제'(빨간 글씨).
- **수정 활성 조건**(`yt_blog._blog_state`): 발행 이후 내용이 바뀌었거나(요약 파일 mtime·`summary_notes.updated_at`) 발행 당시 렌더 버전(`items.blog_render_ver`)이 현재 `BLOG_RENDER_VERSION`과 다를 때. 발행시각 기록이 없는 옛 글은 수정 가능으로 본다.
- **태그 `유튜브` 필수**(2026-11-20 사용자 지시): 새 발행은 채널명 태그 앞에 `유튜브`를 붙이고(`yt_blog._with_blog_tag`), 수정 때는 기존 태그에 없을 때만 붙인다(`_blog_labels_for_update`, 태그를 못 읽어도 본문 수정은 진행). wm은 `뉴스`.
- 수정은 `/history/publish-blog`에 `mode="update"`로 기존 `blog_post_id`의 제목·본문을 덮어쓴다(URL 유지, `blog_published_at` 갱신). 이미 발행된 글을 자동으로 갱신하지 않는다.
- **블로그 본문 표시 형식을 바꾸면 `yt_blog.BLOG_RENDER_VERSION`을 올리고 서버를 재기동한다.** 안 올리면 발행된 글이 옛 모양으로 남고 수정 버튼도 비활성이라 되돌릴 방법이 없다.
- **삭제**: `POST /history/delete-blog`가 `hermes_blogger.delete_post` 후 `db.clear_blog_publish`로 발행 기록을 비운다. 블로거에서 이미 지워진 글(404)과 post id 없는 옛 기록은 기록만 비우고, 그 밖의 실패는 기록을 둔다(앱과 블로그가 어긋나지 않게).
- 발행·수정·삭제 확인은 `confirm()` 대신 공용 팝업 `YS.askConfirm`(wm 발행 팝업과 같은 배치)을 쓴다. 확인 버튼은 `--highlight`, 삭제만 `--error`.
- **제목**: 외국어 영상에 `title_ko`가 있으면 글 제목은 그 제목이고, 본문 맨 아래 푸터(원본 영상 링크 위)에 `원제 : {원래 제목}`을 한 번 둔다. 한국어 원제이거나 번역과 같으면 원제 행을 만들지 않는다. 서버 발행 경로가 최종 보장한다.
- **썸네일**: 발행·수정 직전 서버(`yt_blog._insert_blog_thumbnail`)가 영상 썸네일을 R2(`hermes_blogger.r2`)에 올려 본문 맨 위에 `<div data-ys-thumb="1">`로 넣는다. 고해상도(`maxresdefault`)가 안 되면 `res/thumbs/` 사본을 쓴다. **data URI·i.ytimg.com 직링크 금지**(wm과 같은 원칙). R2 실패면 썸네일 없이 발행한다.
- **모양은 wm과 맞춘다**: 소제목(`_BL.h3`) 18px 굵게·아이콘·형광펜 없음, 본문(`_BL.p`·`_BL.li`) 15px, 원제·원본 영상은 푸터(`data-summary-footer`), 썸네일은 맨 앞. wm 쪽은 `static/dashboard-blog.js`의 `BLOG_HEAD_CSS`·`BLOG_LI_CSS`·`xBlogHtml`. 한쪽을 바꾸면 다른 쪽도 바꾼다.
- **블로그 테마는 `.post-body p, .post-body div`의 margin을 `8px !important`로 고정한다.** 간격은 padding이나 span(display:block)으로 준다.
- **인명 소개**: `mdToBloggerHtml(md, {people})`가 앱과 같은 규칙(`_wrapPeopleNames` 공용)으로 인명을 span으로 감싸고, 글 끝 인라인 `<script>` 하나(`_BLOG_PEOPLE_JS`)가 클릭 팝업을 띄운다.
  - 리스너는 글이 여럿이어도 한 번(`window.__ypInit`)이고, 블로거 편집기가 바꾸는 `&`·`<`를 스크립트에 쓰지 않는다. 스크립트가 못 도는 곳은 span의 `title` 툴팁이 대신한다.
  - 인명 목록은 발행·수정 직전에 `YS.blogPeople(item)`로 새로 받는다(실패하면 인명 없이 발행). 블로그에는 '제외' 버튼이 없다.
- **메모**: 서버의 최신 저장 메모를 이스케이프한 텍스트와 인라인 스타일로 넣는다. 블로그 본문은 인라인 style만 먹으므로 💡는 문자 자체를 앞에 넣는다.
- 한눈 요약, 소제목 편집 버튼·편집창, 섹션 래퍼는 발행 HTML에 넣지 않는다.

## 화면

- **크기 토큰(2026-11-20 정리)**: `index.css`·`mobile.css`·`collection.css`의 글자 크기는 `--fs-2xs`(10px)·`--fs-xs`(11)·`--fs-sm`(12)·`--fs-md`(13)·`--fs-lg`(14)·`--fs-xl`(15) 6단계, 모서리는 `--radius-xs`(4)·`--radius-sm`(6)·`--radius`(8)·`--radius-lg`(12)와 알약(999px)만 쓴다. 9~15px 글자 크기나 px 반경을 직접 적지 않는다(16px 이상 제목·입력칸, 본문의 rem·`--reader-scale` 계산식은 그대로). 토큰은 두 파일의 `:root`에 각각 있다(폰 화면은 `index.css`를 싣지 않는다).
- **색상 모드**: PC·모바일 모두 공용 `hermes-theme`(시스템·라이트·다크, localStorage `hermes-theme`)을 쓴다. `static/js/hermes-theme.js`·`static/css/hermes-theme.css`는 wm 파일의 사본이니 함께 맞춘다. 다크 색은 `:root[data-theme="dark"]`에 두고, 공용 메뉴의 `--ui-*` 토큰은 yt 색 변수로 이어 둔다.
- **팝업이 열려 있는 동안 뒤 화면 스크롤을 막되 화면 배치는 건드리지 않는다**(wm·sa·td·yt·xw 공통). 스크롤 입력만 막는다. body를 `position:fixed`로 고정하거나 html/body에 `overflow:hidden`을 걸면 상단 고정 영역이 사라진다.
- **원격 접근**: 공용 본문 글꼴 경로 `/reader-fonts/`는 원격 허용(`yt_web._restrict_remote_access`)에 있어야 한다 — 빠지면 폰에서 모든 글꼴이 대체 글꼴로 보인다. 글꼴 검증은 Tailscale 주소로도 200을 확인한다.
- **설정 팝업은 3탭**: [모델](선택기 + 큐 현황) [채널] [각주 제외]. 마지막 탭은 localStorage `ytSettingsTab`(PC·모바일 공유). 프롬프트 팝업은 [요약 프롬프트][Grok 문체 보정] 2탭.
- **채널 탭**은 PC·모바일 공용 `static/js/channel-list.js`(`YS.channelList.mount`)가 그린다. 줄마다 로고(`/channels/logo/<UC…>`, `res/channel_logos/`에 30일 캐시), 이름·길이 뱃지, @핸들·처리 편수·마지막 처리일. 스위치 순서는 **자동 > 캡처 > 증류**, 자동이 꺼진 채널은 흐리게. 검색칸은 이름·@핸들·채널 ID를 낱말 AND로 거른다. '마지막 확인 시각'은 보이지 않는다.

### 요약 뷰어

- **HTML 파일로 보내기**(2026-11-17 사용자 지시 '상세팝업을 아는 사람에게 html 파일로'): 요약 팝업 하단(PC·모바일) 'HTML 파일' 버튼 → `YS.exportSummaryFile`(`common.js`). 팝업과 같은 렌더러로 본문을 만들고(제목 번역·목차 생략·섹션 구조, 한눈 요약은 펼친 상태), 캡처 이미지는 data URI로, 스타일은 `index.css`·`hermes-theme.css`·주입 스타일(`ys-kf-style`)을 그대로 실어 **파일 하나만으로 열린다** — 받는 사람은 이 서버에 들어올 수 없기 때문이다. 내 메모·편집 버튼·인명 소개·서버 주소 링크·서버 글꼴은 넣지 않는다. 원본 영상 링크와 만든 시각을 맨 아래에 붙인다. 휴대폰 공유 시트가 파일을 받을 수 있으면(보안 연결) 공유, 아니면 내려받기 — Tailscale `http://` 주소에선 공유 시트가 안 떠 파일로 저장된다. 좁은 폰(≤430px)에선 'HTML 파일'이 아이콘만 보인다.
- **요약 팝업 하단 버튼**(2026-11-18 정리): 읽음 처리 · 증류 ↪ 포함/제외 | HTML 파일 · 블로그 발행. '메타 갱신'(제목·썸네일을 유튜브에서 다시 가져오던 ↻ 버튼, `/history/refresh-meta`)은 사용자 지시로 서버 경로·`fetch_localized_title`·`clear_title_ko`·`save_thumbnail(force=)`까지 삭제했다 — 되살리려면 git 이력. 증류 버튼은 PC·모바일 모두 `증류 ↪ 포함`처럼 짧게 쓰고 근거(채널/기본 설정)는 툴팁에 둔다.

- **`###` 소제목**은 해당 섹션 안에서만 sticky다(제목 고정 영역 아래 8px) — 다음 섹션이 올라오면 밀려나야 한다. 저장 마크다운에는 섹션 래퍼를 넣지 않는다.
- **소제목 ✎ 편집**: 뷰어가 마크다운을 `###` 단위로 잘라(`YS.splitSummarySections`, 코드펜스 안은 제외) 원래 텍스트와 새 텍스트를 `/summary/section`에 보낸다. 서버는 원래 텍스트가 파일에 **정확히 한 번** 나올 때만 갈아 끼우고 재색인한다(아니면 409). 저장 후 각주·메모·sticky를 다시 붙인다.
- **섹션 메모**는 `summary_notes`에 전사 경로·소제목·동명 소제목 순번 기준으로 저장하고 재색인 시 유지한다.
  - 머리말 없이 본문만 두고 첫 글자 앞에 💡(`.summary-memo-text::before`). 모양은 **노란 메모지 카드**다 — 왼쪽 강조선+초록 바탕은 쓰지 않는다(사용자 결정).
  - 빈 내용 저장은 삭제이며 행은 `body=''`로 남긴다(행을 지우면 발행 뒤 삭제를 '변경 없음'으로 잘못 판정한다).
  - 저장·삭제는 `/summary/note` 응답의 `blog` 상태와 `ys:blog-state` 이벤트로 열린 발행 버튼에 바로 반영된다.
- 메타정보 날짜는 `YY.MM.DD`, 영상 길이와 압축률은 한 칩으로 묶는다(저장된 메타 원문은 그대로). 메타 칩은 본문 글꼴·작은 패딩으로 한 줄 우선 배치한다.
- 모바일 요약 헤더의 컨트롤(뒤로가기·YouTube·글꼴·글자크기)은 높이 32px로 통일한다.
- **폰 요약 읽기 모드**(2026-11-20): 아래로 읽어 내려가면 하단 버튼 줄을 숨기고 제목의 원제 줄을 접는다. 위로 올리거나 맨 위·맨 끝에 닿으면 다시 보인다(`mobile.js` `_updateReadingChrome`, `.modal.reading`). 접고 펼 때 스크롤 위치 보정으로 반대 방향 이벤트가 올 수 있어 바꾼 직후 0.3초는 판정을 쉰다.
- **세로 영상 캡처는 비율 유지**(2026-11-20 사용자 지시, yt 2076 쇼츠): 캡처가 세로(높이 > 폭×1.05)면 `figure.kf-portrait`를 붙여 높이 `min(420px, 62vh)`·폭은 비율대로 보인다(16:9 상자에 cover로 넣으면 위아래가 잘렸다). 표시는 `common.js`가 문서 캡처 단계 `load` 리스너 한 곳에서 붙여 PC·폰·모음 화면이 같고, 스타일은 `ys-kf-style`(+ `#sum-panel-body`·몰입형 덮어쓰기는 `index.css`).
- 요약 팝업 일반형 폭은 860px이다(캡처는 한 화면 2장). 몰입형(`.sum-panel.immersive`)은 별개다.

### 영상 모음

- **행사·시리즈 영상만 따로 모아 고르는 페이지**(2026-11-19 사용자 지시 '포츈500 포럼건 영상들만 따로 모아서'): `/collection/<slug>`(PC·폰 공용 한 장 `templates/collection.html` — 모음 전용 디자인, 이력 화면 `index.css`의 색·요약 본문 스타일을 함께 싣는다), 자료는 `/collection/<slug>/items`, **두 단계다: 모음 목록(`/collections`) → 모음** — 모음이 하나여도 목록을 거친다(같은 날 사용자 지시 '이런 유형이 추가될 수 있으니 중간 단계를 하나 더'). 모음 페이지의 뒤로는 모음 목록, 모음 목록의 뒤로는 이력 화면. 버튼은 PC 헤더 '영상 모음'(색상 모드 버튼 왼쪽), 폰 상단 바 첫 아이콘. 원격(폰) 허용 경로에 들어 있다.
- 모음 정의는 `collections_view.COLLECTIONS` 한 곳(제목에 들어갈 문구 `match`·재생목록·행사 현지 시간대 `tz`·행사 시작일 `start` — 'N일차'는 시작일부터 센다·선택 `schedule`). 지금 모음: 포춘 500 혁신 포럼(`fortune500-2026`), TomorrowX Summit(`tomorrowx-2026`, 같은 날 사용자 지시). **편집본을 행사 뒤에 몰아 올리는 행사는 업로드 시각으로는 일차가 틀린다** — 그때는 `schedule`에 공식 일정표의 무대 시각(현지 'YYYY-MM-DD HH:MM')을 영상별로 적고, 일정표에 없는 영상은 업로드 시각으로 두되 카드에 '업로드'라고 표시한다(`time_src`). TomorrowX는 새 편집본이 올라오면 `schedule`에 한 줄씩 더한다. 영상은 이력(items)과 처리 큐(처리 전·실패)에서 제목으로 모은다 — 제목에 행사 이름이 없는 강연은 `include`(yt_id 목록)로, 짧은 제목에서 떼는 꼬리가 `match`와 다르면 `tail`로 지정한다(TomorrowX: Astera Labs·Apollo 편집본, 2026-11-20). **TomorrowX 채널은 자동 감시 대상이 아니다** — 새 편집본은 재생목록을 보고 큐에 넣는다(사용자 확인 뒤). 공식 일정표는 tomorrowxsummit.com 첫 화면의 AGENDA(Day 2는 탭 클릭, JS 렌더라 브라우저로). 새 모음은 여기 한 줄을 더한다.
- **순서는 일자별·방송 시작 시각순**: 이력에는 게시일(날짜)뿐이라 같은 날 세션 순서를 못 정한다. 영상마다 yt-dlp `release_timestamp`(없으면 `timestamp`)를 한 번 받아 `res/collections/<slug>.json`에 두고(백그라운드), 행사 현지 날짜로 '1일차·2일차'를 묶는다. 시각을 아직 모르면 게시일로 그날 맨 뒤에 두고 화면이 15초 간격으로 몇 번 다시 묻는다.
- **보기는 모음 페이지 안이다**(2026-11-19 사용자 지시 '원래 페이지로 돌아와서 팝업이 뜨지 않게'): 카드를 누르면 `/collection/<slug>?v=<item_id>` — 왼쪽 사이드바에 영상 목록(일차별, 안읽음 점, 처리 전은 흐리게), 오른쪽에 요약을 페이지 안에서 연다(`static/js/collection.js`·`static/css/collection.css`). 본문은 요약 창과 같은 렌더(`YS.renderMarkdown`→제목 번역·목차 생략·섹션 고정·인명 소개·섹션 메모, 스타일은 `index.css`의 `.sum-panel-body`/`.sum-md`)이고, 글자 크기·글꼴은 요약 창과 같은 저장값(`sumReaderScale`·`sumReaderFont`)을 쓴다. 상단 막대: 이전·다음·글자 크기·글꼴 | YouTube·읽음 처리. 카드에서 열 때만 기록을 하나 쌓아(`colView`) 뒤로가기·‹·Esc가 카드 화면으로 돌아오고, 보기 안에서 영상을 옮길 때는 주소만 바꿔 쓴다. 폰(≤860px)은 요약만 보이고 목록은 ☰ 서랍. 이력 화면으로 넘겨 팝업을 띄우던 옛 배선(`YS.collectionDeepLink`·`_collectionReturn`)은 지웠다.
- **카드는 한 줄에 2개(폰도), 썸네일은 16:9 상자**(같은 날 사용자 지시): 그림은 `i.ytimg.com/vi/<id>/hqdefault.jpg`(4:3의 검은 띠는 `object-fit: cover`가 자른다), 못 받으면 서버 `/thumb`. **생중계 영상은 서버 썸네일이 방송 전 기본 그림으로 남아 있어** 모음 화면에선 YouTube 것을 먼저 쓴다. 카드에 번역 제목(`short_ko`, 원제에서 행사 이름을 뗐으면 번역 제목의 마지막 '| …'도 뗀다)을 함께 보인다.
- 폰 상단 바는 영상 모음·처리 큐·설정·색상 모드 4개다. 새로고침 버튼은 같은 날 사용자 지시로 지웠다(목록은 아래로 당겨서 새로고침).
- **공개 사본**(2026-11-19 사용자 지시 '포츈500 리스트 페이지랑 하위 페이지들을 무료로 공개', 방식은 사용자 선택 = 정적 사본): `COLLECTIONS`에 `"public": True`인 모음만(fortune500-2026, tomorrowx-2026 — 2026-11-20 추가) `collection_publish.py`가 읽기 전용 정적 페이지로 만들어 **hermes-blogger R2 버킷의 `c/` 아래**에 올린다. 시작 주소는 `<R2_PUBLIC_BASE_URL>/c/index.html`(지금 `https://pub-41d62db27ef3434ea946bb316d6e6a36.r2.dev/c/index.html`). 서버를 바깥에 여는 방식(Funnel·Tunnel)은 쓰기 기능·lan_guard 때문에 택하지 않았다.
  - 싣는 것: 모음 목록·모음(일자별·시간순, 썸네일은 `i.ytimg.com`)·영상별 요약(한눈 요약·본문·각주·원본 영상 링크). **싣지 않는 것: 전사·전문 번역·캡처 이미지(저작권), 내 메모·읽음 표시·버튼, 처리 전 영상.** 모든 페이지 `noindex`.
  - 요약 페이지는 화면과 같은 렌더러 `YS.buildSummaryHtmlFile`로 만든다: 헤드리스 크롬(`/Applications/Google Chrome.app`)이 yt 서버의 `/collection/<slug>/public-render`(공개 모음만, 요약 원문을 서버가 실어 줌 — `/summary/content`를 부르지 않아 뜻 판정 등 부작용 없음)를 `--dump-dom`으로 열어 결과를 받는다. **yt 서버가 떠 있어야 한다.** CSS 주석·이미지는 올리기 전에 지운다.
  - **모양은 yt 모음 화면과 같다**(2026-11-20 사용자 지시 '공개 페이지도 같은 모양으로'): 카드는 한 줄 2개·썸네일 16:9(`hqdefault`), 영상 페이지는 위 막대(모음으로·이전·다음·YouTube) + 왼쪽 영상 목록(현재 강조) + 오른쪽 요약, 본문 끝에 이전·다음 칸, 폰은 목록이 ☰ 서랍. 틀은 `templates/collection_public_viewer.html`을 `_finish_summary_page`가 내보내기 파일 `<body>` 바로 뒤에 끼운다(내보내기 파일은 문서 전체가 스크롤되므로 막대·목록은 fixed, 본문은 `body.pv-body` 여백으로 비킴). 색은 기기 설정(다크면 `data-theme=dark`). 본문의 `<img>`만 지우고 틀의 목록 썸네일은 남긴다.
  - r2.dev는 폴더 주소에 index.html을 붙여 주지 않는다 — 링크는 모두 파일 이름까지(`…/index.html`, `v/<yt_id>.html`).
  - 자동 갱신: 채널 모니터(launchd 20분) 주기 끝 `collection_publish.sync_if_changed()` — 서명(요약 파일 mtime·번역 제목·순서·제외 용어·화면 코드·`FORMAT_VERSION`)이 바뀐 때만 다시 만들고, 내용이 바뀐 파일만 올린다(`res/public/manifest.json`, 사본 `res/public/site/`). 실패는 로그만. 수동: `.venv/bin/python -m collection_publish [--force] [--local]`.
  - 공개를 끄려면 `public`을 빼는 것만으로는 이미 올린 파일이 남는다 — R2의 `c/<slug>/`도 지운다.

### 이력 목록

- **카드 구성(2026-11-20 사용자 승인 디자인 개선, PC·폰 공통)**: 제목은 번역 제목(`title_ko`, `/history` 응답에 실린다)을 크게, 원제를 아래 한 줄로(`YS.cardTitles`). 제목 검색은 원제·번역 제목 둘 다 본다. 읽은 카드는 썸네일·제목을 흐리게(빨간 점만으로는 구분이 안 됐다). 영상 길이는 `12:24`(`YS.fmtClock`), 날짜는 `26.11.20`(`YS.fmtDateShort`) — PC·폰·영상 모음·요약 메타가 같은 표기다. 날짜·번호·길이는 고정폭 글꼴 대신 본문 글꼴(`tabular-nums`).
- **카드 버튼**: 밖에는 읽음(폰은 읽음·북마크, PC 북마크는 썸네일 위)과 ⋯만 둔다. 전사 보기·YouTube에서 열기·삭제는 ⋯ 메뉴(`YS.openCardMenu`, 블로그 발행 메뉴와 같은 모양)다 — 요약 보기는 카드 클릭과 같아 뺐고, 삭제는 자주 누르는 버튼 옆에서 뗐다.
- **PC 상단**: 로고 줄 오른쪽에 큐·설정·프롬프트·영상 모음·색상 모드(`.hdr-tools`), 필터 줄에는 보기 조건(새로고침·채널·정렬·검색·증류만·북마크만·미읽음만·초기화)과 건수만 둔다. 켜진 필터는 색을 채운다. 이력/로그 탭 줄은 localhost에서만 보인다(원격은 `.remote-mode`가 숨김).
- **폰 목록**: 검색 줄 아래 한 줄(`.list-bar`)에 건수 · 켜진 필터 칩(누르면 해제) · 보기 전환(카드/목록, localStorage `ytMobileView`). 목록 보기는 썸네일을 왼쪽에 작게 두고 원제·번호·북마크 버튼을 숨긴다(북마크는 ⋯ 메뉴). 검색 줄의 켜진 필터 아이콘도 색을 채운다.
- 카드 좌하단 `#<item_id>`(내부 DB id)를 클릭하면 `yt 1234` 형식으로 복사한다. `common.js`의 캡처 단계 위임 리스너가 받아 카드 클릭으로 번지지 않게 한다.
- **채널 선택은 팝업**이다: PC는 `#hf-uploader-btn` → `#uploader-modal`, 모바일은 `#uploader-filter-btn` → 유틸 모달. 채널마다 미읽음 배지·전체 건수·안읽은 영상 길이 합계(`.up-dur`, `YS.fmtUnreadDur`), 맨 위 '전체 채널', 검색란.
  - PC의 필터 상태는 숨긴 `<select id="hf-uploader">`가 그대로 든다. 모바일은 `histFilter.uploader`(PC와 같은 키)이고 카드의 채널명 탭도 이 선택을 토글한다.
  - 정렬은 버튼 3개(`#uploader-sort`): 안읽은 개수(기본)·길이 합계·평균 길이, 선택은 localStorage `ytUploaderSort`. **규칙은 `common.js`의 `YS.uploaderSort` 한 곳이다.**
- **증류만 필터**(PC `#hf-distill-btn`, 모바일 `#distill-filter-btn`): 항목의 `distill`은 증류 파이프라인(`~/.hermes/scripts/distill/source_repository._doc_from_path`)과 같은 규칙이다 — `items.distill` 오버라이드가 있으면 그것, 없으면 채널명이 `db.distill_excluded_names`에 걸리면 제외(`db._history_distill`). **파이프라인 규칙을 바꾸면 이 함수도 맞춘다.**
  - 영상 게시일 7일 이내(게시일이 없으면 전사 처리일 — `YS.withinRecentDays`)도 함께 건다. 선택은 `histFilter.distill`.
- 채널 이름이나 증류 설정이 바뀌면 `history_revision`이 바뀌어 목록을 다시 받는다.
- **삭제 되돌리기**(sa·wm과 같은 방식): 확인창 없이 카드를 바로 빼고 토스트 '삭제했습니다 · 되돌리기'를 6초 띄운다. yt 삭제는 파일까지 지워 복원할 수 없으므로 **서버 삭제 요청은 확정 때 보낸다**(`YS.deleteWithUndo` — 6초 경과·다음 토스트·페이지 이탈 중 먼저 오는 때, `apiDeleteItem`은 keepalive).
  - 대기 중인 항목은 목록을 다시 받아도 숨긴다(`YS.isPendingDelete`). 안읽음만 보기에서 읽음 처리로 사라진 카드도 되돌리기 토스트(`YS.undoToast`)를 띄운다. 토스트는 한 번에 하나다.
  - **시험할 때 주의**: 6초 안에 되돌리기를 누르지 않으면 실제로 지워진다.

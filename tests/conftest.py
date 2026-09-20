"""pytest 공통 설정 — 테스트가 실제 데이터/DB를 건드리지 않도록 import 전에 환경 격리.

  - 저장소 루트를 import 경로에 추가
  - DB_PATH를 임시 파일로 지정(db.py가 import 시 DB_PATH를 읽으므로 import보다 먼저 설정해야 함)
"""
import os
# 실 DB(~/.hermes/data/llm_calls.db) 보호: 테스트 중 mock된 LLM 호출이 이력을 남기지 않게 한다.
# 배선 테스트는 monkeypatch로 HERMES_LLM_LOG_DB(임시)와 HERMES_LLM_LOG_DISABLED=""를 켠다.
os.environ.setdefault("HERMES_LLM_LOG_DISABLED", "1")
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(prefix="yts_test_"), "test.db"))

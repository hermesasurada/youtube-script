"""pytest 공통 설정 — 테스트가 실제 데이터/DB를 건드리지 않도록 import 전에 환경 격리.

  - 저장소 루트를 import 경로에 추가
  - DB_PATH를 임시 파일로 지정(db.py가 import 시 DB_PATH를 읽으므로 import보다 먼저 설정해야 함)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(prefix="yts_test_"), "test.db"))

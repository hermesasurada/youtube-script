"""중앙 LLM 카탈로그(`~/projects/hermes-llm-log/llm_catalog.py`)를 못 불러올 때의 대체 구현.

카탈로그는 모델 목록·라벨·지원 추론 레벨을 알려 주는 **부가 정보**다. 모듈이 옮겨지거나
문법 오류로 깨져도 요약·번역·캡처가 멈추면 안 된다(사용자 원칙: 부가 기능이 본업을
실패시켜서는 안 된다). 그래서 같은 이름의 API를 '아무것도 모르는' 상태로 흉내 낸다.

- 모르는 모델도 막지 않는다: `valid()`는 참, 선택지 멤버십(`in`)도 참 → 저장된 설정 유지.
- 추론 레벨은 전부 허용(`levels()`) → 저장된 추론값을 기본값으로 되돌리지 않는다.
- `resolve()`는 None → 호출부는 저장된 모델 ID를 그대로 쓴다.
- 설정 화면 선택지는 저장값만 보여 준다(새 모델을 고를 수는 없다).

이 파일은 서비스 저장소마다 **같은 내용으로** 둔다(카탈로그 저장소가 없어도 살아야 하므로).
"""
from __future__ import annotations

from collections.abc import Sequence

AVAILABLE = False
LEVEL_LABELS = {'default': '모델 기본값', 'none': '없음', 'minimal': '최소',
                'low': '낮음', 'medium': '보통', 'high': '높음', 'xhigh': '매우 높음',
                'max': '최대', 'ultra': 'Ultra', 'on': '켜짐', 'off': '꺼짐',
                'no-think': '추론 끔', 'thinking': '추론 켬'}
PROVIDERS = {'claude': 'Anthropic', 'codex': 'OpenAI', 'grok': 'xAI', 'local': '로컬 LLM'}
WARNING = '중앙 LLM 카탈로그를 불러오지 못해 대체 모드로 동작 중(저장된 설정 유지)'


def read():
    return {'revision': 0, 'models': [], 'warning': WARNING}


def models(provider=None, capability=None, include_disabled=False, keys=None):
    return []


def resolve(key, provider=None, target=None):
    return None


def model_name(model):
    return str(model or '')


def label(key, provider=None):
    return model_name(key)


def levels(key, provider=None):
    return list(LEVEL_LABELS)


def valid(key, effort='default', provider=None, capability='text', selectable=False):
    return bool(key)


def options(provider=None, capability='text', keys=None, selected=()):
    out, seen = [], set()
    for key in selected or ():
        if key and key not in seen:
            seen.add(key)
            out.append(dict(value=key, label=f'{key} (카탈로그 없음·기존 선택)', provider=provider,
                            reasoning=list(LEVEL_LABELS), enabled=True))
    return out


def reasoning_options(values=None):
    return [dict(value=v, label=LEVEL_LABELS.get(v, v)) for v in (values or list(LEVEL_LABELS))]


def describe_request(model, provider=None, reasoning=None, host=None):
    return dict(catalog_revision=0, requested_model=model, catalog_key=None,
                requested_reasoning=reasoning)


class Choices(Sequence):
    """비어 있지만 멤버십 검사는 통과시키는 선택지 — 저장된 모델을 버리지 않는다."""

    def __init__(self, *args, **kwargs):
        pass

    def values(self):
        return ()

    def __getitem__(self, index):
        return ()[index]

    def __len__(self):
        return 0

    def __iter__(self):
        return iter(())

    def __contains__(self, item):
        return isinstance(item, str) and bool(item.strip())

    def __add__(self, other):
        return tuple(other)

    def __radd__(self, other):
        return tuple(other)


class ExecutorChoices(Choices):
    pass

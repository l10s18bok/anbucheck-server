"""대상자 별칭 정규화 — 저장(subject_service)과 렌더(push_service) 공통 규칙.

별칭은 보호자가 자유 입력한 값이고 Push 본문에 그대로 실리므로, 저장 시점과
렌더 시점 양쪽에서 같은 규칙으로 거른다(이중 방어). 규칙이 두 곳에 복사돼
있으면 한쪽만 바뀌었을 때 "DB에는 20자로 들어갔는데 렌더는 다른 기준"처럼
조용히 갈라지므로, 이 모듈이 유일한 권위다.
"""

# 클라 입력단(LengthLimitingTextInputFormatter(20))과 맞춘 값.
# 이 값을 바꾸면 앱 쪽 입력 제한도 같이 바꿔야 사용자가 잘림을 나중에 발견하지 않는다.
ALIAS_MAX_LEN = 20


def clean_alias(raw: str | None) -> str | None:
    """제어문자·개행 제거 → 연속 공백 축약 → 20자 절단. 빈 값이 되면 None.

    None을 돌려주면 호출부는 별칭 없는 정형 문구로 폴백한다(하위호환).
    """
    if not isinstance(raw, str):
        return None
    cleaned = " ".join("".join(c for c in raw if c.isprintable()).split())
    return cleaned[:ALIAS_MAX_LEN] or None

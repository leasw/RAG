"""대화 -> 사실 문장 추출.

mem0의 FACT_RETRIEVAL 방식을 조직지식 도메인에 맞게 고쳤다. 원본 프롬프트는 개인
취향(좋아하는 영화, 식단)을 뽑도록 돼 있어서 이 프로젝트에는 맞지 않는다. 여기서는
과제 진행에서 나중에 다시 참조될 만한 것 — 결정, 일정, 수치, 담당, 미결 — 을 뽑는다.

하는 일은 하나다.

    extract    대화 턴 -> 사실 문장 배열. 남길 게 없으면 빈 배열.

**한 레코드에 한 사실**이 원칙이다. 나열은 항목마다 쪼개고, 두 사실을 연결어미로
잇지 않는다.

이 원칙이 중요한 이유는 셋이다.
- 조회수가 사실 단위로 쌓여야 승격/폐기 판정에 해상도가 생긴다. 네 가지 개선 항목이
  한 문장에 뭉쳐 있으면 그중 하나만 조회돼도 넷이 같은 점수를 받는다.
- 검색 벡터가 사실 하나를 가리켜야 정밀하다. 여러 사실이 한 벡터로 평균화되면
  질의에 걸린 사실 외에 나머지가 노이즈로 딸려 온다.
- 나중에 사실 하나가 정정될 때 그 레코드만 갈아끼울 수 있다.

승격 시 압축(여러 사실을 LLM으로 합치기)은 하지 않는다. 무엇을 합칠지 판단할 검증
가능한 기준이 없었기 때문이다 — 실측에서 병합해야 할 쌍(코사인 0.789)보다 병합하면
안 되는 쌍(0.799)이 더 높게 나와 유사도로는 갈리지 않았고, 결국 LLM 프롬프트의
재현 불가능한 판단에 의존하게 된다. 사실은 원자 상태 그대로 계층만 옮긴다.
"""

from __future__ import annotations

import json
import re
from typing import Any

# 사실 한 문장의 상한. 이보다 길면 원자 사실이 아니라 여러 사실이 뭉친 것이다.
MAX_FACT_CHARS = 200

EXTRACT_PROMPT = """너는 조직지식 에이전트의 메모리 관리자다.
대화에서 **나중에 다시 참조될 사실**만 문장 단위로 뽑아낸다.

뽑을 것:
- 결정된 사항과 그 이유
- 일정, 기한, 마감
- 금액, 수치, 목표치 같은 구체적인 값
- 담당자와 역할
- 미결 사항과 그 이유 (누가 무엇을 아직 못 정했는지)
- 사용자가 반복해서 신경 쓰는 관심사

뽑지 말 것:
- 인사, 잡담, 감사 표현
- 일반 상식이나 개념 설명
- 지시대명사만 있고 무엇을 가리키는지 알 수 없는 내용
- 어시스턴트가 제안했을 뿐 사용자가 받아들이지 않은 내용

규칙:
- **한 문장에 하나의 사실만 담는다.** 이것이 가장 중요한 규칙이다.
- 항목이 여러 개 나열되면 항목마다 별도 문장으로 쪼갠다. 하나로 묶지 않는다.
- "~하며", "~하고", "~인데" 같은 연결어미로 두 사실을 잇지 않는다. 문장을 나눈다.
- 문장만 읽어도 뜻이 통해야 한다. "그거", "아까 그 파일" 같은 표현은 실제 대상으로 바꿔 쓴다.
- 입력 언어를 그대로 따른다(한국어 대화면 한국어로).
- 남길 것이 없으면 빈 배열을 반환한다. 대부분의 잡담은 빈 배열이 정상이다.
- 반드시 {"facts": ["문장1", "문장2"]} 형식의 JSON만 출력한다.

예시)
입력: 사용자 "안녕" / 어시스턴트 "안녕하세요"
출력: {"facts": []}

입력: 사용자 "이월 금액 15,955천원으로 확정했어. 사유는 채용 지연이야."
출력: {"facts": ["가천대 인건비 이월 요청액을 15,955천원으로 확정했다", "이월 사유는 채용 지연으로 인한 인건비 미집행이다"]}

입력: 사용자 "방위 안내 기능 2차년도에 넣을까?" / 어시스턴트 "협약변경이 필요합니다"
출력: {"facts": ["장애물 방위 안내 기능을 2차년도 목표에 넣을지 검토 중이다", "정량목표 변경에는 협약변경 절차가 필요하다"]}

입력: 사용자 "설문에서 유선 불편, 착용 부담, 알림음 간격, 방위 안내 이렇게 나왔어"
출력: {"facts": ["시범서비스 설문에서 유선 연결이 불편하다는 의견이 나왔다", "시범서비스 설문에서 장시간 착용 부담 의견이 나왔다", "시범서비스 설문에서 알림음 간격 조절 요구가 나왔다", "시범서비스 설문에서 장애물 방위 안내 요구가 나왔다"]}
"""

def _parse_facts(content: str) -> list[str]:
    """모델 응답에서 facts 배열을 꺼낸다. 코드펜스가 붙어 와도 견딘다."""
    text = (content or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    facts = data.get("facts")
    if not isinstance(facts, list):
        return []
    out = []
    for f in facts:
        s = str(f).strip()
        if s and len(s) >= 4:
            out.append(s[:MAX_FACT_CHARS])
    return out


class FactExtractor:
    """LLM으로 대화에서 사실을 뽑는다. client는 AgentRuntime이 쓰는 것과 같은 것."""

    def __init__(self, client):
        self.client = client

    def _ask(self, system: str, user: str) -> list[str]:
        try:
            message = self.client.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                tools=[],
            )
        except Exception:  # noqa: BLE001 - 추출 실패가 대화를 막으면 안 된다
            return []
        return _parse_facts(message.get("content") or "")

    def extract(self, turns: list[tuple[str, str]]) -> list[str]:
        """turns: [(role, text), ...] -> 사실 문장 배열."""
        if not turns:
            return []
        body = "\n".join(
            f"{'사용자' if role == 'user' else '어시스턴트'}: {text}" for role, text in turns
        )
        return self._ask(EXTRACT_PROMPT, body)


def has_content(items: list[Any]) -> bool:
    return bool(items)

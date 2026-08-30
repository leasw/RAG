from __future__ import annotations

import json
from typing import Any


class MockLLMClient:
    """Deterministic local model stub for testing the ReAct loop without an API key.

    실제 모델의 라우팅을 흉내낸다. 메모리는 STM/MTM 2계층이고, 확정된 장기 지식이
    필요하면 계층을 더 파는 대신 search_documents(문서 코퍼스)로 넘어간다.
    """

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        user_query = self._last_user_query(messages)
        tool_results = self._tool_results(messages)
        tried = {result.get("tier", "").lower() for result in tool_results}
        searched_docs = any("stage" in result for result in tool_results)

        if not tool_results:
            return self._doc_call(user_query, "문서 코퍼스에서 근거 확인")

        if self._needs_comparison(user_query) and not searched_docs:
            return self._doc_call(user_query, "최근 결정과 확정 기준을 비교하기 위해 문서 코퍼스 확인")

        if not self._has_results(tool_results) and not searched_docs:
            return self._doc_call(user_query, "메모리에 근거가 부족해 문서 코퍼스로 확장")

        return {"role": "assistant", "content": self._final_answer(user_query, tool_results)}

    def _last_user_query(self, messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content", ""))
        return ""

    def _tool_results(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") != "tool":
                continue
            try:
                results.append(json.loads(str(message.get("content", "{}"))))
            except json.JSONDecodeError:
                pass
        return results

    def _first_tier(self, query: str) -> str:
        if any(word in query for word in ["아까", "방금", "오늘", "다음 일정", "action item"]):
            return "stm"
        if any(word in query for word in ["이번 달", "최근", "회의록", "초안", "보고서"]):
            return "mtm"
        return "all"

    def _needs_documents(self, query: str) -> bool:
        """메모리를 거치지 않고 바로 문서 코퍼스로 가야 하는 질문인지."""
        return any(
            word in query
            for word in ["공식", "최종", "기준", "계획서", "사업비", "예산",
                         "정량목표", "평가결과", "협약", "마일스톤"]
        )

    def _needs_comparison(self, query: str) -> bool:
        return any(word in query for word in ["충돌", "비교", "달라진", "차이"])

    def _is_recent(self, query: str) -> bool:
        return any(word in query for word in ["아까", "방금", "오늘", "최근"])

    def _has_results(self, tool_results: list[dict[str, Any]]) -> bool:
        return any(int(result.get("result_count", 0)) > 0 for result in tool_results)

    def _memory_call(self, tier: str, query: str, reason: str) -> dict[str, Any]:
        return self._tool_call(
            "retrieve_memory",
            f"mock_call_{tier}",
            {
                "tier": tier,
                "query": query,
                "filters": {"project": "A 과제"} if "A 과제" in query else {},
                "top_k": 5,
                "reason": reason,
            },
        )

    def _doc_call(self, query: str, reason: str) -> dict[str, Any]:
        return self._tool_call(
            "search_documents",
            "mock_call_docs",
            {"query": query, "top_k": 5, "reason": reason},
        )

    def _tool_call(self, name: str, call_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            ],
        }

    def _final_answer(self, query: str, tool_results: list[dict[str, Any]]) -> str:
        cards: list[dict[str, Any]] = []
        for result in tool_results:
            cards.extend(result.get("results", []))
        if not cards:
            return "확인 가능한 근거를 찾지 못했습니다. 현재 seed memory에는 해당 질문에 답할 자료가 부족합니다."

        lines = [f"질문에 대해 근거를 확인했습니다: {query}", ""]
        for card in cards[:4]:
            ref = card.get("source_ref", {})
            # 메모리 카드는 tier/title/summary를, 문서 카드는 stage/content를 가진다.
            label = card.get("tier") or ref.get("stage") or "DOC"
            title = card.get("title") or ref.get("document_id", "")
            body = card.get("summary") or card.get("quote") or card.get("content", "")
            lines.append(f"- [{label}] {title}: {str(body)[:200]}")
        lines.append("")
        lines.append("출처: " + ", ".join(card.get("source_ref", {}).get("document_id", "") for card in cards[:4]))
        return "\n".join(lines)


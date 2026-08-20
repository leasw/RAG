from __future__ import annotations

import json
from typing import Any


class MockLLMClient:
    """Deterministic local model stub for testing the ReAct loop without an API key."""

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        user_query = self._last_user_query(messages)
        tool_results = self._tool_results(messages)
        tried_tiers = {result.get("tier", "").lower() for result in tool_results}

        if not tool_results:
            tier = self._first_tier(user_query)
            return self._tool_call(tier, user_query, f"사용자 질문에 대한 {tier.upper()} 근거 확인")

        if self._needs_comparison(user_query) and "ltm" not in tried_tiers:
            return self._tool_call("ltm", user_query, "최근 결정과 공식 기준을 비교하기 위해 LTM 확인")

        if self._is_recent(user_query) and not self._has_results(tool_results) and "mtm" not in tried_tiers:
            return self._tool_call("mtm", user_query, "STM에서 충분한 근거를 찾지 못해 MTM으로 확장")

        if not self._has_results(tool_results) and "ltm" not in tried_tiers:
            return self._tool_call("ltm", user_query, "이전 tier에서 근거가 부족해 LTM으로 확장")

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
        if self._needs_comparison(query):
            return "stm"
        if any(word in query for word in ["아까", "방금", "오늘", "다음 일정", "action item"]):
            return "stm"
        if any(word in query for word in ["이번 달", "최근", "회의록", "초안", "보고서"]):
            return "mtm"
        if any(word in query for word in ["공식", "최종", "기준", "계획서", "회사"]):
            return "ltm"
        return "all"

    def _needs_comparison(self, query: str) -> bool:
        return any(word in query for word in ["충돌", "비교", "달라진", "차이"])

    def _is_recent(self, query: str) -> bool:
        return any(word in query for word in ["아까", "방금", "오늘", "최근"])

    def _has_results(self, tool_results: list[dict[str, Any]]) -> bool:
        return any(int(result.get("result_count", 0)) > 0 for result in tool_results)

    def _tool_call(self, tier: str, query: str, reason: str) -> dict[str, Any]:
        arguments = {
            "tier": tier,
            "query": query,
            "filters": {"project": "A 과제"} if "A 과제" in query else {},
            "top_k": 5,
            "reason": reason,
        }
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"mock_call_{tier}",
                    "type": "function",
                    "function": {
                        "name": "retrieve_memory",
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

        lines = [f"질문에 대해 seed memory 근거를 확인했습니다: {query}", ""]
        for card in cards[:4]:
            lines.append(
                f"- [{card.get('tier')}] {card.get('title')}: {card.get('summary') or card.get('quote')}"
            )
        lines.append("")
        lines.append("출처: " + ", ".join(card.get("source_ref", {}).get("document_id", "") for card in cards[:4]))
        return "\n".join(lines)


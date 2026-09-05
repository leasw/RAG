"""`search_ltm_memory` 도구: 채팅에서 검증을 거쳐 승격된 사실을 찾는 아이템.

    작업 메모리(자동 조회)   STM 5건 + MTM 5건 — 휘발성 맥락, 도구 아님
    search_ltm_memory       채팅 기원 LTM — 검증(출처·진실성·유용성)을 통과해
                             영구 지식으로 확정된 사실. 도구로 둔다.
    search_documents        문서 코퍼스(원래부터 문서였던 것) — 확정 산출물

LTM을 작업 메모리에서 뺀 이유는, 이미 검증까지 끝난 사실이라 매 턴 무조건 볼
필요가 없기 때문이다 — STM/MTM처럼 "지금 굴러가는 맥락"이 아니라 필요할 때만
찾아보면 되는 안정된 지식이라, 모델의 판단에 맡기는 도구 쪽이 맞다.
"""

from __future__ import annotations

from typing import Any

from .fact_memory import FactMemory


class LtmMemoryTool:
    name = "search_ltm_memory"

    def __init__(self, memory: FactMemory, default_top_k: int = 5):
        self.memory = memory
        self.default_top_k = default_top_k

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        top_k = int(args.get("top_k") or self.default_top_k)
        if not query:
            return {"query": query, "result_count": 0, "results": [],
                    "error": "query가 비어 있습니다."}

        facts = self.memory.search(query, tier="ltm", top_k=top_k)
        cards = [{
            "record_id": f.id,
            "content": f.text,
            "score": (f.meta or {}).get("score"),
            "source_ref": {"document_id": f.source_id, "path": f"ltm/{f.id}", "date": f.date},
        } for f in facts]
        return {"query": query, "result_count": len(cards), "results": cards}

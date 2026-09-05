"""`search_graph` 도구: 정의된 개체 사이의 구조적 관계를 묻는 아이템.

    (자동 조회)         STM/MTM — 채팅에서 나온 휘발성 맥락, 도구 아님
    search_ltm_memory   채팅 기원 LTM — 검증까지 끝나 확정된 사실
    search_documents    문서 코퍼스 전문 검색 — "무엇이 적혀 있나"
    search_graph        문서 코퍼스 위의 관계 그래프 — "무엇이 무엇과 어떻게 엮이나"

범위는 문서 코퍼스뿐이고, 그 안에서도 schema.py에 정의된 개체(기관/인물/과제/과업/
문서/제품/정량목표)만 노드다. 정의 밖 개념은 그래프에 없으므로 그럴 땐
search_documents로 가라고 결과에 명시한다.

그래프 탐색으로 후보를 좁히고 임베딩 유사도로 순위를 정하는 하이브리드라, 관계
사실(facts)과 근거 청크(results)를 함께 돌려준다. 관계만 주면 근거를 인용할 수 없고,
청크만 주면 문서 검색과 다를 게 없다.
"""

from __future__ import annotations

from typing import Any


class GraphSearchTool:
    name = "search_graph"

    def __init__(self, max_chars_per_card: int = 1200):
        self.max_chars_per_card = max_chars_per_card
        self._retriever = None
        self._load_error: str | None = None

    def _get(self):
        if self._retriever is None and self._load_error is None:
            try:
                from graph_rag.query import GraphRetriever

                self._retriever = GraphRetriever()
            except Exception as exc:  # noqa: BLE001
                self._load_error = f"{type(exc).__name__}: {exc}"
        return self._retriever

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        entities = args.get("entities") or None
        relation = args.get("relation") or None
        if relation == "any":
            relation = None
        hops = int(args.get("hops") or 1)
        top_k = int(args.get("top_k") or 5)

        retriever = self._get()
        if retriever is None:
            return {"query": query, "result_count": 0, "results": [], "facts": [],
                    "error": f"그래프 인덱스를 열 수 없습니다: {self._load_error}"}
        if not query:
            return {"query": query, "result_count": 0, "results": [], "facts": [],
                    "error": "query가 비어 있습니다."}

        try:
            found = retriever.search(query, entities=entities, relation=relation,
                                     hops=hops, top_k=top_k)
        except Exception as exc:  # noqa: BLE001
            return {"query": query, "result_count": 0, "results": [], "facts": [],
                    "error": f"{type(exc).__name__}: {exc}"}

        cards = []
        for hit in found.get("results", []):
            src = hit["source"]
            cards.append({
                "record_id": hit["chunk_id"],
                "content": hit["text"][: self.max_chars_per_card],
                "score": hit["score"],
                "source_ref": {
                    "document_id": src["file_name"],
                    "path": src["rel_path"],
                    "stage": src["stage"],
                    "page": src["page_no"],
                    "section": " > ".join(src["headings"]) if src["headings"] else "",
                },
            })

        out = {
            "query": query,
            "resolved_entities": found.get("resolved_entities", []),
            "facts": found.get("facts", []),
            "result_count": len(cards),
            "results": cards,
        }
        if found.get("note"):
            out["note"] = found["note"]
        return out

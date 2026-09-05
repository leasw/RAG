"""`search_documents` 도구: 에이전트가 과제 문서 코퍼스를 검색하는 아이템.

메모리 파이프라인(STM/MTM)과 나란히 놓이는 도구입니다. LTM은 두 갈래입니다.
- 메모리 파이프라인 : STM/MTM 사실 문장 (도구가 아니라 매 턴 자동 조회) — 휘발성 맥락
- search_ltm_memory : 채팅에서 나왔지만 검증까지 끝나 확정된 사실 (memory_promote 참고)
- search_documents : 실제 과제 산출물 코퍼스 (사업계획서/보고서/평가결과/회의자료)
                     = 채팅 밖에서 원래부터 확정돼 있던 문서 지식.

인덱스와 리랭커 로딩이 무거워서 최초 호출 때 한 번만 올리고 이후 재사용합니다.
인덱스가 없으면 도구가 죽는 대신 에러를 담은 결과를 돌려줘, 에이전트가 다른
근거로 답을 이어가게 합니다.
"""

from __future__ import annotations

from typing import Any


class DocSearchTool:
    name = "search_documents"

    def __init__(self, max_chars_per_card: int = 1200):
        self.max_chars_per_card = max_chars_per_card
        self._retriever = None
        self._load_error: str | None = None

    def _get_retriever(self):
        if self._retriever is None and self._load_error is None:
            try:
                from doc_rag.retriever import DocRetriever

                self._retriever = DocRetriever()
            except Exception as exc:  # noqa: BLE001
                self._load_error = f"{type(exc).__name__}: {exc}"
        return self._retriever

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        top_k = int(args.get("top_k") or 5)
        stage = args.get("stage") or None
        formats = args.get("formats") or None
        if stage == "all":
            stage = None

        retriever = self._get_retriever()
        if retriever is None:
            return {"query": query, "result_count": 0, "results": [],
                    "error": f"문서 인덱스를 열 수 없습니다: {self._load_error}"}
        if not query:
            return {"query": query, "result_count": 0, "results": [],
                    "error": "query가 비어 있습니다."}

        try:
            hits = retriever.search(query, top_k=top_k, stage=stage, formats=formats)
        except Exception as exc:  # noqa: BLE001
            return {"query": query, "result_count": 0, "results": [],
                    "error": f"{type(exc).__name__}: {exc}"}

        cards = []
        for hit in hits:
            src = hit["source"]
            cards.append({
                "record_id": hit["chunk_id"],
                "content": hit["text"][: self.max_chars_per_card],
                "score": hit["score"],
                "source_ref": {
                    "document_id": src["file_name"],
                    "path": src["rel_path"],
                    "stage": src["stage"],
                    "format": src["format"],
                    "page": src["page_no"],
                    "section": " > ".join(src["headings"]) if src["headings"] else "",
                },
            })
        return {"query": query, "stage": stage or "all",
                "result_count": len(cards), "results": cards}

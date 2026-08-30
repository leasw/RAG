"""문서 RAG 단독 검색 CLI (에이전트 없이 검색 품질만 확인할 때).

    python -m doc_rag.query "1단계 정량목표가 뭐야?"
    python -m doc_rag.query --stage 2단계 --top-k 5 "시범서비스 결과"
"""

from __future__ import annotations

import argparse

from .retriever import DocRetriever


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--top-k", type=int, default=None)
    ap.add_argument("--stage", default=None)
    ap.add_argument("--formats", nargs="*", default=None)
    ap.add_argument("--full", action="store_true", help="청크 전문 출력")
    args = ap.parse_args()

    retriever = DocRetriever()
    hits = retriever.search(args.query, top_k=args.top_k, stage=args.stage, formats=args.formats)
    if not hits:
        print("검색 결과가 없습니다.")
        return 1
    for i, hit in enumerate(hits, start=1):
        src = hit["source"]
        page = f" p.{src['page_no']}" if src["page_no"] else ""
        section = f"  [{' > '.join(src['headings'])}]" if src["headings"] else ""
        print(f"\n#{i}  score={hit['score']:.3f}  {src['file_name']}{page}{section}")
        print(f"    {src['rel_path']}")
        body = hit["text"] if args.full else hit["text"][:400].replace("\n", " ")
        print(f"    {body}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

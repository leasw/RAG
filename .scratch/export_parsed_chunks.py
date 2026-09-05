"""파싱된 청크 전체를 문서 단위로 묶어 JSON으로 내보낸다.

    python .scratch/export_parsed_chunks.py

index/doc_rag/chunks.sqlite3 (documents + chunks 테이블)을 그대로 읽어,
문서 하나당 메타데이터 + 청크 목록으로 구조화한다.
"""
import json
import sqlite3
from pathlib import Path

DB = Path("index/doc_rag/chunks.sqlite3")
OUT = Path(".scratch/parsed_chunks.json")
OUT_SUMMARY = Path(".scratch/parsed_chunks_summary.json")


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    docs = conn.execute(
        "SELECT doc_id, file_name, rel_path, format, stage, n_chunks, status, error "
        "FROM documents WHERE status = 'ok' ORDER BY rel_path, file_name"
    ).fetchall()

    out = []
    for d in docs:
        chunks = conn.execute(
            "SELECT chunk_id, ord, text, headings, page_no, n_chars "
            "FROM chunks WHERE doc_id = ? ORDER BY ord",
            (d["doc_id"],),
        ).fetchall()
        out.append({
            "doc_id": d["doc_id"],
            "file_name": d["file_name"],
            "rel_path": d["rel_path"],
            "format": d["format"],
            "stage": d["stage"],
            "n_chunks": len(chunks),
            "chunks": [
                {
                    "chunk_id": c["chunk_id"],
                    "ord": c["ord"],
                    "page_no": c["page_no"],
                    "headings": json.loads(c["headings"] or "[]"),
                    "n_chars": c["n_chars"],
                    "text": c["text"],
                }
                for c in chunks
            ],
        })

    OUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    # 요약본(텍스트 제외, 문서/청크 개수 감 잡기용)
    summary = [
        {"file_name": d["file_name"], "rel_path": d["rel_path"], "format": d["format"],
         "stage": d["stage"], "n_chunks": d["n_chunks"]}
        for d in out
    ]
    OUT_SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    total_chunks = sum(d["n_chunks"] for d in out)
    print(f"문서 {len(out)}건 / 청크 {total_chunks}건")
    print(f"전체: {OUT} ({OUT.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"요약: {OUT_SUMMARY} ({OUT_SUMMARY.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()

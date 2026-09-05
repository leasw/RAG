"""청크 하나 = 한 줄인 JSONL로 내보낸다. (요청: "하나의 청크가 한줄")

    python .scratch/export_chunks_jsonl.py

각 줄에 그 청크만 봐도 어느 문서·어디 위치인지 알 수 있게 문서 메타를 같이 싣는다.
"""
import json
import sqlite3
from pathlib import Path

DB = Path("index/doc_rag/chunks.sqlite3")
OUT = Path(".scratch/parsed_chunks.jsonl")


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT c.chunk_id, c.ord, c.text, c.headings, c.page_no, c.n_chars, "
        "       d.doc_id, d.file_name, d.rel_path, d.format, d.stage "
        "FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
        "WHERE d.status = 'ok' "
        "ORDER BY d.rel_path, d.file_name, c.ord"
    ).fetchall()

    n = 0
    with open(OUT, "w", encoding="utf-8") as f:
        for r in rows:
            line = {
                "file_name": r["file_name"], "rel_path": r["rel_path"], "format": r["format"],
                "doc_id": r["doc_id"], "chunk_id": r["chunk_id"], "ord": r["ord"],
                "page_no": r["page_no"], "headings": json.loads(r["headings"] or "[]"),
                "n_chars": r["n_chars"], "text": r["text"],
            }
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
            n += 1

    print(f"청크 {n}건 -> {OUT} ({OUT.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()

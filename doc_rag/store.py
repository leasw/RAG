"""청크 저장소: SQLite(메타/본문) + .npy(임베딩 행렬).

코퍼스가 수만 청크 규모라 외부 벡터 DB 없이 numpy 전량 코사인으로 충분합니다
(D:/RAG 벤치마크 파이프라인과 동일한 방식).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id      TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    file_name   TEXT NOT NULL,
    format      TEXT NOT NULL,
    stage       TEXT,
    size_bytes  INTEGER,
    mtime       REAL,
    content_sha TEXT,
    n_chunks    INTEGER,
    status      TEXT,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL,
    ord         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    headings    TEXT,
    page_no     INTEGER,
    n_chars     INTEGER,
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id)
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_docs_sha ON documents(content_sha);
"""


class ChunkStore:
    def __init__(self, index_dir: Path):
        self.index_dir = index_dir
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = index_dir / "chunks.sqlite3"
        self.vec_path = index_dir / "embeddings.npy"
        self.ids_path = index_dir / "chunk_ids.json"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------------------------------------------------- write

    def upsert_document(self, **row) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        self.conn.execute(
            f"INSERT OR REPLACE INTO documents ({cols}) VALUES ({marks})", list(row.values())
        )

    def replace_chunks(self, doc_id: str, chunks: list) -> None:
        self.conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        self.conn.executemany(
            "INSERT INTO chunks (chunk_id, doc_id, ord, text, headings, page_no, n_chars) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"{doc_id}#{i}",
                    doc_id,
                    i,
                    c.text,
                    json.dumps(c.headings, ensure_ascii=False),
                    c.page_no,
                    len(c.text),
                )
                for i, c in enumerate(chunks)
            ],
        )

    def commit(self) -> None:
        self.conn.commit()

    def seen_sha(self, content_sha: str) -> str | None:
        row = self.conn.execute(
            "SELECT doc_id FROM documents WHERE content_sha = ? AND status = 'ok' LIMIT 1",
            (content_sha,),
        ).fetchone()
        return row[0] if row else None

    def doc_status(self, doc_id: str) -> tuple[str | None, float | None]:
        row = self.conn.execute(
            "SELECT status, mtime FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    # ---------------------------------------------------------- read

    def all_chunks(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT c.chunk_id, c.doc_id, c.ord, c.text, c.headings, c.page_no, "
            "       d.rel_path, d.file_name, d.format, d.stage "
            "FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
            "ORDER BY c.doc_id, c.ord"
        ).fetchall()
        return [
            {
                "chunk_id": r[0], "doc_id": r[1], "ord": r[2], "text": r[3],
                "headings": json.loads(r[4] or "[]"), "page_no": r[5],
                "rel_path": r[6], "file_name": r[7], "format": r[8], "stage": r[9],
            }
            for r in rows
        ]

    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        by_fmt = self.conn.execute(
            "SELECT d.format, COUNT(DISTINCT d.doc_id), COUNT(c.chunk_id) "
            "FROM documents d LEFT JOIN chunks c ON c.doc_id = d.doc_id "
            "WHERE d.status = 'ok' GROUP BY d.format ORDER BY 3 DESC"
        ).fetchall()
        return {
            "documents_ok": q("SELECT COUNT(*) FROM documents WHERE status='ok'"),
            "documents_failed": q("SELECT COUNT(*) FROM documents WHERE status='failed'"),
            "documents_skipped": q("SELECT COUNT(*) FROM documents WHERE status='skipped'"),
            "chunks": q("SELECT COUNT(*) FROM chunks"),
            "by_format": [{"format": f, "docs": d, "chunks": c} for f, d, c in by_fmt],
        }

    def failures(self) -> list[tuple[str, str]]:
        return self.conn.execute(
            "SELECT rel_path, error FROM documents WHERE status='failed' ORDER BY rel_path"
        ).fetchall()

    # ---------------------------------------------------------- vectors

    def save_vectors(self, chunk_ids: list[str], vecs: np.ndarray) -> None:
        np.save(self.vec_path, vecs.astype(np.float32))
        self.ids_path.write_text(json.dumps(chunk_ids), encoding="utf-8")

    def load_vectors(self) -> tuple[list[str], np.ndarray]:
        if not self.vec_path.exists():
            raise FileNotFoundError(
                f"임베딩 인덱스가 없습니다: {self.vec_path}. 먼저 `python -m doc_rag.build_index`를 실행하세요."
            )
        return json.loads(self.ids_path.read_text(encoding="utf-8")), np.load(self.vec_path)

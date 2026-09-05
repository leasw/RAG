"""표 행 복구로 텍스트가 바뀐 hwp 청크들의 임베딩만 다시 계산해 갱신한다.

정확히 바뀐 5,516건의 chunk_id 목록을 저장해두지 않았으므로, 안전하게 hwp 포맷
청크 전체(11,172건)를 재임베딩한다 — 실제 변경분의 상위집합이라 누락이 없다.

embeddings.npy/chunk_ids.json은 전체를 한 번에 덮어쓰는 구조(save_vectors)라,
기존 벡터를 불러와 hwp 행만 교체하고 나머지(비-hwp, 22,504건)는 그대로 둔 채
전체를 다시 저장한다.
"""
import json
import sys
sys.path.insert(0, r"D:\LAB_RAG\Org-AI-Body")
import os
os.chdir(r"D:\LAB_RAG\Org-AI-Body")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

import sqlite3
import numpy as np

from doc_rag.config import INDEX_DIR, load_config
from doc_rag.embedding_factory import build_embedder
from doc_rag.store import ChunkStore


def main():
    cfg = load_config()
    store = ChunkStore(INDEX_DIR)

    ids, vecs = store.load_vectors()
    id_index = {cid: i for i, cid in enumerate(ids)}
    print(f"기존 벡터: {vecs.shape[0]}개 x {vecs.shape[1]}d")

    conn = sqlite3.connect("index/doc_rag/chunks.sqlite3")
    conn.row_factory = sqlite3.Row
    hwp_chunks = conn.execute(
        "SELECT c.chunk_id, c.text FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
        "WHERE d.format = 'hwp' AND d.status = 'ok'"
    ).fetchall()
    print(f"재임베딩 대상(hwp 전체): {len(hwp_chunks)}개")

    missing = [r["chunk_id"] for r in hwp_chunks if r["chunk_id"] not in id_index]
    if missing:
        print(f"경고: 기존 벡터에 없는 chunk_id {len(missing)}건 (건너뜀)")
    targets = [r for r in hwp_chunks if r["chunk_id"] in id_index]

    embedder = build_embedder(cfg)
    prefix = cfg["embedding"].get("passage_prefix", "")
    new_vecs = embedder.embed([prefix + r["text"] for r in targets], show_progress=True)
    new_vecs = np.asarray(new_vecs, dtype=np.float32)

    for r, v in zip(targets, new_vecs):
        vecs[id_index[r["chunk_id"]]] = v

    store.save_vectors(ids, vecs)
    print(f"\n갱신 완료: {len(targets)}개 벡터 교체, 전체 {vecs.shape[0]}개 저장")


if __name__ == "__main__":
    main()

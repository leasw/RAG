"""검색: Dense + BM25+ -> RRF -> cross-encoder rerank.

D:/RAG 벤치마크에서 nDCG@10 1위였던 구성(google/gemini-embedding-2 +
local BAAI/bge-reranker-v2-m3)을 그대로 문서 코퍼스에 적용한 것입니다.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .config import INDEX_DIR, get_api_key, load_config
from .embedding_factory import build_embedder
from .fusion import fuse
from .sparse import BM25PlusIndex
from .store import ChunkStore


def _cosine_topk(qvec: np.ndarray, mat: np.ndarray, ids: list[str], k: int) -> list[tuple[str, float]]:
    q = qvec / (np.linalg.norm(qvec) + 1e-12)
    m = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12)
    sims = m @ q
    k = min(k, len(ids))
    idx = np.argpartition(-sims, kth=k - 1)[:k]
    idx = idx[np.argsort(-sims[idx])]
    return [(ids[i], float(sims[i])) for i in idx]


class DocRetriever:
    """인덱스를 메모리에 올려두고 질의를 받는다. 첫 검색 때 리랭커를 지연 로드한다."""

    def __init__(self, cfg: dict | None = None, index_dir: Path = INDEX_DIR):
        self.cfg = cfg or load_config()
        self.store = ChunkStore(index_dir)
        self.chunk_ids, self.vectors = self.store.load_vectors()

        rows = {c["chunk_id"]: c for c in self.store.all_chunks()}
        # 벡터 순서와 메타 순서를 강제로 일치시킨다(인덱스 재빌드 중 불일치 방지).
        self.chunk_ids = [cid for cid in self.chunk_ids if cid in rows]
        self.meta = [rows[cid] for cid in self.chunk_ids]
        self.vectors = self.vectors[: len(self.chunk_ids)]

        self.bm25 = BM25PlusIndex(self.chunk_ids, [m["text"] for m in self.meta], lang="ko")
        self.embedder = build_embedder(self.cfg)
        self._reranker = None

    @property
    def reranker(self):
        if self._reranker is None:
            from .reranker import build_reranker

            # 로컬 백엔드는 API 키가 필요 없다(임베딩도 로컬이면 키 없이 전부 동작).
            api_key = (
                get_api_key(self.cfg)
                if self.cfg["reranker"].get("backend") == "openrouter"
                else None
            )
            self._reranker = build_reranker(self.cfg, api_key)
        return self._reranker

    def search(self, query: str, top_k: int | None = None,
               stage: str | None = None, formats: list[str] | None = None) -> list[dict]:
        r = self.cfg["retrieval"]
        top_k = top_k or r["final_top_k"]

        qvec = self.embedder.embed([self.cfg["embedding"].get("query_prefix", "") + query])[0]
        dense = _cosine_topk(qvec, self.vectors, self.chunk_ids, r["dense_top_k"])
        sparse = self.bm25.search_batch([query], r["sparse_top_k"])[0]
        fused = fuse(dense, sparse, k=r["rrf_k"])

        by_id = dict(zip(self.chunk_ids, self.meta))
        candidates = [(cid, by_id[cid]) for cid in fused if cid in by_id]
        if stage:
            candidates = [c for c in candidates if c[1]["stage"] == stage]
        if formats:
            candidates = [c for c in candidates if c[1]["format"] in formats]
        if not candidates:
            return []

        ranked = self.reranker.rerank(
            query, [(cid, m["text"]) for cid, m in candidates], top_k=top_k
        )
        floor = r.get("min_rerank_score")
        if floor is not None:
            ranked = [x for x in ranked if x[1] >= floor] or ranked[:1]

        out = []
        for cid, score in ranked:
            m = by_id[cid]
            out.append({
                "chunk_id": cid,
                "score": round(float(score), 4),
                "text": m["text"],
                "source": {
                    "file_name": m["file_name"],
                    "rel_path": m["rel_path"],
                    "stage": m["stage"],
                    "format": m["format"],
                    "page_no": m["page_no"],
                    "headings": m["headings"],
                },
            })
        return out

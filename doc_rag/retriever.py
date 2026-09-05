"""검색: Dense + BM25+ -> RRF. 리랭커 없음.

D:/RAG 벤치마크(KO-1/KO-2/EN-1, nDCG)로 실측한 결과, 임베딩을 300M급(mE5-base)으로
낮춘 상태에서는 300M 리랭커(bge-reranker-base)를 얹는 것보다 **RRF 융합 순위를 그대로
쓰는 게 평균 nDCG@10이 더 높았다**(0.7539 vs 0.7416) — 특히 Ko-StrategyQA(KO-2)에서
리랭커가 순위를 오히려 악화시켰다(0.6946 -> 0.6260). 그래서 리랭커 단계를 제거하고
RRF 융합 순위를 최종 순위로 그대로 쓴다. GPU 로딩·추론 비용도 그만큼 없앤다.

리랭커가 필요한 실험(예: 더 큰 리랭커 재도입)을 다시 하려면 이 파일의 git 이력에서
이전 버전을 참고할 것 -- reranker.py 자체는 지우지 않았다(memory_promote.py가
"같은 사실인가" 판정에 여전히 쓴다. 그건 문서 검색 순위와 무관한 별개 용도라
이번 벤치마크 결론과 상관없다).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .config import INDEX_DIR, load_config
from .embedding_factory import build_embedder
from .fusion import reciprocal_rank_fusion
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

    def search(self, query: str, top_k: int | None = None,
               stage: str | None = None, formats: list[str] | None = None) -> list[dict]:
        r = self.cfg["retrieval"]
        top_k = top_k or r["final_top_k"]

        qvec = self.embedder.embed([self.cfg["embedding"].get("query_prefix", "") + query])[0]
        dense = _cosine_topk(qvec, self.vectors, self.chunk_ids, r["dense_top_k"])
        sparse = self.bm25.search_batch([query], r["sparse_top_k"])[0]

        # 리랭커 없이 RRF 융합 점수를 그대로 최종 순위로 쓴다(모듈 docstring 참고).
        # reciprocal_rank_fusion을 직접 불러 점수를 살려둔다 -- fuse()는 순서만
        # 주고 점수를 버린다.
        rrf_scores = reciprocal_rank_fusion([[d for d, _ in dense], [d for d, _ in sparse]],
                                            k=r["rrf_k"])
        ranked_ids = sorted(rrf_scores, key=rrf_scores.get, reverse=True)

        by_id = dict(zip(self.chunk_ids, self.meta))
        candidates = [cid for cid in ranked_ids if cid in by_id]
        if stage:
            candidates = [c for c in candidates if by_id[c]["stage"] == stage]
        if formats:
            candidates = [c for c in candidates if by_id[c]["format"] in formats]
        candidates = candidates[:top_k]

        out = []
        for cid in candidates:
            m = by_id[cid]
            out.append({
                "chunk_id": cid,
                # RRF 점수다(0.01~0.03대 작은 양수). 예전 리랭커 로짓 스케일과
                # 다르니, 이 값에 절대 문턱(예: memory_promote.py의 옛
                # LTM_COVERED_SCORE=0.0)을 그대로 재사용하면 안 된다.
                "score": round(float(rrf_scores[cid]), 5),
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

"""Reciprocal Rank Fusion for combining dense + sparse candidate lists."""


def reciprocal_rank_fusion(rank_lists: list[list[str]], k: int = 60) -> dict[str, float]:
    """rank_lists: ranked doc_id lists (best first, ties broken by input order).
    Returns doc_id -> rrf score (higher is better)."""
    scores: dict[str, float] = {}
    for ranked_ids in rank_lists:
        for rank, doc_id in enumerate(ranked_ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def fuse(dense: list[tuple[str, float]], sparse: list[tuple[str, float]], k: int = 60) -> list[str]:
    """dense/sparse: [(doc_id, score), ...] already sorted best-first.
    Returns deduplicated doc_id list sorted by RRF score, best-first."""
    dense_ids = [d for d, _ in dense]
    sparse_ids = [d for d, _ in sparse]
    scores = reciprocal_rank_fusion([dense_ids, sparse_ids], k=k)
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]

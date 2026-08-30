"""인덱스 빌드 CLI: 인제스천 -> 청킹 -> 임베딩 -> 벡터 저장.

    python -m doc_rag.build_index                 # 전체 (중단 지점부터 이어서)
    python -m doc_rag.build_index --limit 50      # 앞 50개 파일만
    python -m doc_rag.build_index --skip-ingest   # 임베딩만 다시
    python -m doc_rag.build_index --stats         # 현재 인덱스 통계
"""

from __future__ import annotations

import argparse
import json

from .config import INDEX_DIR, corpus_roots, load_config
from .embedding_factory import build_embedder
from .ingest import ingest
from .store import ChunkStore


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="이미 처리한 문서도 다시 인제스천")
    ap.add_argument("--skip-ingest", action="store_true")
    ap.add_argument("--skip-embed", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    store = ChunkStore(INDEX_DIR)

    if args.stats:
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
        fails = store.failures()
        if fails:
            print(f"\n실패 {len(fails)}건:")
            for rel, err in fails[:40]:
                print(f"  {rel}\n    {err}")
        return 0

    if not args.skip_ingest:
        counters = ingest(cfg, corpus_roots(cfg), store, limit=args.limit,
                          force=args.force, verbose=not args.quiet)
        print("\n[ingest]", json.dumps(counters, ensure_ascii=False))

    if not args.skip_embed:
        chunks = store.all_chunks()
        if not chunks:
            print("[embed] 청크가 없습니다.")
            return 1
        embedder = build_embedder(cfg)
        prefix = cfg["embedding"].get("passage_prefix", "")
        vecs = embedder.embed([prefix + c["text"] for c in chunks], show_progress=True)
        store.save_vectors([c["chunk_id"] for c in chunks], vecs)
        print(f"[embed] {vecs.shape[0]} chunks x {vecs.shape[1]}d -> {store.vec_path}")

    print("\n[stats]", json.dumps(store.stats(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

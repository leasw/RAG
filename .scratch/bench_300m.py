"""300M급(mE5-base + bge-reranker-base) 전환 후 성능 벤치마크.

    python .scratch/bench_300m.py

측정 둘:
    1. 지연시간 -- 임베딩(질의 1건, 청크 30건), 리랭킹(30쌍) 각각 실측.
       인계 문서(D:\\Agent_RAG)의 "30쌍 리랭킹" 벤치마크와 같은 단위라 직접 비교 가능.
    2. 검색 품질 -- 이번 세션에서 이미 정답을 알고 있는 질문 6개로 실제
       DocRetriever.search()를 돌려, 알려진 정답 수치/이름이 top-k 안에 실제로
       나오는지 확인한다. nDCG 같은 정량 지표는 라벨 코퍼스가 없어 못 낸다 --
       "찾았다/못 찾았다"만 본다. 이전 568M 조합 벡터는 재계산 과정에서 덮어써서
       남아있지 않아 이번 세션 안에서 직접 A/B 비교는 불가능하다는 점을 밝혀둔다.
"""
import sys, time
sys.path.insert(0, r"D:\LAB_RAG\Org-AI-Body")
import os
os.chdir(r"D:\LAB_RAG\Org-AI-Body")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

import numpy as np

from doc_rag.config import load_config
from doc_rag.embedding_factory import build_embedder
from doc_rag.reranker import build_reranker
from doc_rag.retriever import DocRetriever

cfg = load_config()

print("=" * 70)
print("1. 지연시간 (콜드 스타트 로딩 시간 별도, 추론만)")
print("=" * 70)

t = time.time()
embedder = build_embedder(cfg)
load_embed_s = time.time() - t

t = time.time()
reranker = build_reranker(cfg)
load_rerank_s = time.time() - t

print(f"임베더 로딩: {load_embed_s:.2f}s / 리랭커 로딩: {load_rerank_s:.2f}s")

# 질의 1건 임베딩
prefix = cfg["embedding"].get("query_prefix", "")
q = "장애물 감지 인식률 1차년도 목표치가 몇 %야?"
t = time.time()
_ = embedder.embed([prefix + q])
embed_query_s = time.time() - t

# 청크 30건 임베딩 (신규 텍스트로, 캐시 안 타게)
dummy_chunks = [f"임시 벤치마크용 문장 {i} 서로 다른 내용을 담아 캐시를 피한다 {'x'*i}" for i in range(30)]
t = time.time()
_ = embedder.embed(dummy_chunks)
embed_30chunks_s = time.time() - t

print(f"질의 1건 임베딩: {embed_query_s*1000:.1f}ms")
print(f"청크 30건 임베딩(캐시 미스): {embed_30chunks_s:.2f}s ({embed_30chunks_s/30*1000:.1f}ms/청크)")

# 리랭킹 30쌍
pairs = [(f"c{i}", f"문서 청크 예시 {i} " * 20) for i in range(30)]
t = time.time()
_ = reranker.rerank(q, pairs, top_k=5)
rerank_30_s = time.time() - t
print(f"리랭킹 30쌍: {rerank_30_s:.2f}s")

print("\n" + "=" * 70)
print("2. 검색 품질 -- 정답을 아는 질문으로 확인")
print("=" * 70)

retriever = DocRetriever()

CASES = [
    ("장애물 감지 인식률 1차년도 목표치가 몇 %야?", ["90%", "90 %"]),
    ("이 과제 1차년도 정부출연금이 얼마야?", ["100,000천", "1억"]),
    ("가천대 인건비 이월 요청액이 얼마야?", ["15,955천", "16,200천"]),
    ("2단계 전체 정부출연금 합계가 얼마야?", ["1,099,750천"]),
    ("위험 감지 센서 응답 속도 2차년도 목표가 뭐야?", ["1초", "1sec"]),
    ("과제 전담기관이 어디야?", ["한국산업기술평가관리원", "KEIT"]),
]

hit, total = 0, len(CASES)
for q, answer_markers in CASES:
    t = time.time()
    hits = retriever.search(q, top_k=5)
    dt = time.time() - t
    found = any(any(m in h["text"] for m in answer_markers) for h in hits)
    hit += found
    mark = "O" if found else "X"
    print(f"\n[{mark}] {q}  ({dt*1000:.0f}ms, top-{len(hits)})")
    if not found:
        for h in hits[:2]:
            print(f"      (참고) {h['text'][:70]}")

print(f"\n정답 포함율: {hit}/{total} = {hit/total:.1%}")

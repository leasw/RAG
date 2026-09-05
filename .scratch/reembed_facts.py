"""facts.sqlite3(STM/MTM/LTM 전부)의 vec 컬럼을 새 임베딩 모델로 재계산한다.

임베딩 모델을 300M급(mE5-base)으로 바꾸면서 기존에 저장된 벡터(arctic-ko 1024차원)가
전부 차원도 다르고 임베딩 공간도 달라져 무의미해졌다. FactMemory에 이런 일괄
재계산 유틸리티가 없어서 여기서 직접 SQL로 갱신한다 -- 저장 경로(_vectors)와
동일하게 embed_fn()으로 임베딩하고 L2 정규화한다.

    python .scratch/reembed_facts.py
"""
import sys
sys.path.insert(0, r"D:\LAB_RAG\Org-AI-Body")
import os
os.chdir(r"D:\LAB_RAG\Org-AI-Body")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

from pathlib import Path
from org_agent_mvp.fact_memory import FactMemory

BATCH = 64

memory = FactMemory(Path("memory_seed"), track_access=False)
facts = memory.all_facts()
print(f"전체 {len(facts)}건 재임베딩 (새 모델: mE5-base, 768차원)")

updated = 0
for i in range(0, len(facts), BATCH):
    batch = facts[i:i + BATCH]
    texts = [f.text for f in batch]
    vecs = memory._vectors(texts)
    if vecs is None:
        print("임베더를 못 불렀습니다 — 중단")
        break
    for f, v in zip(batch, vecs):
        memory.conn.execute("UPDATE facts SET vec = ? WHERE id = ?", (v.tobytes(), f.id))
    memory.conn.commit()
    updated += len(batch)
    print(f"  {updated}/{len(facts)}")

print(f"완료: {updated}건 재임베딩")
print(memory.stats())

import difflib
import sys
sys.path.insert(0, r"D:\LAB_RAG\Org-AI-Body")
import os
os.chdir(r"D:\LAB_RAG\Org-AI-Body")

from graph_rag.schema import ENTITIES


def fuzzy_best(text, surface, max_delta=2, min_len=3):
    L = len(surface)
    if L < min_len:
        return 0.0
    best = 0.0
    for wlen in range(max(min_len, L - max_delta), L + max_delta + 1):
        if wlen > len(text):
            continue
        for start in range(0, len(text) - wlen + 1):
            window = text[start:start + wlen]
            r = difflib.SequenceMatcher(None, window, surface, autojunk=False).ratio()
            if r > best:
                best = r
    return best


def best_entity_match(text):
    scored = []
    for ent in ENTITIES:
        best = max((fuzzy_best(text, s) for s in ent.surfaces), default=0.0)
        scored.append((best, ent.key, ent.label))
    scored.sort(reverse=True)
    return scored


POSITIVE = [
    ("경북대산학협력단은 무슨 역할로 참여하고 있어?", "org:경북대산협"),
    ("동국대산학협력단은 이 과제에서 어떤 역할이야?", "org:동국대산협"),
    ("피씨티 대표이사가 누구고 어떤 역할이야?", "org:피씨티"),
    ("가천대 소속 연구원 알려줘", "org:가천대산협"),
]
NEGATIVE_ONLY = [
    "이 과제 연구책임자가 누구야?",   # 개체명 자체가 없음 -> fuzzy도 안 걸려야 정상
    "오늘 점심 뭐 먹지",
]

print("=== 정답이 있는 케이스 ===")
for text, target in POSITIVE:
    scored = best_entity_match(text)
    top = scored[0]
    target_score = next(s for s, k, l in scored if k == target)
    print(f"{text}")
    print(f"  1등: {top[2]} ({top[1]}) score={top[0]:.3f}")
    print(f"  정답({target}) score={target_score:.3f}  {'OK top1' if top[1]==target else 'MISS'}")
    print(f"  상위5: {[(round(s,3), k) for s,k,l in scored[:5]]}")
    print()

print("=== 개체명이 아예 없는 케이스 (오탐 확인) ===")
for text in NEGATIVE_ONLY:
    scored = best_entity_match(text)
    top = scored[0]
    print(f"{text}")
    print(f"  1등: {top[2]} ({top[1]}) score={top[0]:.3f}")
    print()

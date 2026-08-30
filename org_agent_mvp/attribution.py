"""최종 답변과 후보 청크의 임베딩 코사인으로 조회수 증가분을 계산한다.

    Gemini(LLM) → 최종 답변 회신
                → 답변을 500M 임베딩 모델로 임베딩 (한 덩어리, 벡터 1개)
                → 후보 청크 각각과 코사인 비교
                → 각 유사도만큼 그 청크의 조회수 증가

"도구가 반환했다"(+1)와 "답변에 실제로 쓰였다"는 다르다. 반환됐지만 모델이 무시한
근거와 답변 절반을 채운 근거가 같은 1회로 잡히면, 조회수 기준 승격이 무의미해진다.
그래서 반환 횟수 대신 답변과의 유사도를 그대로 조회수에 더한다.

점수는 카드마다 독립이다. 정규화하지 않으므로 합이 1을 넘을 수 있고, 카드가 몇 장이든
같은 근거는 같은 값을 받는다. 후보 구성에 따라 점수가 흔들리지 않는다는 뜻이다.

문턱을 두지 않는다. 참고로 이 코퍼스에서 무관한 쌍의 코사인은 평균 0.195(표준편차
0.112, p95 0.408)라 바닥이 0이 아니다. 나중에 문턱을 넣는다면 그 분포를 기준으로 잡으면 된다.

측정하는 것은 **귀속**이지 인과가 아니다. 근거를 빼고 다시 생성했을 때 답변이 어떻게
달라지는지(leave-one-out)가 인과에 가깝지만, 턴마다 근거 수만큼 LLM을 다시 불러야
해서 여기서는 하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

EmbedFn = Callable[[Sequence[str]], Any]  # texts -> (N, D) float array


@dataclass
class CardScore:
    record_id: str
    source: str                  # "memory" | "documents"
    label: str
    influence: float = 0.0       # 답변과의 코사인 유사도 (음수는 0)
    method: str = ""             # "embedding" | "unavailable"


def attribute(answer: str, cards: list[dict[str, Any]],
              embed_fn: EmbedFn | None = None) -> list[CardScore]:
    """cards: [{record_id, source, label, text}] 형태.

    embed_fn이 없거나 실패하면 전부 0을 돌려준다(method="unavailable"). 근거 없는
    점수를 만들어내는 것보다 계산하지 못했다고 말하는 편이 낫다.
    """
    scores = [
        CardScore(record_id=c["record_id"], source=c["source"], label=c.get("label", ""))
        for c in cards
    ]
    if not cards or not (answer or "").strip() or embed_fn is None:
        for s in scores:
            s.method = "unavailable" if embed_fn is None else "embedding"
        return scores

    sims = _cosine_to_answer(answer, [c.get("text", "") for c in cards], embed_fn)
    if sims is None:
        for s in scores:
            s.method = "unavailable"
        return scores

    for score, sim in zip(scores, sims):
        score.influence = round(sim, 4)
        score.method = "embedding"
    return scores


def _cosine_to_answer(answer: str, card_texts: list[str],
                      embed_fn: EmbedFn) -> list[float] | None:
    """답변 벡터 1개 vs 카드 벡터 N개의 코사인. 음수는 0으로 자른다."""
    try:
        import numpy as np

        vecs = np.asarray(embed_fn([answer] + list(card_texts)), dtype="float32")
        if vecs.shape[0] != len(card_texts) + 1:
            return None
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
        sims = vecs[1:] @ vecs[0]
        return [max(0.0, float(v)) for v in sims]
    except Exception:  # noqa: BLE001 - 임베딩 실패가 답변을 막으면 안 된다
        return None

"""STM -> MTM 승격. 조건을 넘긴 사실을 그대로 올린다.

    python -m org_agent_mvp.memory_promote            # 승격 실행
    python -m org_agent_mvp.memory_promote --dry-run  # 무엇이 올라가고 무엇이 버려지는지만
    python -m org_agent_mvp.memory_promote --stats    # 현재 계층 상태

승격 조건은 조회수 하나다. 최신성 창(7일)은 승격의 전제조건이 아니라 STM에 남을
자격일 뿐이다.

    조회수 >= 임계치  ->  창과 무관하게 즉시 승격
    조회수 <  임계치  ->  창 안에 있으면 STM 유지, 창을 벗어나면 그냥 폐기
                          (문서 코퍼스에 있는지는 보지 않는다)

승격은 사실마다 **ADD 또는 UPDATE** 둘 중 하나다.

    ADD     MTM에 같은 사실이 없다 -> 계층 표시만 바꿔 올린다
    UPDATE  MTM에 같은 사실이 있다 -> 그 레코드의 문장을 새 것으로 갈아끼우고
            조회수를 합산 승계한 뒤, STM 쪽 레코드는 지운다

승격이 끝나면 **DELETE**로 MTM 안의 중복을 정리한다. 승격 판정은 STM 사실 하나와
MTM을 1:1로 보기 때문에, MTM 안에 이미 쌓여 있던 중복은 손대지 못한다. 그래서
마지막에 MTM 전체를 훑어 상호 함의로 같은 사실인 쌍을 찾아 하나로 합친다.

**DELETE는 중복 제거에만 쓴다.** 정정이나 모순 해소에는 쓰지 않는다. 실측에서 정정은
상호 함의가 음수로 나와(수치 변경 -1.34, 결정 뒤집힘 -4.16) 중복 판정에 걸리지 않으므로,
옛 사실과 새 사실이 MTM에 함께 남는다. 어느 쪽이 맞는지는 날짜를 보고 모델이 판단한다.
사실을 지우는 것은 되돌릴 수 없어서, 판정이 확실한 중복에만 허용한다.

**압축하지 않는다.** 사실은 이미 원자 단위(한 문장 = 한 사실)라 여러 사실을 하나로
합칠 이유가 없다. UPDATE는 합치기가 아니라 같은 사실의 표현을 최신 것으로 바꾸는
1:1 교체다.

    씨앗    조회수 >= 임계치인 사실
    동반    씨앗과 코사인 SIM_THRESHOLD 이상인 사실들 (임계 미달이어도 함께 승격)

임계 미달 사실도 함께 올리는 이유는, 유사도가 그만큼 높으면 같은 사안이기 때문이다.
조회수가 낮은 건 그 표현이 안 걸렸을 뿐이다. 한 사안이 자주 쓰이면 그 사안 전체를
올린다.

승격이 LLM을 부르지 않으므로 결정적이고 비용이 0이다.

LTM(문서 코퍼스) 확인은 하지 않는다. 창을 벗어난 채 조회수 미달인 사실은 문서에
있든 없든 STM에서는 필요 없다고 보고 그냥 버린다. 필요하면 채팅으로 다시 논의될
것이고, 그때 다시 조회수가 쌓여 승격 후보가 된다.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from .config import AppConfig
from .fact_memory import FactMemory
from .openrouter_client import OpenRouterClient

ROOT = Path(__file__).resolve().parents[1]
RAW_CHAT = ROOT / "data" / "raw" / "chat"
RAW_AI = ROOT / "data" / "raw" / "ai_chat"

# STM 최신성 창. fact_memory와 같은 값을 써야 한다 — 그쪽은 진입 자격을,
# 여기서는 승격 심사 대상을 가른다.
from .fact_memory import STM_WINDOW_DAYS  # noqa: E402
# 설계안의 MTM 구분 기준: 조회수. 여기서 조회수는 검색 기여도(0..1)의 누적합이라
# 실수다. 한 턴에서 답변과의 코사인이 0.6이면 0.6이 쌓인다.
#
# 3.0은 대략 "답변에 확실히 쓰인 턴이 5~6번"에 해당한다(턴당 기여도가 보통 0.5~0.7).
# 아직 평가셋이 없어 최적값이 아니라 운영 시작점이다.
DEFAULT_MIN_VIEWS = 3.0

# 사실끼리 "같은 사안"으로 볼 코사인 문턱.
#
# 현재 사실 19개의 쌍 분포로 잡았다: p50=0.204 p75=0.305 p90=0.464 p95=0.543 p99=0.788.
# 경계가 되는 실제 쌍은 이 둘이다.
#   0.619  "임수빈이 평가 최고 점수 확인" vs "임수빈이 개선 항목표 정리"  -> 합치면 안 됨
#   0.659  "방위 안내 근거자료 보관" vs "방위 안내 반영 여부 결정"        -> 합쳐야 함
# 그래서 그 사이인 0.65로 둔다. 표본이 19개뿐이라 사실이 쌓이면 다시 재야 한다.
SIM_THRESHOLD = 0.65

# MTM의 기존 사실과 "같은 사실"로 보아 UPDATE할 리랭커 양방향 문턱.
#
# 한 방향만 보면 포함 관계를 못 거른다. 구체적인 문장이 일반적인 문장을 뒷받침하는
# 것은 쉽지만 그 반대는 약하다. 양방향이 모두 성립해야 상호 함의 = 같은 사실이다.
#
# 실측 (min(rr(a->b), rr(b->a))):
#     같은 사실 / 표현만 다름   +4.75, +5.26
#     정정 / 수치가 바뀜        -1.34
#     정정 / 결정이 뒤집힘      -4.16
#     다른 사실 / 주제만 같음   -1.20
#     완전히 다름              -11.04
# 같은 사실과 나머지 사이 격차가 5.95라 그 중간인 2.0으로 둔다.
#
# 정정이 음수로 나오는 것에 주의. 수치가 바뀌거나 결정이 뒤집힌 문장은 "같은 사실"로
# 판정되지 않아 UPDATE가 아니라 ADD가 된다. DELETE 연산이 없으므로 옛 사실이 MTM에
# 그대로 남아 새 사실과 공존한다.
UPDATE_SCORE = 2.0

# 리랭커에 넘길 MTM 후보 수. 코사인으로 먼저 추린다.
UPDATE_CANDIDATES = 5

# LTM 갱신(같은 사안·다른 정보) 판정의 하한. 위 실측 각주의 정정 사례가 -1.34 ~
# -4.16에 있었고, 완전히 무관한 사실은 -11.04까지 떨어졌다. 상호함의가 이 밑으로
# 내려가면 "같은 사안의 최신 정보"가 아니라 "코사인만 우연히 겹친 무관한 문장"으로
# 본다 — 실측에서 코사인 0.85인데 상호함의 -7.65인 완전 무관 쌍(엔티티 담당자 변경
# vs 전담기관명)이 나와서 하한 없이는 엉뚱한 갱신이 발생했다.
UPDATE_FLOOR = -6.0

# (과거) LTM(문서 코퍼스) 보유 여부를 가르던 리랭커 점수 문턱 — 문서에 있음
# +2.709~+4.714 / 채팅에만 -4.473~-0.613, 격차 3.3으로 실측했었다. doc_rag의
# search_documents 경로에서 리랭커를 없앤 뒤로는 이 문턱이 더 이상 안 맞는
# 점수 스케일(RRF 점수, 항상 작은 양수)에 적용될 판이라 삭제했다 —
# `_doc_corroboration()`이 이제 "있다/없다"를 숫자로 확정하지 않고 최상위 후보
# 문서 조각을 그대로 LLM에게 넘겨 직접 판단하게 한다.


def _cluster(memory: FactMemory, aged: list, seeds: list,
             sim_threshold: float) -> tuple[list[dict], list]:
    """씨앗을 중심으로 같은 사안의 사실을 모은다.

    조회수가 높은 씨앗부터 처리한다. 먼저 잡은 묶음이 우선권을 갖도록 해서, 한 사실이
    두 묶음에 들어가 압축이 중복되는 일을 막는다.
    """
    vecs = memory.vectors_for(aged)
    if vecs is None:
        # 벡터가 없으면 묶을 근거가 없다. 씨앗 하나가 곧 묶음이다.
        return [{"facts": [s]} for s in seeds], []

    index = {f.id: i for i, f in enumerate(aged)}
    assigned: set[str] = set()
    clusters: list[dict] = []

    for seed in sorted(seeds, key=lambda f: -f.views):
        if seed.id in assigned:
            continue
        sims = vecs @ vecs[index[seed.id]]
        members = [
            aged[i] for i in range(len(aged))
            if float(sims[i]) >= sim_threshold and aged[i].id not in assigned
        ]
        if seed not in members:
            members.append(seed)
        for m in members:
            assigned.add(m.id)

        clusters.append({"facts": members})

    leftover = [f for f in aged if f.id not in assigned]
    return clusters, leftover


class _Judge:
    """MTM에 같은 사실이 이미 있는지 판정한다. ADD / UPDATE를 가른다.

    코사인으로 후보를 추린 뒤 리랭커 양방향으로 상호 함의를 본다. 한 방향만 보면
    포함 관계를 못 거른다 — 구체적인 문장이 일반적인 문장을 뒷받침하기는 쉽지만
    그 반대는 약하다. 둘 다 성립해야 같은 사실이다.

    리랭커를 못 올리면 전부 ADD로 처리한다. 잘못 UPDATE해서 기존 사실을 덮어쓰는
    것보다, 중복을 남기는 편이 안전하다.
    """

    def __init__(self, memory: FactMemory, min_score: float, candidates: int):
        self.memory = memory
        self.min_score = min_score
        self.candidates = candidates
        self.reranker = None
        try:
            from doc_rag.config import load_config
            from doc_rag.reranker import build_reranker

            self.reranker = build_reranker(load_config())
        except Exception as exc:  # noqa: BLE001
            print(f"  (ADD/UPDATE 판정 불가 — {type(exc).__name__}: {exc}. 전부 ADD)")

    def _mutual(self, a: str, b: str) -> float:
        x = self.reranker.rerank(a, [("x", b)], top_k=1)[0][1]
        y = self.reranker.rerank(b, [("y", a)], top_k=1)[0][1]
        return min(float(x), float(y))

    def target_for(self, fact):
        """UPDATE 대상 MTM 사실. 없으면 None(=ADD)."""
        if self.reranker is None:
            return None, 0.0
        vecs = self.memory.vectors_for([fact])
        if vecs is None:
            return None, 0.0
        best, best_score = None, float("-inf")
        for cand, _cos in self.memory.candidates_in_tier(vecs[0], "mtm", self.candidates):
            score = self._mutual(fact.text, cand.text)
            if score > best_score:
                best, best_score = cand, score
        if best is not None and best_score >= self.min_score:
            return best, best_score
        return None, best_score


def _promote_facts(memory: FactMemory, facts: list, judge: _Judge,
                   dry_run: bool, indent: str = "      ") -> dict:
    """사실마다 ADD 또는 UPDATE를 골라 MTM으로 올린다."""
    counts = {"add": 0, "update": 0}
    for f in facts:
        target, score = judge.target_for(f)
        if target is not None:
            print(f"{indent}~ UPDATE (상호함의 {score:+.2f}) {f.text[:56]}")
            print(f"{indent}    교체 대상: {target.text[:60]}")
            if not dry_run:
                memory.update_fact(target.id, f.text, add_views=f.views,
                                   add_returns=f.returns)
                memory.drop([f.id])
            counts["update"] += 1
        else:
            print(f"{indent}+ ADD    (최고 {score:+.2f}) {f.text[:56]}")
            if not dry_run:
                memory.retier([f.id], "mtm")
            counts["add"] += 1
    return counts


def _dedup_tier(memory: FactMemory, tier: str, judge: "_Judge", dry_run: bool,
                only_ids: set[str] | None = None) -> int:
    """그 계층 안의 중복을 찾아 하나로 합친다(DELETE). STM->MTM(mtm)에도,
    MTM->LTM(ltm)에도 같은 로직을 쓴다 — "같은 사실인가" 판정 자체는 계층과 무관하다.

    조회수가 높은 쪽을 남긴다. 실제로 답변에 쓰인 표현이 그쪽이기 때문이다.
    지워지는 쪽의 조회수는 남는 쪽으로 넘어간다.

    only_ids를 주면 비교 대상을 그 id들로만 좁힌다. LTM에서 이게 중요한 이유가
    있다 — only_ids 없이 tier 전체를 훑으면, 방금 승격된 사실이 예전부터 있던
    LTM 사실과 (리랭커 오판으로) 잘못 병합돼 이미 검증된 기존 레코드가 지워질
    위험이 있다. LTM은 STM/MTM과 달리 "이미 확정된" 계층이라 이 위험을 감수할
    이유가 없다 — 그래서 promote_to_ltm()은 이번에 새로 승격된 것들끼리만
    비교하도록 이 인자를 넘긴다. STM->MTM 쪽은 그런 우려가 없어 tier 전체를 본다.
    """
    facts = memory.all_facts(tier)
    if only_ids is not None:
        facts = [f for f in facts if f.id in only_ids]
    if len(facts) < 2 or judge.reranker is None:
        return 0
    vecs = memory.vectors_for(facts)
    if vecs is None:
        return 0

    order = sorted(range(len(facts)), key=lambda i: -facts[i].views)
    removed: set[str] = set()
    total = 0
    for i in order:
        keeper = facts[i]
        if keeper.id in removed:
            continue
        victims = []
        for j in order:
            other = facts[j]
            if other.id == keeper.id or other.id in removed:
                continue
            if float(vecs[i] @ vecs[j]) < SIM_THRESHOLD:
                continue                      # 싼 1차 필터
            score = judge._mutual(keeper.text, other.text)
            if score >= judge.min_score:
                victims.append((other, score))
        if not victims:
            continue
        print(f"\n  [DELETE] 중복 {len(victims)}건을 합침 (조회수 {keeper.views:.3f} 유지)")
        print(f"      남김: {keeper.text[:64]}")
        for v, sc in victims:
            print(f"      지움: (상호함의 {sc:+.2f} / 조회수 {v.views:.3f}) {v.text[:52]}")
            removed.add(v.id)
        if not dry_run:
            memory.absorb(keeper.id, [v.id for v, _ in victims])
        total += len(victims)
    return total


def _resolve_against_ltm(memory: FactMemory, candidates: list, judge: "_Judge",
                          sim_threshold: float = SIM_THRESHOLD) -> dict:
    """승격 후보마다 **기존** LTM(이번에 새로 승격되는 것들 말고, 이미 있던 것)과
    비교해서 신규 승격/중복 폐기/기존 갱신 셋 중 하나로 정한다.

    기존 LTM 레코드는 여기서 지우거나 합치지(absorb) 않는다 — 오직 UPDATE 케이스에서만
    문장을 갈아끼운다(그것도 "같은 사안, 최신 정보로 교체"라는 명시적 의도가 있을 때만).
    _dedup_tier()의 흡수 병합과 달리, 여기는 리랭커 오판으로 기존 레코드가 통째로
    지워질 경로 자체가 없다.

    코사인(주제 유사)과 상호함의(내용 일치)를 따로 본다:

        코사인 < sim_threshold                          무관한 사안 -> 신규 승격
        상호함의 >= min_score                            같은 사실 -> 중복, 후보 폐기
        UPDATE_FLOOR <= 상호함의 < min_score              같은 사안·다른 정보
                                                          -> 최신 정보로 기존 LTM 갱신
        상호함의 < UPDATE_FLOOR                           코사인만 겹친 무관한 사실 -> 신규 승격

    코사인은 "리랭커에 돌릴 후보를 추리는 1차 필터"일 뿐 그 자체로 "같은 사안"을
    확정하지 않는다 — 짧은 한국어 문장은 표면 단어가 겹치면 주제가 달라도 코사인이
    쉽게 0.8대까지 올라간다. 최종 판단은 항상 상호함의(내용) 쪽이 한다.
    """
    new_ids: list[str] = []
    updated: list[dict] = []
    rejected: list[dict] = []

    existing = memory.all_facts("ltm")
    if not existing or judge.reranker is None:
        return {"new": [f.id for f in candidates], "updated": [], "rejected": []}

    cand_vecs = memory.vectors_for(candidates)
    exist_vecs = memory.vectors_for(existing)
    if cand_vecs is None or exist_vecs is None:
        return {"new": [f.id for f in candidates], "updated": [], "rejected": []}

    for i, f in enumerate(candidates):
        sims = exist_vecs @ cand_vecs[i]
        best_j, best_cos = None, sim_threshold
        for j in range(len(existing)):
            cos = float(sims[j])
            if cos >= best_cos:
                best_j, best_cos = j, cos
        if best_j is None:
            new_ids.append(f.id)
            continue

        target = existing[best_j]
        score = judge._mutual(f.text, target.text)
        if score >= judge.min_score:
            print(f"  [중복] LTM에 이미 있음 (상호함의 {score:+.2f}) -> 승격 취소: {f.text[:52]}")
            print(f"      기존: {target.text[:60]}")
            rejected.append({"id": f.id, "text": f.text, "matched_ltm_id": target.id, "score": score})
        elif score >= UPDATE_FLOOR:
            print(f"  [갱신] 같은 사안·다른 정보 (코사인 {best_cos:.2f} / 상호함의 {score:+.2f}) "
                  f"-> LTM 갱신: {f.text[:52]}")
            print(f"      기존: {target.text[:60]}")
            updated.append({"id": f.id, "text": f.text, "target_id": target.id, "score": score})
        else:
            # 코사인은 겹쳤지만 상호함의가 바닥까지 떨어짐 -> 표면 단어만 겹친
            # 무관한 사실. "같은 사안"이 아니므로 갱신 대상이 아니라 신규로 본다.
            print(f"  [무관] 코사인만 겹침 (코사인 {best_cos:.2f} / 상호함의 {score:+.2f}) "
                  f"-> 신규 승격: {f.text[:52]}")
            new_ids.append(f.id)

    return {"new": new_ids, "updated": updated, "rejected": rejected}


def promote_touched(memory: FactMemory, fact_ids: list[str],
                    min_views: float = DEFAULT_MIN_VIEWS,
                    sim_threshold: float = SIM_THRESHOLD,
                    update_score: float = UPDATE_SCORE) -> dict:
    """이번 턴에 손댄(새로 생기거나 조회수가 오른) 사실만 승격 여부를 확인한다.

    매 턴 끝에 부르는 용도라 run() 전체를 돌리면 안 된다 — run()은 MTM 전체를
    훑는 중복 정리(O(n^2) 리랭커 호출)와 STM 전체 폐기 검사를 포함해서, 대화가
    쌓일수록 턴마다 느려진다. 여기서는 "이번 턴에 조회수가 임계치를 넘긴 사실이
    있는가"만 본다 — 대부분의 턴은 answer가 된다(아니오), 그러면 리랭커도
    임베딩 행렬곱도 없이 그냥 반환한다.

    폐기(창을 벗어난 조회수 미달 사실 정리)와 MTM 중복 통합은 여기서 하지 않는다.
    그건 대화 흐름과 무관한 유지보수 작업이라 run()을 별도 주기로(배치·크론 등)
    돌려서 처리한다.
    """
    if not fact_ids:
        return {"add": 0, "update": 0}

    all_stm = memory.all_facts("stm")
    by_id = {f.id: f for f in all_stm}
    seeds = [by_id[fid] for fid in fact_ids if fid in by_id and by_id[fid].views >= min_views]
    if not seeds:
        return {"add": 0, "update": 0}

    clusters, _leftover = _cluster(memory, all_stm, seeds, sim_threshold)
    judge = _Judge(memory, update_score, UPDATE_CANDIDATES)
    counts = {"add": 0, "update": 0}
    for cluster in clusters:
        c = _promote_facts(memory, cluster["facts"], judge, dry_run=False)
        counts["add"] += c["add"]
        counts["update"] += c["update"]
    return counts


def run(min_views: float = DEFAULT_MIN_VIEWS, window_days: int = STM_WINDOW_DAYS,
        today: str | None = None, dry_run: bool = False,
        sim_threshold: float = SIM_THRESHOLD,
        update_score: float = UPDATE_SCORE) -> dict:
    config = AppConfig.load()
    memory = FactMemory(config.memory_root, track_access=False)

    anchor = today or date.today().isoformat()
    cutoff = (date.fromisoformat(anchor) - timedelta(days=window_days - 1)).isoformat()

    # 승격 조건(조회수 >= min_views)은 최신성 창과 무관하게 STM 전체에 즉시 적용한다.
    # "7일 지남"은 승격의 전제조건이 아니다 — STM에 남을 수 있는 자격일 뿐이다.
    all_stm = memory.all_facts("stm")
    seeds = [f for f in all_stm if f.views >= min_views]

    print(f"STM 전체 {len(all_stm)}개 (즉시 승격 심사)")
    print(f"MTM 승격 기준: 조회수 >= {min_views}  /  결합 기준: 코사인 >= {sim_threshold}")
    print(f"  씨앗 {len(seeds)}개")

    clusters, leftover = _cluster(memory, all_stm, seeds, sim_threshold)
    result = {"aged": len(all_stm), "clusters": len(clusters),
              "add": 0, "update": 0, "dropped": 0}

    judge = _Judge(memory, update_score, UPDATE_CANDIDATES)
    print(f"  ADD/UPDATE 기준: 상호함의 >= {update_score}")

    for ci, cluster in enumerate(clusters, start=1):
        members = cluster["facts"]
        seed_ids = {f.id for f in members if f.views >= min_views}
        print(f"\n  [사안 {ci}] {len(members)}개 승격 "
              f"(조회수 합 {sum(f.views for f in members):.3f})")
        for f in members:
            print(f"      ({'씨앗' if f.id in seed_ids else '동반'} {f.views:.3f}) "
                  f"{f.text[:66]}")
        counts = _promote_facts(memory, members, judge, dry_run)
        result["add"] += counts["add"]
        result["update"] += counts["update"]

    # 조회수 미달로 클러스터에 못 들어간 leftover 중, 최신성 창(7일)까지 벗어난 것은
    # 형태와 무관하게 그냥 버린다. STM에 남을 자격(최신성)도, MTM으로 올라갈 자격
    # (조회수)도 둘 다 없기 때문이다. 문서 코퍼스에 있는지는 보지 않는다 — 있든 없든
    # STM에서는 필요 없다는 판단.
    aged_ids = {f.id for f in memory.aged_out(keep_after=cutoff, tier="stm")}
    evictable = [f for f in leftover if f.id in aged_ids]
    print(f"\n  폐기 {len(evictable)}개 (창을 벗어났고 조회수 미달) "
          f"/ 나머지 {len(leftover) - len(evictable)}개는 아직 창 안 -> STM 유지")
    if evictable:
        for f in evictable[:10]:
            print(f"      - (조회수 {f.views:.3f}) {f.text[:66]}")
        if not dry_run:
            memory.drop([f.id for f in evictable])
    result["dropped"] = len(evictable)

    # MTM 안에 남아 있는 중복 정리. 승격 판정이 1:1이라 여기서만 잡힌다.
    result["deleted"] = _dedup_tier(memory, "mtm", judge, dry_run)

    if dry_run:
        print("\n(dry-run: 아무것도 바꾸지 않았습니다)")
    print("\n[계층 상태]", json.dumps(memory.stats(), ensure_ascii=False))
    return result


def _fact_source_traceable(f) -> bool:
    """f.source_id가 실제 원문 로그 파일로 이어지는지 데이터로 확인한다.

    LLM 판단이 아니다 — LLM에게 "출처가 있어 보이나?"를 물으면 그럴듯한 텍스트를
    보고 있다고 답할 수 있어서, 출처만큼은 파일 존재 여부로 직접 검증한다.
    """
    if not f.source_id:
        return False
    path = (RAW_CHAT if f.source_type == "team_chat" else RAW_AI) / f"{f.source_id}.jsonl"
    return path.exists()


_LTM_JUDGE_PROMPT = """다음은 한 조직의 채팅에서 뽑혀 반복적으로 참조된 사실 문장이다.
이 사실은 문서 코퍼스(공식 문서)에 {doc_status}.

"{text}"

두 가지를 판정해라.

1. truthful: 이 문장이 구체적이고 검증 가능한 형태로 서술돼 있는가(모호한 추측이나
   근거 없는 소문이 아닌가). 문서 근거가 없어도, 채팅에서 구체적 수치·이름·결정으로
   명확히 진술됐다면 truthful로 볼 수 있다.
2. useful: 앞으로도 반복적으로 참조될 실질적 가치가 있는가(단발성 잡담이나 이미
   끝난 일회성 조치가 아닌가. "내일까지", "이번 주" 같은 시한부 표현이 핵심이면
   대개 useful=false다).

반드시 이 형식의 JSON만 출력해라:
{{"truthful": true/false, "useful": true/false,
  "reason": "한 문장으로 각 판정 근거 요약"}}"""


def _parse_ltm_judgment(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def _doc_corroboration(retriever, fact_text: str) -> tuple[float | None, str]:
    """문서 코퍼스에서 가장 가까운 근거를 찾아 LLM 판정에 힌트로 준다.

    **리랭커 제거 이후로 "있다/없다"를 확신 있게 못 가른다.** doc_rag/retriever.py가
    더 이상 리랭커를 안 써서(D:/RAG 벤치마크 실측 결과 — 300M 리랭커가 RRF 단독보다
    오히려 nDCG를 깎았다), `search()`가 돌려주는 점수는 리랭커 로짓이 아니라 RRF
    점수(항상 작은 양수, 0.01~0.03대)다. 옛 문턱(LTM_COVERED_SCORE=0.0, "문서에
    있음 +2.7~+4.7 / 채팅에만 -4.5~-0.6" 실측으로 잡은 값)은 이제 스케일이 안 맞아
    아무 의미가 없다 — RRF 점수는 전부 그 문턱보다 크다.

    그래서 "근거가 있다/없다"를 확정하는 대신, 최상위 후보 청크의 본문 일부를
    그대로 인용해서 LLM에게 넘긴다. 실제로 그 사실을 뒷받침하는지는 문장을 읽은
    LLM이 판단하게 한다 — 우리가 숫자로 확신할 근거가 없어졌기 때문이다.
    """
    if retriever is None:
        return None, "확인 불가(문서 검색기 없음)"
    try:
        hits = retriever.search(fact_text, top_k=1)
    except Exception:  # noqa: BLE001
        hits = []
    if not hits:
        return None, "관련 문서를 못 찾음"
    snippet = hits[0]["text"][:200].replace("\n", " ")
    return hits[0]["score"], f'가장 관련성 높은 문서 조각: "{snippet}..." (이게 이 사실을 실제로 뒷받침하는지는 직접 판단할 것)'


def promote_to_ltm(memory: FactMemory, client: OpenRouterClient | None = None,
                   dry_run: bool = False) -> dict:
    """MTM -> LTM 승격. 3개 기준(출처·진실성·유용성)을 전부 통과해야 승격된다.

    공유적합성(shareable) 기준은 뺐다 — 원래 H6에 있었지만 (사용자 지시로) 제거함.
    실측에서도 이게 병목이었던 적이 없다(traceable/truthful/useful 세 개는 종종
    실패가 났는데 shareable은 거의 항상 통과했다).

    **알려진 한계 (2026-09-03 인계 문서에서 지적됨, 해소 안 됨):**

    이 기준들의 판정 품질을 검증할 진짜 정답셋이 없다. `.scratch/sim_ltm_promotion*.py`
    에서 5개 가설(조회수 임계, 활용률, LLM 내구성 판정 등)을 만들어 비교해봤지만, 그때
    "기준선"으로 쓴 것도 결국 또 다른 LLM 프롬프트였다 — 사람이 라벨링한 것도, 승격 후
    실제로 재참조됐는지 추적한 것도 아니다. 게다가 그 기준선이 조회수·활용률 수치를
    입력으로 받았기 때문에, 조회수 기반 가설들과 비교한 결과는 순환논리였다(채점 기준이
    응시자의 답안을 미리 본 셈).

    이 함수가 쓰는 기준(H6에서 shareable을 뺀 것)은 그 문제에서 상대적으로 자유롭다 —
    truthful/useful 판정에 조회수·활용률을 프롬프트에 안 준다. 다만 여전히 LLM 판정이고,
    사람 라벨이나 사후 재참조 추적으로 검증된 적은 없다. **운영 시작점이지 확정값이 아니다.**

        출처(traceable)  source_id가 실제 원문 로그 파일로 이어지는가 (데이터로 확인)
        진실성(truthful)  구체적·검증 가능한 서술인가 (LLM 판정, 문서 근거 여부를 힌트로 줌)
        유용성(useful)    앞으로도 반복 참조될 가치가 있는가 (LLM 판정)

    셋 다 True여야 승격한다. 하나라도 실패하면 MTM에 그대로 남는다.
    """
    if client is None:
        config = AppConfig.load()
        client = OpenRouterClient(config)

    retriever = None
    try:
        from doc_rag.retriever import DocRetriever
        retriever = DocRetriever()
    except Exception as exc:  # noqa: BLE001
        print(f"  (문서 근거 확인 불가 — {type(exc).__name__}: {exc}. truthful 판정에서 힌트 없이 진행)")

    facts = memory.all_facts("mtm")
    print(f"MTM {len(facts)}건 대상 LTM 승격 심사 (기준: 출처 AND 진실성 AND 유용성)")

    promoted, rows = [], []
    for f in facts:
        traceable = _fact_source_traceable(f)
        _doc_score, doc_status = _doc_corroboration(retriever, f.text)
        msg = client.chat(
            [{"role": "system", "content": "너는 JSON만 출력하는 판정기다."},
             {"role": "user", "content": _LTM_JUDGE_PROMPT.format(text=f.text, doc_status=doc_status)}],
            tools=[],
        )
        judged = _parse_ltm_judgment(msg.get("content"))
        truthful = bool(judged.get("truthful", False))
        useful = bool(judged.get("useful", False))
        ok = traceable and truthful and useful
        rows.append({"id": f.id, "text": f.text, "traceable": traceable,
                     "truthful": truthful, "useful": useful,
                     "reason": judged.get("reason", "")})
        flags = "".join(["출" if traceable else "-", "진" if truthful else "-",
                         "유" if useful else "-"])
        print(f"  [{flags}] {'PROMOTE' if ok else 'hold':7s} {f.text[:56]}")
        if ok:
            promoted.append(f.id)

    result = {"examined": len(facts), "promoted": 0, "rows": rows,
              "rejected_duplicate": 0, "updated_existing": 0}

    # 3개 기준을 통과한 후보라도 그대로 다 새 LTM 레코드로 올리지 않는다. 승격
    # 판정이 사실 하나씩 독립적이라, "가천대학교 소속 참여연구원 알려줘"와
    # "가천대산학협력단이랑 관련된 연구원이 누구야"처럼 다른 질문에서 나온 같은
    # 내용이 둘 다 통과하면 LTM에 중복이 쌓인다. 그래서 이번에 새로 승격되는
    # 후보끼리 서로 병합(흡수)하는 게 아니라, **각 후보를 기존 LTM과 대조**해서
    # 판정한다 — 이미 있으면(중복) 후보를 버리고, 같은 사안인데 정보가 다르면
    # (최신화) 기존 LTM 레코드 문장을 갈아끼우고, 무관하면 그대로 새로 올린다.
    # 기존 LTM 레코드를 흡수 삭제하는 경로는 없어서, 리랭커 오판으로 확정된
    # 레코드가 통째로 사라질 위험이 없다.
    if promoted:
        by_id = {f.id: f for f in facts}
        candidates = [by_id[fid] for fid in promoted]
        judge = _Judge(memory, UPDATE_SCORE, UPDATE_CANDIDATES)
        resolution = _resolve_against_ltm(memory, candidates, judge)

        if not dry_run:
            if resolution["new"]:
                memory.retier(resolution["new"], "ltm")
            for u in resolution["updated"]:
                memory.update_fact(u["target_id"], u["text"],
                                    add_views=by_id[u["id"]].views,
                                    add_returns=by_id[u["id"]].returns)
                memory.drop([u["id"]])
            if resolution["rejected"]:
                memory.drop([r["id"] for r in resolution["rejected"]])

        result["promoted"] = len(resolution["new"])
        result["rejected_duplicate"] = len(resolution["rejected"])
        result["updated_existing"] = len(resolution["updated"])

    if dry_run:
        print("\n(dry-run: 아무것도 바꾸지 않았습니다 — 기존 LTM 대조도 승격 후보 확정 뒤에만 도니 여기선 확인 못 함)")
    extra = []
    if result["rejected_duplicate"]:
        extra.append(f"중복 폐기 {result['rejected_duplicate']}건")
    if result["updated_existing"]:
        extra.append(f"기존 갱신 {result['updated_existing']}건")
    suffix = f" ({', '.join(extra)})" if extra else ""
    print(f"\nLTM 승격: {result['promoted']}/{len(facts)}건{suffix}")
    print("[계층 상태]", json.dumps(memory.stats(), ensure_ascii=False))
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-views", type=float, default=DEFAULT_MIN_VIEWS,
                    help="MTM 승격에 필요한 누적 조회수(기여도 합).")
    ap.add_argument("--window-days", type=int, default=STM_WINDOW_DAYS,
                    help="STM 최신성 기준 일수.")
    ap.add_argument("--today", default=None, help="기준일 (YYYY-MM-DD). 기본은 오늘.")
    ap.add_argument("--sim-threshold", type=float, default=SIM_THRESHOLD,
                    help="같은 사안으로 묶을 코사인 문턱.")
    ap.add_argument("--update-score", type=float, default=UPDATE_SCORE,
                    help="MTM 기존 사실과 같다고 보아 UPDATE할 상호함의 문턱.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--ltm", action="store_true",
                    help="STM->MTM 대신 MTM->LTM 승격을 실행한다(출처/진실성/공유적합성/유용성, LLM 판정 포함).")
    args = ap.parse_args()

    config = AppConfig.load()
    if args.stats:
        memory = FactMemory(config.memory_root, track_access=False)
        print(json.dumps(memory.stats(), ensure_ascii=False, indent=2))
        for f in memory.all_facts():
            print(f"  [{f.tier}] views={f.views:6.3f} ret={f.returns:2d} "
                  f"{f.date} {f.text[:70]}")
        return 0

    if args.ltm:
        memory = FactMemory(config.memory_root, track_access=False)
        promote_to_ltm(memory, dry_run=args.dry_run)
        return 0

    run(min_views=args.min_views, window_days=args.window_days,
        today=args.today, dry_run=args.dry_run, sim_threshold=args.sim_threshold,
        update_score=args.update_score)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

from .config import AppConfig
from .fact_memory import FactMemory

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


def _dedup_mtm(memory: FactMemory, judge: "_Judge", dry_run: bool) -> int:
    """MTM 안의 중복을 찾아 하나로 합친다(DELETE).

    조회수가 높은 쪽을 남긴다. 실제로 답변에 쓰인 표현이 그쪽이기 때문이다.
    지워지는 쪽의 조회수는 남는 쪽으로 넘어간다.
    """
    facts = memory.all_facts("mtm")
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
    result["deleted"] = _dedup_mtm(memory, judge, dry_run)

    if dry_run:
        print("\n(dry-run: 아무것도 바꾸지 않았습니다)")
    print("\n[계층 상태]", json.dumps(memory.stats(), ensure_ascii=False))
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
    args = ap.parse_args()

    config = AppConfig.load()
    if args.stats:
        memory = FactMemory(config.memory_root, track_access=False)
        print(json.dumps(memory.stats(), ensure_ascii=False, indent=2))
        for f in memory.all_facts():
            print(f"  [{f.tier}] views={f.views:6.3f} ret={f.returns:2d} "
                  f"{f.date} {f.text[:70]}")
        return 0

    run(min_views=args.min_views, window_days=args.window_days,
        today=args.today, dry_run=args.dry_run, sim_threshold=args.sim_threshold,
        update_score=args.update_score)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

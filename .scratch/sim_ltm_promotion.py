"""MTM -> LTM 승급 기준 가설 5개를 세워, 지금 쌓인 실제 MTM 데이터로 시뮬레이션한다.

기준으로 쓸 수 있는 실제 필드는 세 갈래뿐이다 (facts.sqlite3 스키마 확인 결과).

    views     검색-답변 귀속 누적 (얼마나 실제로 쓰였나)
    returns   검색에 걸린 횟수 (얼마나 자주 조회 후보에 올랐나) -> views/returns로
              "조회됐을 때 실제로 쓰인 비율"을 만들 수 있다
    text      사실 문장 자체 -> LLM이 "시간이 지나도 안 변할 사실인가"를 판정할 수 있다

세션 간 교차 검증(같은 사실이 여러 세션에서 독립적으로 확인됐는가)은 시도했지만,
지금 데이터에는 MTM 중복 통합(absorb) 이벤트가 0건이라 측정 불가 — 그래서 가설에서
뺐다. 정직하게 "측정 가능한 것만" 썼다.

가설 5개:
    H1  조회수 절대량        views >= 15  (STM->MTM 임계 3.0의 5배)
    H2  활용률               views/returns >= 0.5
    H3  LLM 내구성 판정       조직/구조적 사실(안 변함) vs 맥락적 사실(곧 낡음)
    H4  복합 점수             정규화 views*0.5 + 정규화 활용률*0.5 >= 0.5
    H5  보수적 결합           H1 AND H3 (둘 다 만족해야 승급)

분석 기준선(reference)은 H3와 별개로, 더 신중한 LLM 프롬프트로 한 번 더 판정한
"이 사실이 영구 지식으로 남을 가치가 있는가"다 — 완전한 정답은 아니지만, 각 가설이
사람이 보기에 그럴듯한 판단과 얼마나 겹치는지 보는 비교 기준으로 쓴다.
"""
import json, sys
sys.path.insert(0, r"D:\LAB_RAG\Org-AI-Body")
import os
os.chdir(r"D:\LAB_RAG\Org-AI-Body")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

from pathlib import Path
from org_agent_mvp.config import AppConfig
from org_agent_mvp.fact_memory import FactMemory
from org_agent_mvp.openrouter_client import OpenRouterClient

DURABILITY_PROMPT = """다음은 한 조직의 채팅에서 뽑혀 반복적으로 참조된 사실 문장이다.

"{text}"

이 문장이 **시간이 지나도 잘 안 변하는, 조직의 영구적 지식**(예: 조직 구조, 담당자와
소속, 제품 사양, 확정된 정책·계약 조건)인지, 아니면 **곧 낡거나 상황이 바뀌는 맥락적
정보**(예: 오늘 회의 시간, 이번 주 진행 상황, 임시 조치, 아직 미확정인 계획)인지
판정해라.

반드시 이 형식의 JSON만 출력해라:
{{"durable": true 또는 false, "reason": "한 문장 이유"}}"""

REFERENCE_PROMPT = """다음은 한 조직의 채팅에서 뽑혀 반복적으로 참조된 사실 문장이다.
이 사실은 조회수(views) {views:.1f}, 검색 대비 실사용률 {util:.0%}을 기록했다.

"{text}"

이 사실이 **영구 지식 저장소(LTM)로 승급시킬 가치가 있는지** 종합적으로 판단해라.
고려할 것: (1) 시간이 지나도 유효한가 (2) 조직 운영에 반복적으로 참조될 만한가
(3) 이미 알려진 상식이 아니라 이 조직 고유의 정보인가.

반드시 이 형식의 JSON만 출력해라:
{{"promote": true 또는 false, "reason": "한 문장 이유"}}"""


def _parse_json(text):
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


def ask(client, prompt):
    msg = client.chat(
        [{"role": "system", "content": "너는 JSON만 출력하는 판정기다."},
         {"role": "user", "content": prompt}],
        tools=[],
    )
    return _parse_json(msg.get("content"))


def main():
    config = AppConfig.load()
    client = OpenRouterClient(config)
    memory = FactMemory(Path("memory_seed"), track_access=False)

    facts = memory.all_facts("mtm")
    print(f"MTM 전체 {len(facts)}건 대상\n")

    rows = []
    for i, f in enumerate(facts, 1):
        util = (f.views / f.returns) if f.returns > 0 else 0.0
        durability = ask(client, DURABILITY_PROMPT.format(text=f.text))
        reference = ask(client, REFERENCE_PROMPT.format(text=f.text, views=f.views, util=util))
        row = {
            "id": f.id, "text": f.text, "views": f.views, "returns": f.returns,
            "util": util,
            "durable": bool(durability.get("durable", False)),
            "durable_reason": durability.get("reason", ""),
            "ref_promote": bool(reference.get("promote", False)),
            "ref_reason": reference.get("reason", ""),
        }
        rows.append(row)
        print(f"[{i}/{len(facts)}] views={f.views:6.2f} util={util:4.0%} "
              f"durable={row['durable']!s:5s} ref={row['ref_promote']!s:5s}  {f.text[:50]}")

    json.dump(rows, open(".scratch/ltm_promotion_data.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    # ---- 정규화 (H4용) ----
    max_views = max((r["views"] for r in rows), default=1.0) or 1.0
    for r in rows:
        r["norm_views"] = r["views"] / max_views
        r["composite"] = 0.5 * r["norm_views"] + 0.5 * r["util"]

    HYPOTHESES = {
        "H1_조회수(>=15)": lambda r: r["views"] >= 15,
        "H2_활용률(>=0.5)": lambda r: r["util"] >= 0.5,
        "H3_LLM내구성판정": lambda r: r["durable"],
        "H4_복합점수(>=0.5)": lambda r: r["composite"] >= 0.5,
        "H5_조회수AND내구성": lambda r: (r["views"] >= 15) and r["durable"],
    }

    ref_set = {r["id"] for r in rows if r["ref_promote"]}
    print(f"\n\n{'='*70}\n분석 기준선(reference LLM) 승급 판정: {len(ref_set)}/{len(rows)}건\n{'='*70}")

    summary = {}
    for name, rule in HYPOTHESES.items():
        promote_set = {r["id"] for r in rows if rule(r)}
        overlap = promote_set & ref_set
        precision = len(overlap) / len(promote_set) if promote_set else 0.0
        recall = len(overlap) / len(ref_set) if ref_set else 0.0
        summary[name] = {
            "promote_count": len(promote_set),
            "precision_vs_ref": round(precision, 3),
            "recall_vs_ref": round(recall, 3),
        }
        print(f"\n[{name}]")
        print(f"  승급 {len(promote_set)}/{len(rows)}건  "
              f"기준선과 일치율(precision) {precision:.1%}  포착률(recall) {recall:.1%}")
        promoted = [r for r in rows if rule(r)]
        held = [r for r in rows if not rule(r)]
        for r in sorted(promoted, key=lambda x: -x["views"])[:3]:
            print(f"    + 승급: (views {r['views']:.1f}) {r['text'][:60]}")
        for r in sorted(held, key=lambda x: -x["views"])[:2]:
            print(f"    - 보류: (views {r['views']:.1f}) {r['text'][:60]}")

    json.dump(summary, open(".scratch/ltm_promotion_summary.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\n\n완료 — 상세 데이터: .scratch/ltm_promotion_data.json")


if __name__ == "__main__":
    main()

"""H6: 사용자가 지정한 4개 기준(출처/진실성/공유적합성/유용성)을 전부 만족해야
승급하는 가설. 앞서(v1) 만든 5개 가설과 별개로 돌려서 비교한다.

    출처 여부      source_id가 있고, 그 원문 로그 파일이 실제로 존재하는가 (데이터로 확인,
                   LLM 판단 아님 -- 거짓 보고를 막기 위해 파일 존재 자체를 검사한다)
    진실성         이 사실이 문서 코퍼스(LTM)에 근거가 있는지 리랭커로 확인한다. 이전에
                   측정된 바 있는 LTM_COVERED_SCORE(0.0) 문턱을 그대로 재사용한다
                   (문서에 있음 +2.7~+4.7 / 채팅에만 있음 -4.5~-0.6, 격차 3.3).
                   문서 근거가 없으면 "허위"가 아니라 "미검증"으로 표시하고, 최종 판단은
                   LLM에게 넘긴다(채팅 고유 지식은 원래 문서에 없는 게 정상이라서).
    공유 적합성    LLM 판단 -- 개인정보/민감정보/특정인만 알아야 할 내용이 아니라
                   조직 전체에 장기간 공유해도 되는 내용인지.
    유용성         LLM 판단 -- 앞으로도 반복 참조될 실질적 가치가 있는지.

4개 전부 통과해야 승급이다. 하나라도 실패하면 보류.
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

RAW_AI = Path("data/raw/ai_chat")
RAW_TEAM = Path("data/raw/chat")

LTM_COVERED_SCORE = 0.0  # memory_promote.py에서 실측해 쓰던 값 그대로 재사용

JUDGE_PROMPT = """다음은 한 조직의 채팅에서 뽑힌 사실 문장이다. 이 사실은 문서
코퍼스(공식 문서)에 {doc_status}.

"{text}"

세 가지를 판정해라.

1. truthful: 이 문장이 구체적이고 검증 가능한 형태로 서술돼 있는가(모호한 추측이나
   근거 없는 소문이 아닌가). 문서 근거가 없어도, 채팅에서 구체적 수치·이름·결정으로
   명확히 진술됐다면 truthful로 볼 수 있다.
2. shareable: 개인의 연봉·연락처·인사평가처럼 특정인만 알아야 할 민감정보가 아니라,
   조직 전체에 장기간 공유해도 괜찮은 내용인가.
3. useful: 앞으로도 반복적으로 참조될 실질적 가치가 있는가(단발성 잡담이나 이미
   끝난 일회성 조치가 아닌가).

반드시 이 형식의 JSON만 출력해라:
{{"truthful": true/false, "shareable": true/false, "useful": true/false,
  "reason": "한 문장으로 각 판정 근거 요약"}}"""


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


def source_traceable(f) -> bool:
    """source_id가 실제 원문 로그 파일로 이어지는지 데이터로 확인한다."""
    if not f.source_id:
        return False
    if f.source_type == "team_chat":
        return (RAW_TEAM / f"{f.source_id}.jsonl").exists()
    return (RAW_AI / f"{f.source_id}.jsonl").exists()


def doc_corroboration(fact_text: str):
    """리랭커로 문서 코퍼스 근거 여부를 확인한다. (score, status_text)."""
    try:
        from doc_rag.retriever import DocRetriever
        retriever = doc_corroboration._retriever
    except Exception as exc:
        return None, f"확인 불가({type(exc).__name__})"
    try:
        hits = retriever.search(fact_text, top_k=1)
    except Exception:
        hits = []
    if not hits:
        return None, "근거 문서를 못 찾음"
    score = hits[0]["score"]
    status = "근거가 있다" if score >= LTM_COVERED_SCORE else "근거를 못 찾음(채팅 고유 정보로 추정)"
    return score, status


def main():
    from doc_rag.retriever import DocRetriever
    doc_corroboration._retriever = DocRetriever()

    config = AppConfig.load()
    client = OpenRouterClient(config)
    memory = FactMemory(Path("memory_seed"), track_access=False)
    facts = memory.all_facts("mtm")
    print(f"MTM 전체 {len(facts)}건 대상 (H6: 4개 기준 전부 AND)\n")

    rows = []
    for i, f in enumerate(facts, 1):
        traceable = source_traceable(f)
        doc_score, doc_status = doc_corroboration(f.text)
        judged = {}
        msg = client.chat(
            [{"role": "system", "content": "너는 JSON만 출력하는 판정기다."},
             {"role": "user", "content": JUDGE_PROMPT.format(text=f.text, doc_status=doc_status)}],
            tools=[],
        )
        judged = _parse_json(msg.get("content"))

        row = {
            "id": f.id, "text": f.text, "views": f.views, "source_id": f.source_id,
            "traceable": traceable,
            "doc_score": doc_score, "doc_status": doc_status,
            "truthful": bool(judged.get("truthful", False)),
            "shareable": bool(judged.get("shareable", False)),
            "useful": bool(judged.get("useful", False)),
            "reason": judged.get("reason", ""),
        }
        row["promote_H6"] = row["traceable"] and row["truthful"] and row["shareable"] and row["useful"]
        rows.append(row)

        flags = "".join([
            "출" if row["traceable"] else "-",
            "진" if row["truthful"] else "-",
            "공" if row["shareable"] else "-",
            "유" if row["useful"] else "-",
        ])
        print(f"[{i}/{len(facts)}] [{flags}] {'PROMOTE' if row['promote_H6'] else 'hold':7s} "
              f"doc={doc_status[:14]:14s} {f.text[:44]}")

    json.dump(rows, open(".scratch/ltm_promotion_v2_data.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    promoted = [r for r in rows if r["promote_H6"]]
    print(f"\n\n{'='*70}\nH6 승급: {len(promoted)}/{len(rows)}건\n{'='*70}")

    # 어떤 기준이 병목인지 진단 -- 그 기준 하나만 실패해서 탈락한 건수
    for crit in ["traceable", "truthful", "shareable", "useful"]:
        blockers = [r for r in rows if not r[crit] and
                    all(r[c] for c in ["traceable", "truthful", "shareable", "useful"] if c != crit)]
        print(f"  '{crit}' 단독 병목으로 탈락: {len(blockers)}건")

    fail_counts = {"traceable": 0, "truthful": 0, "shareable": 0, "useful": 0}
    for r in rows:
        for c in fail_counts:
            if not r[c]:
                fail_counts[c] += 1
    print(f"\n  기준별 전체 실패 건수: {fail_counts}")

    print("\n  승급 예시 (조회수 높은 순 5개):")
    for r in sorted(promoted, key=lambda x: -x["views"])[:5]:
        print(f"    + {r['text'][:60]}")

    print("\n  보류 예시 (기준을 하나만 놓친 것들):")
    near_miss = [r for r in rows if not r["promote_H6"] and
                 sum(r[c] for c in ["traceable", "truthful", "shareable", "useful"]) == 3]
    for r in near_miss[:6]:
        failed = [c for c in ["traceable", "truthful", "shareable", "useful"] if not r[c]]
        print(f"    - ({failed[0]} 실패) {r['text'][:55]}  |  {r['reason'][:50]}")

    print("\n완료 — 상세: .scratch/ltm_promotion_v2_data.json")


if __name__ == "__main__":
    main()

import json, sys
sys.path.insert(0, r"D:\LAB_RAG\Org-AI-Body")
import os
os.chdir(r"D:\LAB_RAG\Org-AI-Body")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

from org_agent_mvp.config import AppConfig
from org_agent_mvp.__main__ import build_runtime

QUESTIONS = [
    "이 과제 연구책임자가 누구야?",
    "피씨티는 이 프로젝트에서 무슨 역할을 맡고 있어?",
    "가천대산학협력단이랑 관련된 연구원이 누구누구 있어?",
    "HMD는 몇 차년도에 개발하는 부분이야?",
    "이 프로젝트 전문기관이 어디야?",
    "선행기술 보유 기관으로 파악된 곳이 어디어디야?",
    "정정일 님은 어느 기관 소속이고 무슨 역할이야?",
    "이 과제의 특허 관련 협력 법률사무소가 어디어디 있어?",
    "스마트 글래스(Low Vision Smart Glass)는 어떤 제품 구성으로 나뉘어?",
    "이 프로젝트 사업 기간이 어떻게 돼?",
]

config = AppConfig.load()
runtime = build_runtime(config, use_mock=False, record=False)

results = []
for i, q in enumerate(QUESTIONS, 1):
    r = runtime.run(q)
    tools = [tc.get("tool") for tc in (r.get("trace") or {}).get("tool_calls", [])]
    graph_hit = "search_graph" in tools
    results.append({"q": q, "a": r.get("answer", ""), "tools": tools, "graph_hit": graph_hit})
    print(f"\n{'='*80}\n[{i}] {q}\n{'-'*80}")
    print(f"도구 호출: {tools}  (그래프 걸림: {graph_hit})")
    print(r.get("answer", ""))

json.dump(results, open(".scratch/eval_graph_results.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print(f"\n\n그래프 도구 걸린 건수: {sum(r['graph_hit'] for r in results)}/{len(results)}")

import json, random, sys
sys.path.insert(0, r"D:\LAB_RAG\Org-AI-Body")
import os
os.chdir(r"D:\LAB_RAG\Org-AI-Body")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

from org_agent_mvp.config import AppConfig
from org_agent_mvp.__main__ import build_runtime

# 그래프 스키마(조직/인물/제품/과업 관계)를 겨냥한 질문 풀. 표현을 다양하게 섞어서
# 같은 문장만 반복하지 않게 한다 -> 그래프 도구가 실제로 걸릴 확률을 여러 각도로 시도.
POOL = [
    "이 과제 연구책임자가 누구야?",
    "이 프로젝트 총괄책임자가 누구야?",
    "피씨티는 이 프로젝트에서 무슨 역할을 맡고 있어?",
    "피씨티 대표이사가 누구고 어떤 역할이야?",
    "가천대산학협력단이랑 관련된 연구원이 누구누구 있어?",
    "가천대학교 소속 참여연구원 명단 알려줘",
    "HMD는 몇 차년도에 개발하는 부분이야?",
    "이 프로젝트 전문기관이 어디야?",
    "이 과제 전담기관이 어디인지 알려줘",
    "선행기술 보유 기관으로 파악된 곳이 어디어디야?",
    "선행기술 조사에서 확인된 보유 기관 목록 알려줘",
    "정정일 님은 어느 기관 소속이고 무슨 역할이야?",
    "정정일 대표는 어느 회사 소속이야?",
    "이 과제의 특허 관련 협력 법률사무소가 어디어디 있어?",
    "특허 출원에 협력한 법률사무소 명단 알려줘",
    "스마트 글래스(Low Vision Smart Glass)는 어떤 제품 구성으로 나뉘어?",
    "Low Vision Smart Glass 제품은 어떤 부품들로 이루어져?",
    "이 프로젝트 사업 기간이 어떻게 돼?",
    "동국대산학협력단은 이 과제에서 어떤 역할이야?",
    "경북대산학협력단은 무슨 역할로 참여하고 있어?",
    "임수빈 연구원은 어느 기관 소속이야?",
    "이상웅 교수는 무슨 역할을 맡고 있어?",
    "그린광학은 이 과제와 어떤 관계야?",
    "제일특허법인은 이 프로젝트에서 무슨 업무를 담당해?",
    "산기평은 이 과제에서 어떤 역할이야?",
]

TARGET = 10
MAX_ATTEMPTS = 60

config = AppConfig.load()
runtime = build_runtime(config, use_mock=False, record=False)

hits = []
tried = []
random.shuffle(POOL)
i = 0
attempt = 0

while len(hits) < TARGET and attempt < MAX_ATTEMPTS:
    if i >= len(POOL):
        i = 0
        random.shuffle(POOL)
    q = POOL[i]
    i += 1
    attempt += 1

    r = runtime.run(q)
    tools = [tc.get("tool") for tc in (r.get("trace") or {}).get("tool_calls", [])]
    graph_hit = "search_graph" in tools
    tried.append({"q": q, "tools": tools, "graph_hit": graph_hit})
    print(f"[시도 {attempt}] {'O' if graph_hit else 'x'} {q}  tools={tools}")

    if graph_hit:
        hits.append({"q": q, "a": r.get("answer", ""), "tools": tools})
        print(f"  -> 확보 {len(hits)}/{TARGET}")

json.dump(hits, open(".scratch/eval_graph10_results.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
json.dump(tried, open(".scratch/eval_graph10_attempts.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)

print(f"\n종료: 그래프 걸린 답변 {len(hits)}건 확보 (총 시도 {attempt}회, 적중률 {len(hits)/attempt:.1%})")

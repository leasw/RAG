"""같은 10개 질문을, 메모리(STM/MTM) + 문서 검색(search_documents) 없이
오로지 그래프RAG(search_graph) 결과만 근거로 답변하게 시킨다.

전체 파이프라인(runtime.run)은 LLM이 도구를 자유롭게 골라서 메모리/문서/그래프가
섞인 답을 낸다. 여기서는 그 선택권을 없애고, GraphSearchTool을 직접 호출해 얻은
결과만 컨텍스트로 주고 LLM이 그 안에서만 답하게 강제한다 -> "그래프RAG만 썼을 때
실제로 뭘 알 수 있는지"를 격리해서 본다.

    python .scratch/eval_graph_only.py
"""
import json, sys
sys.path.insert(0, r"D:\LAB_RAG\Org-AI-Body")
import os
os.chdir(r"D:\LAB_RAG\Org-AI-Body")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

from org_agent_mvp.config import AppConfig
from org_agent_mvp.openrouter_client import OpenRouterClient
from org_agent_mvp.graph_tool import GraphSearchTool

GRAPH_ONLY_PROMPT = """너는 지식 그래프 검색 결과만 보고 답하는 어시스턴트다.

아래는 그래프 검색으로 찾은 관계 사실(facts)과 근거 청크(results)다. 이것 외에는
아무것도 모른다고 가정해라 - 메모리도, 문서 원문도, 상식도 쓰지 마라.

[그래프 검색 결과]
{graph_json}

[사용자 질문]
{question}

위 그래프 결과 안에서만 답해라. 근거가 없으면 "그래프 검색 결과에 없음"이라고
명시해라. 답변 마지막에 어떤 관계 사실(facts)을 근거로 썼는지 적어라."""

QUESTIONS = [r["q"] for r in json.load(open(".scratch/eval_graph10_results.json", encoding="utf-8"))]

config = AppConfig.load()
client = OpenRouterClient(config)
tool = GraphSearchTool()

results = []
for i, q in enumerate(QUESTIONS, 1):
    graph_out = tool.run({"query": q, "top_k": 5})
    graph_json = json.dumps({
        "resolved_entities": graph_out.get("resolved_entities", []),
        "facts": graph_out.get("facts", []),
        "results": [
            {"content": r["content"][:400], "source": r["source_ref"]}
            for r in graph_out.get("results", [])
        ],
    }, ensure_ascii=False, indent=2)

    msg = client.chat(
        [{"role": "system", "content": "너는 주어진 자료만 근거로 답하는 어시스턴트다."},
         {"role": "user", "content": GRAPH_ONLY_PROMPT.format(graph_json=graph_json, question=q)}],
        tools=[],
    )
    answer = (msg.get("content") or "").strip()

    results.append({
        "q": q,
        "graph_only_answer": answer,
        "graph_result_count": graph_out.get("result_count", 0),
        "graph_facts": graph_out.get("facts", []),
        "graph_note": graph_out.get("note", ""),
        "graph_error": graph_out.get("error", ""),
    })
    print(f"\n{'='*80}\n[{i}] {q}")
    print(f"  그래프 결과 {graph_out.get('result_count', 0)}건 / facts {len(graph_out.get('facts', []))}건")
    print(f"  답변: {answer[:200]}")

json.dump(results, open(".scratch/eval_graph_only_results.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print(f"\n\n완료: {len(results)}건 저장")

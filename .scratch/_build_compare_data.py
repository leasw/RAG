import json

pipeline = json.load(open(".scratch/eval_graph10_results.json", encoding="utf-8"))
graph_only = json.load(open(".scratch/eval_graph_only_results.json", encoding="utf-8"))

assert len(pipeline) == len(graph_only) == 10

merged = []
for p, g in zip(pipeline, graph_only):
    assert p["q"] == g["q"]
    merged.append({
        "q": p["q"],
        "pipeline_answer": p["a"],
        "pipeline_tools": p["tools"],
        "graph_only_answer": g["graph_only_answer"],
        "graph_result_count": g["graph_result_count"],
        "graph_facts_count": len(g["graph_facts"]),
        "graph_note": g["graph_note"],
    })

open(".scratch/compare_data.json", "w", encoding="utf-8").write(
    json.dumps(merged, ensure_ascii=False)
)
print("merged", len(merged))
for i, m in enumerate(merged, 1):
    empty = m["graph_result_count"] == 0
    print(i, "EMPTY" if empty else "ok", m["q"][:40])

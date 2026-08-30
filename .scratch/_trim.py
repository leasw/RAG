lines = open(".scratch/graph_rag_eval.html", encoding="utf-8").readlines()
kept = lines[:424]  # 1-indexed line 424 is </script> of the good block
open(".scratch/graph_rag_eval.html", "w", encoding="utf-8").writelines(kept)
print("kept", len(kept), "lines")

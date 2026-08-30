import json

path = ".scratch/graph_rag_eval.html"
html = open(path, encoding="utf-8").read()
new_data = json.load(open(".scratch/eval_graph10_embed.json", encoding="utf-8"))
payload = json.dumps(new_data, ensure_ascii=False).replace("</script>", "<\\/script>")

start_tag = '<script id="eval-data" type="application/json">'
si = html.index(start_tag)
content_start = si + len(start_tag)
ei = html.index("</script>", content_start)

new_html = html[:content_start] + payload + html[ei:]
assert new_html != html
open(path, "w", encoding="utf-8").write(new_html)
print("swapped, new len", len(new_html))

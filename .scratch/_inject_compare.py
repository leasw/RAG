import json

path = ".scratch/graph_only_compare.html"
html = open(path, encoding="utf-8").read()
data = json.load(open(".scratch/compare_data.json", encoding="utf-8"))
payload = json.dumps(data, ensure_ascii=False).replace("</script>", "<\\/script>")

placeholder = "__DATA_JSON__"
assert placeholder in html
html2 = html.replace(placeholder, payload)
open(path, "w", encoding="utf-8").write(html2)
print("injected, len", len(html2))

"""parsed_chunks.json(40MB, 한 줄)을 VS Code가 열 수 있는 크기로 쪼갠다.

    python .scratch/shard_parsed_chunks.py

문서 단위로 묶은 배열을 N개 파일로 나누고, 각 파일은 보기 편하게 들여쓰기한다.
"""
import json
from pathlib import Path

SRC = Path(".scratch/parsed_chunks.json")
OUT_DIR = Path(".scratch/parsed_chunks_shards")
DOCS_PER_SHARD = 60  # 문서 60개씩 -> 파일당 대략 2~3MB

def main():
    docs = json.loads(SRC.read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.json"):
        old.unlink()

    n_shards = 0
    for i in range(0, len(docs), DOCS_PER_SHARD):
        shard = docs[i:i + DOCS_PER_SHARD]
        path = OUT_DIR / f"shard_{i // DOCS_PER_SHARD + 1:03d}.json"
        path.write_text(json.dumps(shard, ensure_ascii=False, indent=1), encoding="utf-8")
        n_shards += 1
        print(f"  {path.name}: 문서 {len(shard)}건, {path.stat().st_size/1024/1024:.1f}MB")

    print(f"\n총 {n_shards}개 샤드 -> {OUT_DIR}")


if __name__ == "__main__":
    main()

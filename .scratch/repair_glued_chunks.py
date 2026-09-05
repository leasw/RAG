"""이미 인덱싱된 chunks.sqlite3에서 표 행이 눌어붙은 청크를 제자리 복구한다.

재인제스천(hwp5html+Docling 재실행) 없이, 이미 추출된 텍스트에 저장된 패턴만
고친다 — chunking.py에 넣은 _repair_glued_table_rows()와 같은 로직을 그대로 쓴다.

    python .scratch/repair_glued_chunks.py            # 실제 적용
    python .scratch/repair_glued_chunks.py --dry-run  # 몇 건이 바뀌는지만
"""
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, r"D:\LAB_RAG\Org-AI-Body")
from doc_rag.chunking import _repair_glued_table_rows  # 같은 로직 재사용

DB = Path("index/doc_rag/chunks.sqlite3")


def main():
    dry_run = "--dry-run" in sys.argv
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT chunk_id, text FROM chunks").fetchall()

    changed = []
    for r in rows:
        fixed = _repair_glued_table_rows(r["text"])
        if fixed != r["text"]:
            changed.append((r["chunk_id"], r["text"], fixed))

    print(f"전체 {len(rows)}개 중 {len(changed)}개 수정 대상")
    for chunk_id, before, after in changed[:3]:
        print(f"\n  [{chunk_id}]")
        print(f"    수정 전: {before[:120]!r}")
        print(f"    수정 후: {after[:120]!r}")

    if dry_run:
        print("\n(dry-run: 실제로 반영 안 함)")
        return

    for chunk_id, _before, after in changed:
        conn.execute("UPDATE chunks SET text = ?, n_chars = ? WHERE chunk_id = ?",
                     (after, len(after), chunk_id))
    conn.commit()
    print(f"\n{len(changed)}건 반영 완료.")
    print("주의: 텍스트가 바뀐 청크는 embeddings.npy의 벡터가 근소하게 낡았습니다 "
          "(줄바꿈만 추가돼 의미 변화는 거의 없지만, 정합성을 위해선 재임베딩 권장).")


if __name__ == "__main__":
    main()

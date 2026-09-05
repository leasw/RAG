"""hwp 청크(11,172개) 대상으로 파싱 깨짐 의심 사례를 자동 탐지한다.

    python .scratch/audit_hwp_chunks.py

검사 항목:
    1. 깨진 문자(mojibake)     대체문자(U+FFFD), 사설영역(PUA), 제어문자 비율
    2. HTML 태그 잔존          hwp5html -> lxml 변환이 안 벗겨진 <td>, <tr>, &nbsp; 등
    3. 표 깨짐                 마크다운 표에서 행마다 파이프(|) 개수가 다름 (병합셀 오류 의심)
    4. 반복/중복 문자열         같은 짧은 구절이 한 청크 안에서 비정상적으로 반복
    5. 텅 빈/의미없는 청크     한글·영문·숫자가 거의 없이 기호만 있는 경우
"""
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path("index/doc_rag/chunks.sqlite3")

CTRL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
HTML_TAG_RE = re.compile(r"</?(td|tr|table|span|div|p|br|nbsp|font)\b", re.I)
HTML_ENTITY_RE = re.compile(r"&(nbsp|amp|lt|gt|quot|#\d+);")
KOREAN_ENG_NUM_RE = re.compile("[가-힣A-Za-z0-9]")


def _count_pua(text: str) -> int:
    """유니코드 사설영역(PUA) 문자 개수. 한글 완성형 범위 밖의, 폰트별로 제멋대로
    매핑되는 영역이라 파싱이 깨지면 여기로 떨어지는 경우가 많다."""
    n = 0
    for ch in text:
        o = ord(ch)
        if 0xE000 <= o <= 0xF8FF or 0xF0000 <= o <= 0xFFFFD or 0x100000 <= o <= 0x10FFFD:
            n += 1
    return n


def check_chunk(text: str) -> list[str]:
    issues = []
    n = len(text) or 1

    if "�" in text:
        issues.append(f"replacement_char x{text.count(chr(0xfffd))}")

    pua = _count_pua(text)
    if pua > 0:
        issues.append(f"PUA_char x{pua}")

    ctrl = len(CTRL_RE.findall(text))
    if ctrl > 0:
        issues.append(f"control_char x{ctrl}")

    html_tags = HTML_TAG_RE.findall(text)
    if html_tags:
        issues.append(f"html_tag_leak x{len(html_tags)} ({Counter(html_tags).most_common(3)})")

    html_ents = HTML_ENTITY_RE.findall(text)
    if html_ents:
        issues.append(f"html_entity_leak x{len(html_ents)}")

    # 표 파이프 개수 불일치 (연속된 |...| 행들의 컬럼 수가 서로 다름)
    table_lines = [l for l in text.split("\n") if l.strip().startswith("|")]
    if len(table_lines) >= 2:
        pipe_counts = Counter(l.count("|") for l in table_lines)
        if len(pipe_counts) > 2:  # 헤더/구분선 제외하고도 들쭉날쭉하면
            issues.append(f"table_pipe_mismatch {dict(pipe_counts)}")

    # 짧은 구절(4자 이상) 과다 반복 -> 병합 셀 중복 등
    words = re.findall("[가-힣A-Za-z0-9]{4,}", text)
    if words:
        common = Counter(words).most_common(1)[0]
        if common[1] >= 6 and common[1] / len(words) > 0.15:
            issues.append(f"repeated_phrase '{common[0]}' x{common[1]}")

    # 의미있는 문자 비율이 너무 낮음
    useful = len(KOREAN_ENG_NUM_RE.findall(text))
    if n > 30 and useful / n < 0.3:
        issues.append(f"low_content_ratio {useful/n:.0%}")

    return issues


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.chunk_id, c.text, d.file_name, d.rel_path "
        "FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
        "WHERE d.format = 'hwp' AND d.status = 'ok'"
    ).fetchall()

    flagged = []
    issue_tally = Counter()
    for r in rows:
        issues = check_chunk(r["text"])
        if issues:
            flagged.append({"chunk_id": r["chunk_id"], "file_name": r["file_name"],
                            "rel_path": r["rel_path"], "issues": issues, "text": r["text"]})
            for i in issues:
                issue_tally[i.split()[0].split("(")[0]] += 1

    print(f"검사 대상 {len(rows)}개 청크 중 {len(flagged)}개({len(flagged)/len(rows):.1%})에서 이상 신호 발견\n")
    print("유형별 건수:")
    for k, v in issue_tally.most_common():
        print(f"  {k}: {v}건")

    json.dump(flagged, open(".scratch/hwp_audit_flagged.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n상세: .scratch/hwp_audit_flagged.json")

    print("\n=== 유형별 샘플 3개씩 ===")
    by_type: dict[str, list] = {}
    for item in flagged:
        t = item["issues"][0].split()[0].split("(")[0]
        by_type.setdefault(t, []).append(item)
    for t, items in by_type.items():
        print(f"\n--- {t} ({len(items)}건) ---")
        for it in items[:3]:
            print(f"  [{it['file_name']}] {it['chunk_id']}")
            print(f"    이슈: {it['issues']}")
            print(f"    텍스트: {it['text'][:150]!r}")


if __name__ == "__main__":
    main()

"""병합 셀을 복제하지 않는 Markdown 표 직렬화기.

Docling 기본 직렬화기 두 가지 모두 이 코퍼스에는 맞지 않았다.

- `TripletTableSerializer` (HybridChunker 기본): 셀마다 "행헤더, 열 = 값" 삼중항을
  뽑는다. 병합 영역이 원본 값으로 채워져 있어서 관공서 서식처럼 병합이 많은 표에서는
  같은 문장이 수십 번 반복된다.

      과 제 명, 20012260 = 시야확보 주변위험 보조 및 ... . 과 제 명,  = . 과 제 명,  = .

- `MarkdownTableSerializer`: 표 모양은 살지만 병합 영역 복제는 그대로다.

여기서는 `TableCell.start_row_offset_idx / start_col_offset_idx`를 보고 셀이 실제로
시작하는 자리에서만 값을 쓰고, 나머지 병합 자리는 빈 칸으로 둔다. 열 정렬(= 라벨과
값의 대응)은 유지하면서 중복 텍스트가 임베딩에 들어가지 않는다.

모든 형식(pdf/docx/pptx/xlsx/hwp)에 동일하게 적용된다.
"""

from __future__ import annotations

from typing import Any

from docling_core.transforms.chunker.hierarchical_chunker import (
    ChunkingDocSerializer,
    ChunkingSerializerProvider,
)
from docling_core.transforms.serializer.base import BaseTableSerializer, SerializationResult
from docling_core.transforms.serializer.common import create_ser_result


def _cell_text(cell) -> str:
    text = getattr(cell, "text", None) or ""
    return text.replace("\n", " ").replace("|", "&#124;").strip()


def table_to_markdown(item) -> str:
    grid = getattr(getattr(item, "data", None), "grid", None)
    if not grid:
        return ""

    rows: list[list[str]] = []
    for r, row in enumerate(grid):
        rendered: list[str] = []
        for c, cell in enumerate(row):
            start_r = getattr(cell, "start_row_offset_idx", r)
            start_c = getattr(cell, "start_col_offset_idx", c)
            # 병합 영역의 첫 칸에서만 값을 쓴다.
            rendered.append(_cell_text(cell) if (start_r == r and start_c == c) else "")
        rows.append(rendered)

    # 통째로 빈 행은 버린다(병합 때문에 생긴 잔여 행).
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


class CompactTableSerializer(BaseTableSerializer):
    """병합 셀 값을 한 번만 쓰는 Markdown 표 직렬화기."""

    def serialize(self, *, item, doc_serializer, doc, **kwargs: Any) -> SerializationResult:
        parts: list[SerializationResult] = []

        caption = doc_serializer.serialize_captions(item=item, **kwargs)
        if caption.text:
            parts.append(caption)

        if item.self_ref not in doc_serializer.get_excluded_refs(**kwargs):
            table_text = table_to_markdown(item)
            if table_text:
                parts.append(create_ser_result(text=table_text, span_source=item))

        return create_ser_result(
            text="\n\n".join(p.text for p in parts), span_source=parts
        )


class CompactSerializerProvider(ChunkingSerializerProvider):
    def get_serializer(self, doc):
        return ChunkingDocSerializer(doc=doc, table_serializer=CompactTableSerializer())

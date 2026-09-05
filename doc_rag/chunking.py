"""Docling HybridChunker 기반 청킹 + 형식별 파라미터.

HybridChunker는 문서 구조(heading / 표 / 리스트)를 먼저 따라 자르고, 토크나이저로
센 토큰 수가 max_tokens를 넘으면 다시 쪼개며, 너무 짧은 이웃 청크는 병합합니다.
형식마다 텍스트 밀도가 달라서(pptx는 조각조각, xlsx는 표 한 덩어리) max_tokens와
overlap을 config의 chunking.by_format에서 다르게 줍니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

# HWP -> 마크다운(_table_to_markdown) -> Docling(InputFormat.MD 재입력) -> HybridChunker
# 경로에서, Docling이 표를 다시 파싱해 contextualize()로 직렬화할 때 인접한 표 행
# 사이의 줄바꿈이 사라지는 경우가 있다("...환경 ||---|---|| 14 |..."처럼 헤더·구분선·
# 본문행이 한 줄로 눌어붙음). 실측(.scratch/audit_hwp_chunks.py)으로 hwp 표 청크의
# 6.9%(770/11,172)가 이 문제였다.
#
# 우리가 만드는 표 행은 "| " + " | ".join(cells) + " |" 형태라, 빈 셀도 파이프 사이에
# 공백이 낀다("|  |"). 그래서 공백 없이 파이프가 바로 붙는 "||"는 정상 표에서는 절대
# 나올 수 없고, 오직 이 행-경계 유실 버그에서만 나타난다 — 안전하게 구분 가능한 신호다.
_GLUED_PIPE_RE = re.compile(r"\|(?=\|)")


def _repair_glued_table_rows(text: str) -> str:
    """인접한 '|'와 '|' 사이에 줄바꿈을 되돌려 넣어 눌어붙은 표 행을 되살린다."""
    return _GLUED_PIPE_RE.sub("|\n", text)


@dataclass
class Chunk:
    text: str          # 임베딩/BM25에 들어가는 최종 텍스트 (heading 경로 포함)
    headings: list[str]
    page_no: int | None


class ChunkerBank:
    """형식별 HybridChunker 인스턴스를 만들어 캐시한다(토크나이저는 공유)."""

    def __init__(self, cfg: dict):
        chunk_cfg = cfg["chunking"]
        self.default = chunk_cfg["default"]
        self.by_format: dict[str, dict] = chunk_cfg.get("by_format") or {}
        self.min_chars: int = chunk_cfg.get("min_chars", 30)
        self.merge_peers: bool = chunk_cfg.get("merge_peers", True)
        self._tokenizer_id: str = chunk_cfg["tokenizer"]
        self._hf_tokenizer = None
        self._cache: dict[str, object] = {}

    def _tokenizer(self, max_tokens: int):
        from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
        from transformers import AutoTokenizer

        if self._hf_tokenizer is None:
            self._hf_tokenizer = AutoTokenizer.from_pretrained(self._tokenizer_id)
        return HuggingFaceTokenizer(tokenizer=self._hf_tokenizer, max_tokens=max_tokens)

    def params(self, fmt: str) -> dict:
        return {**self.default, **self.by_format.get(fmt, {})}

    def for_format(self, fmt: str):
        if fmt not in self._cache:
            from docling.chunking import HybridChunker

            from .table_serializer import CompactSerializerProvider

            params = self.params(fmt)
            self._cache[fmt] = HybridChunker(
                tokenizer=self._tokenizer(params["max_tokens"]),
                merge_peers=self.merge_peers,
                serializer_provider=CompactSerializerProvider(),
            )
        return self._cache[fmt]

    def chunk(self, doc, fmt: str) -> Iterator[Chunk]:
        chunker = self.for_format(fmt)
        overlap = int(self.params(fmt).get("overlap_tokens", 0))
        raw = list(chunker.chunk(dl_doc=doc))

        prev_tail = ""
        for item in raw:
            body = _repair_glued_table_rows(chunker.contextualize(chunk=item))
            if len(body.strip()) < self.min_chars:
                prev_tail = ""
                continue
            text = (prev_tail + "\n" + body) if prev_tail else body
            yield Chunk(
                text=text,
                headings=list(getattr(item.meta, "headings", None) or []),
                page_no=_page_of(item),
            )
            prev_tail = _tail(body, overlap) if overlap else ""


def _tail(text: str, overlap_tokens: int) -> str:
    """다음 청크 앞에 붙일 꼬리. 토큰 대신 대략 2.5자/토큰으로 근사해 문자 단위로 자른다.

    (정확한 토큰 절단은 heading 컨텍스트까지 다시 세야 해서, 경계 손실을 막는다는
     overlap 본래 목적에는 문자 근사로 충분하다.)"""
    if overlap_tokens <= 0:
        return ""
    n = int(overlap_tokens * 2.5)
    tail = text[-n:]
    cut = tail.find(" ")
    return tail[cut + 1 :] if 0 <= cut < 40 else tail


def _page_of(item) -> int | None:
    for prov in getattr(item.meta, "doc_items", []) or []:
        for p in getattr(prov, "prov", []) or []:
            if getattr(p, "page_no", None):
                return int(p.page_no)
    return None

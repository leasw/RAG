"""Docling HybridChunker 기반 청킹 + 형식별 파라미터.

HybridChunker는 문서 구조(heading / 표 / 리스트)를 먼저 따라 자르고, 토크나이저로
센 토큰 수가 max_tokens를 넘으면 다시 쪼개며, 너무 짧은 이웃 청크는 병합합니다.
형식마다 텍스트 밀도가 달라서(pptx는 조각조각, xlsx는 표 한 덩어리) max_tokens와
overlap을 config의 chunking.by_format에서 다르게 줍니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


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
            body = chunker.contextualize(chunk=item)
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

"""BM25+ sparse retrieval via bm25s.

A single Tokenizer instance is reused for both corpus and query tokenization
(update_vocab defaults to "if_empty", so the query call reuses the corpus
vocab instead of building a separate, inconsistent one).
"""

import bm25s
from bm25s.tokenization import Tokenizer


class BM25PlusIndex:
    def __init__(self, corpus_ids: list[str], corpus_texts: list[str], lang: str = "ko"):
        self.corpus_ids = corpus_ids
        stopwords = "english" if lang == "en" else None
        self.tokenizer = Tokenizer(stopwords=stopwords)
        corpus_tokens = self.tokenizer.tokenize(corpus_texts, show_progress=False)
        self.retriever = bm25s.BM25(method="bm25+")
        self.retriever.index(corpus_tokens, show_progress=False)

    def search_batch(self, query_texts: list[str], k: int) -> list[list[tuple[str, float]]]:
        query_tokens = self.tokenizer.tokenize(query_texts, update_vocab=False, show_progress=False)
        k = min(k, len(self.corpus_ids))
        results, scores = self.retriever.retrieve(query_tokens, k=k, show_progress=False)
        out = []
        for doc_idx_row, score_row in zip(results, scores):
            out.append([(self.corpus_ids[i], float(s)) for i, s in zip(doc_idx_row, score_row)])
        return out

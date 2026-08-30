"""Rerankers: local cross-encoder (default) or OpenRouter's hosted /rerank endpoint.

Both expose the same .rerank(query, candidates, top_k) -> [(doc_id, score), ...] interface
so they're interchangeable in the pipeline.

Local default: BAAI/bge-reranker-v2-m3 (~568M params, multilingual incl. Korean).
OpenRouter /rerank response shape (verified against the live API):
    {"results": [{"index": int, "relevance_score": float, "document": {"text": str}}, ...]}
"""

import time

import requests
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer


class CrossEncoderReranker:
    def __init__(self, model_id: str, device: str = "cuda", batch_size: int = 16, max_length: int = 1024):
        self.device = device if (device == "cuda" and torch.cuda.is_available()) else "cpu"
        self.tokenizer = AutoTokenizer.from_pretrained(model_id)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_id)
        self.model.to(self.device)
        self.model.eval()
        self.batch_size = batch_size
        self.max_length = max_length

    @torch.no_grad()
    def score(self, query: str, docs: list[str]) -> list[float]:
        scores: list[float] = []
        for i in range(0, len(docs), self.batch_size):
            batch_docs = docs[i : i + self.batch_size]
            pairs = [[query, d] for d in batch_docs]
            inputs = self.tokenizer(
                pairs, padding=True, truncation=True, max_length=self.max_length, return_tensors="pt"
            ).to(self.device)
            logits = self.model(**inputs).logits.view(-1).float()
            scores.extend(logits.cpu().tolist())
        return scores

    def rerank(self, query: str, candidates: list[tuple[str, str]], top_k: int) -> list[tuple[str, float]]:
        """candidates: [(doc_id, doc_text), ...]. Returns top_k (doc_id, score), best-first."""
        if not candidates:
            return []
        doc_ids = [c[0] for c in candidates]
        doc_texts = [c[1] for c in candidates]
        scores = self.score(query, doc_texts)
        ranked = sorted(zip(doc_ids, scores), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]


class OpenRouterReranker:
    """Calls OpenRouter's hosted /rerank endpoint instead of running a local model."""

    def __init__(self, cfg: dict, api_key: str, model_id: str):
        self.base_url = cfg["openrouter"]["base_url"].rstrip("/")
        self.api_key = api_key
        self.model_id = model_id
        self.timeout = cfg["openrouter"]["request_timeout_s"]
        self.max_retries = cfg["openrouter"]["max_retries"]
        self.device = "openrouter-api"  # for parity with CrossEncoderReranker logging

    def rerank(self, query: str, candidates: list[tuple[str, str]], top_k: int) -> list[tuple[str, float]]:
        if not candidates:
            return []
        doc_ids = [c[0] for c in candidates]
        doc_texts = [c[1] for c in candidates]

        url = f"{self.base_url}/rerank"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"model": self.model_id, "query": query, "documents": doc_texts}

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                resp.raise_for_status()
                results = resp.json()["results"]
                ranked = sorted(results, key=lambda r: r["relevance_score"], reverse=True)
                return [(doc_ids[r["index"]], float(r["relevance_score"])) for r in ranked[:top_k]]
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(min(2**attempt, 30))
        raise RuntimeError(
            f"OpenRouter rerank request failed for model={self.model_id} "
            f"after {self.max_retries} retries: {last_err}"
        )


_INSTANCES: dict[tuple, object] = {}


def build_reranker(cfg: dict, api_key: str | None = None, reuse: bool = True):
    """Factory: returns CrossEncoderReranker (local) or OpenRouterReranker based on
    cfg['reranker']['backend'] ('local' | 'openrouter').

    같은 (백엔드, 모델, 디바이스) 조합은 인스턴스를 재사용한다. 호출부가
    DocRetriever와 memory_promote 두 곳인데 서로를 모른다 — 한 프로세스에서 둘 다
    쓰면 2.1GB짜리 모델이 두 벌 올라간다. 임베더와 같은 이유로 여기서 막는다.
    """
    rr = cfg["reranker"]
    key = (rr.get("backend", "local"), rr["model_id"], rr.get("device", "cuda"))
    if reuse and key in _INSTANCES:
        return _INSTANCES[key]

    backend = cfg["reranker"].get("backend", "local")
    if backend == "openrouter":
        if not api_key:
            raise RuntimeError("openrouter reranker backend requires an OpenRouter API key")
        instance = OpenRouterReranker(cfg, api_key, cfg["reranker"]["model_id"])
    elif backend != "local":
        raise ValueError(
            f"Unknown reranker backend: {backend!r} (expected 'local' or 'openrouter')"
        )
    else:
        instance = CrossEncoderReranker(
            cfg["reranker"]["model_id"],
            device=cfg["reranker"]["device"],
            batch_size=cfg["reranker"]["batch_size"],
            max_length=cfg["reranker"]["max_length"],
        )

    if reuse:
        _INSTANCES[key] = instance
    return instance

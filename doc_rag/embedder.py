"""OpenRouter embeddings client with an on-disk (SQLite) cache per model.

Endpoint: POST {base_url}/embeddings
  headers: Authorization: Bearer <key>
  body: {"model": "<id>", "input": [...], "encoding_format": "float"}
  resp: {"data": [{"embedding": [...], "index": 0}, ...]}
"""

import hashlib
import sqlite3
import time
from pathlib import Path

import numpy as np
import requests

from .config import CACHE_DIR


def _slug(model_id: str) -> str:
    return model_id.replace("/", "__").replace(":", "_")


def _hash_text(model_id: str, text: str) -> str:
    return hashlib.sha256(f"{model_id}\x00{text}".encode("utf-8")).hexdigest()


class EmbeddingCache:
    def __init__(self, model_id: str):
        cache_dir = CACHE_DIR / "embeddings"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path: Path = cache_dir / f"{_slug(model_id)}.sqlite3"
        # check_same_thread=False: embedding_factory가 이 임베더 인스턴스를 전역
        # 캐시로 공유한다(FactMemory/DocRetriever/GraphRetriever/AgentRuntime이
        # 전부 같은 인스턴스를 씀). 웹 서버(chat_server)처럼 요청마다 스레드가
        # 바뀌는 곳에서는 만들어진 스레드가 아닌 다른 스레드에서도 이 커넥션이
        # 쓰인다. 동시 접근 직렬화는 호출부(chat_server의 _lock)가 책임진다.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS embeddings (hash TEXT PRIMARY KEY, dim INTEGER, vec BLOB)"
        )
        self.conn.commit()

    def get_many(self, hashes: list[str]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for i in range(0, len(hashes), 900):  # stay under SQLite's variable limit
            chunk = hashes[i : i + 900]
            qmarks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT hash, vec FROM embeddings WHERE hash IN ({qmarks})", chunk
            )
            for h, vec in rows:
                out[h] = np.frombuffer(vec, dtype=np.float32)
        return out

    def put_many(self, items: list[tuple[str, np.ndarray]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO embeddings (hash, dim, vec) VALUES (?, ?, ?)",
            [(h, int(v.shape[0]), v.astype(np.float32).tobytes()) for h, v in items],
        )
        self.conn.commit()


class OpenRouterEmbedder:
    def __init__(self, cfg: dict, api_key: str, model_id: str):
        self.base_url = cfg["openrouter"]["base_url"].rstrip("/")
        self.api_key = api_key
        self.model_id = model_id
        self.timeout = cfg["openrouter"]["request_timeout_s"]
        self.max_retries = cfg["openrouter"]["max_retries"]
        self.batch_size = cfg["openrouter"]["batch_size"]
        self.cache = EmbeddingCache(model_id)

    def _post(self, texts: list[str]) -> list[np.ndarray]:
        url = f"{self.base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": self.model_id, "input": texts, "encoding_format": "float"}

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                resp.raise_for_status()
                data = resp.json()["data"]
                data.sort(key=lambda d: d["index"])
                return [np.array(d["embedding"], dtype=np.float32) for d in data]
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(min(2**attempt, 30))
        raise RuntimeError(
            f"OpenRouter embedding request failed for model={self.model_id} "
            f"after {self.max_retries} retries: {last_err}"
        )

    def embed(self, texts: list[str], show_progress: bool = False) -> np.ndarray:
        """Returns an (N, D) float32 array aligned with `texts`. Cached on disk by (model, text)."""
        hashes = [_hash_text(self.model_id, t) for t in texts]
        cached = self.cache.get_many(hashes)

        missing_idx = [i for i, h in enumerate(hashes) if h not in cached]
        batches = [missing_idx[i : i + self.batch_size] for i in range(0, len(missing_idx), self.batch_size)]
        if show_progress and batches:
            from tqdm import tqdm

            batches = tqdm(batches, desc=f"embed[{self.model_id}]")

        new_items: list[tuple[str, np.ndarray]] = []
        for batch_idx in batches:
            batch_texts = [texts[i] for i in batch_idx]
            vecs = self._post(batch_texts)
            for i, v in zip(batch_idx, vecs):
                cached[hashes[i]] = v
                new_items.append((hashes[i], v))
            if len(new_items) >= 500:
                self.cache.put_many(new_items)
                new_items = []
        if new_items:
            self.cache.put_many(new_items)

        return np.stack([cached[h] for h in hashes])

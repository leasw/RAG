"""Local (HF/sentence-transformers) embedding backend, for models not served by OpenRouter.

Uses sentence-transformers so each model's bundled pooling config (CLS vs mean, etc.)
and normalization are applied automatically instead of being hand-picked per model.
Same on-disk cache and .embed(texts) interface as OpenRouterEmbedder, so it's a drop-in
replacement in the pipeline.
"""

import torch
from sentence_transformers import SentenceTransformer

from .embedder import EmbeddingCache, _hash_text


class LocalEmbedder:
    def __init__(self, model_id: str, device: str = "cuda", batch_size: int = 32):
        self.model_id = model_id
        self.device = device if (device == "cuda" and torch.cuda.is_available()) else "cpu"
        self.model = SentenceTransformer(model_id, device=self.device, trust_remote_code=True)
        self.batch_size = batch_size
        self.cache = EmbeddingCache(model_id)

    def embed(self, texts: list[str], show_progress: bool = False):
        hashes = [_hash_text(self.model_id, t) for t in texts]
        cached = self.cache.get_many(hashes)

        missing_idx = [i for i, h in enumerate(hashes) if h not in cached]
        if missing_idx:
            missing_texts = [texts[i] for i in missing_idx]
            vecs = self.model.encode(
                missing_texts,
                batch_size=self.batch_size,
                show_progress_bar=show_progress,
                normalize_embeddings=True,
                convert_to_numpy=True,
            )
            new_items = [(hashes[i], vecs[j]) for j, i in enumerate(missing_idx)]
            for h, v in new_items:
                cached[h] = v
            self.cache.put_many(new_items)

        import numpy as np

        return np.stack([cached[h] for h in hashes])

    def unload(self):
        del self.model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

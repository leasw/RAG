"""임베딩 백엔드 선택: local(sentence-transformers, GPU) | openrouter(API).

두 백엔드 모두 `.embed(texts, show_progress=False) -> (N, D) float32` 인터페이스와
`cache/embeddings/<model>.sqlite3` 디스크 캐시를 공유해서 서로 드롭인 교체됩니다.

**같은 (백엔드, 모델, 디바이스) 조합은 인스턴스를 재사용합니다.**

호출부가 다섯 곳(FactMemory, DocRetriever, GraphRetriever, AgentRuntime, build_index)
인데 서로를 모릅니다. 캐시가 없을 때 한 프로세스에서 실측하면 arctic-ko가 3벌
올라가 VRAM 8.47GB를 썼고, 그중 4.24GB가 중복이었습니다. 로딩 시간도 7.2초씩
중복으로 냈습니다.

호출부를 고치는 대신 여기서 막습니다. 인스턴스는 상태가 없고(모델 가중치 + 디스크
캐시 핸들뿐) 스레드 안에서 공유해도 문제가 없습니다.
"""

from __future__ import annotations

_INSTANCES: dict[tuple, object] = {}


def _key(cfg: dict) -> tuple:
    emb = cfg["embedding"]
    backend = emb.get("backend", "local")
    return (backend, emb["id"], emb.get("device", "cuda"),
            int(emb.get("batch_size", 32)), emb.get("max_seq_length"))


def build_embedder(cfg: dict, api_key: str | None = None, reuse: bool = True):
    """임베더를 만든다. reuse=True(기본)면 같은 설정의 기존 인스턴스를 돌려준다."""
    key = _key(cfg)
    if reuse and key in _INSTANCES:
        return _INSTANCES[key]

    emb = cfg["embedding"]
    backend = emb.get("backend", "local")

    if backend == "local":
        from .local_embedder import LocalEmbedder

        instance = LocalEmbedder(
            emb["id"],
            device=emb.get("device", "cuda"),
            batch_size=int(emb.get("batch_size", 32)),
            max_seq_length=emb.get("max_seq_length"),
        )
    elif backend == "openrouter":
        from .config import get_api_key
        from .embedder import OpenRouterEmbedder

        instance = OpenRouterEmbedder(cfg, api_key or get_api_key(cfg), emb["id"])
    else:
        raise ValueError(
            f"Unknown embedding backend: {backend!r} (expected 'local' or 'openrouter')"
        )

    if reuse:
        _INSTANCES[key] = instance
    return instance


def release_embedders() -> None:
    """캐시된 인스턴스를 놓아준다. 모델을 바꿔 실험할 때만 쓴다."""
    for instance in _INSTANCES.values():
        unload = getattr(instance, "unload", None)
        if callable(unload):
            unload()
    _INSTANCES.clear()

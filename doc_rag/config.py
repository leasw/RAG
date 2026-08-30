from __future__ import annotations

import os
from pathlib import Path

import yaml

# Windows에서 개발자 모드/관리자 권한 없이 HF 캐시를 쓰면 symlink 생성이 WinError 1314로
# 막힌다. 심볼릭 링크 대신 파일을 복사하도록 강제한다. (모델 다운로드보다 먼저 설정)
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "doc_rag.yaml"
INDEX_DIR = ROOT / "index" / "doc_rag"
CACHE_DIR = ROOT / "cache"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: str | Path = CONFIG_PATH) -> dict:
    _load_dotenv(ROOT / ".env")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_api_key(cfg: dict) -> str:
    env_var = cfg["openrouter"]["api_key_env"]
    key = os.environ.get(env_var, "").strip()
    if not key:
        raise RuntimeError(f"{env_var} is not set. Fill it in .env.")
    return key


def corpus_roots(cfg: dict) -> list[Path]:
    return [(ROOT / r).resolve() for r in cfg["corpus"]["roots"]]

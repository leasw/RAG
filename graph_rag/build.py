"""LTM 코퍼스에서 그래프를 만든다.

    python -m graph_rag.build            # 전체 재구축
    python -m graph_rag.build --stats    # 현재 그래프 통계

doc_rag 인덱스(index/doc_rag/chunks.sqlite3)의 청크를 훑으면서 schema.py에 정의된
개체의 표기를 찾는다. 닫힌 어휘라 새 개체를 발견하지는 않는다.

만들어지는 엣지는 셋이다.

    구조     schema.STRUCTURAL_EDGES 그대로 (소속, 참여, 제품 계층)
    MENTIONS Document -> Entity, weight = 그 문서에서의 언급 수
    CO_OCCURS Entity <-> Entity, weight = 같은 청크에 함께 나온 횟수

CO_OCCURS는 방향이 없다. 청크 하나에 개체가 여럿 나오면 모든 쌍을 센다. 한 청크에
같은 개체가 여러 번 나와도 쌍 계산에는 1회로만 본다 — 반복 표기가 가중치를 부풀리는 걸
막기 위해서다.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

from .schema import ENTITIES, STRUCTURAL_EDGES

ROOT = Path(__file__).resolve().parents[1]
CHUNKS_DB = ROOT / "index" / "doc_rag" / "chunks.sqlite3"
VECS_NPY = ROOT / "index" / "doc_rag" / "embeddings.npy"
VECS_IDS = ROOT / "index" / "doc_rag" / "chunk_ids.json"
GRAPH_DB = ROOT / "index" / "graph_rag" / "graph.sqlite3"
ENTITY_VECS_NPY = ROOT / "index" / "graph_rag" / "entity_vecs.npy"
ENTITY_VECS_KEYS = ROOT / "index" / "graph_rag" / "entity_keys.json"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    key    TEXT PRIMARY KEY,
    type   TEXT NOT NULL,
    label  TEXT NOT NULL,
    attrs  TEXT,
    mentions INTEGER DEFAULT 0,   -- 코퍼스 전체 언급 수
    docs     INTEGER DEFAULT 0    -- 등장 문서 수
);
CREATE TABLE IF NOT EXISTS edges (
    src    TEXT NOT NULL,
    type   TEXT NOT NULL,
    dst    TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    attrs  TEXT,
    PRIMARY KEY (src, type, dst)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, type);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, type);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
"""


def _matcher():
    """표기 -> 노드 key. 긴 표기를 먼저 보도록 정렬한 정규식 하나로 합친다.

    "가천대학교 산학협력단"과 "가천대"가 둘 다 사전에 있을 때, 짧은 쪽이 먼저 매칭되면
    긴 표기가 잘린다. 정규식 대안(alternation)은 앞에 온 것이 우선이라 길이 역순으로 넣는다.
    """
    surface_to_key: dict[str, str] = {}
    for ent in ENTITIES:
        for s in ent.surfaces:
            surface_to_key.setdefault(s, ent.key)
    ordered = sorted(surface_to_key, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(s) for s in ordered))
    return pattern, surface_to_key


class Disambiguator:
    """동음이의어 판정기.

    "스마트 글래스"는 코퍼스 2,859청크에 나오지만 그중 238청크는 경쟁사 제품
    (삼성 Gear VR 릴루미노, Oxsight, OrCam 등)을 가리킨다. 문자열만 보면 이것들이
    전부 우리 제품 노드에 붙어 그래프가 오염된다.

    절대 문턱 대신 판별식을 쓴다 — 청크 벡터를 개체의 context(맞는 문맥)와
    negative(틀린 문맥) 양쪽과 비교해 더 가까운 쪽으로 정한다. 문턱값을 보정할
    필요가 없고, 코사인의 절대 척도가 모델마다 다른 문제도 피한다.

    청크 벡터는 doc_rag가 이미 계산해 둔 embeddings.npy를 그대로 재사용한다.
    """

    def __init__(self):
        self.ok = False
        self.entity_vecs: dict[str, tuple] = {}
        self.chunk_vecs = None
        self.chunk_index: dict[str, int] = {}

        targets = [e for e in ENTITIES if e.ambiguous]
        if not targets or not VECS_NPY.exists():
            return
        try:
            import numpy as np

            from doc_rag.config import load_config
            from doc_rag.embedding_factory import build_embedder

            self.chunk_vecs = np.load(VECS_NPY)
            ids = json.loads(VECS_IDS.read_text(encoding="utf-8"))
            self.chunk_index = {cid: i for i, cid in enumerate(ids)}

            embedder = build_embedder(load_config())
            texts, keys = [], []
            for e in targets:
                texts += [e.context, e.negative]
                keys.append(e.key)
            vecs = np.asarray(embedder.embed(texts), dtype="float32")
            vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
            for i, key in enumerate(keys):
                self.entity_vecs[key] = (vecs[2 * i], vecs[2 * i + 1])
            self.ok = True
        except Exception as exc:  # noqa: BLE001
            print(f"[disambiguate] 비활성 — {type(exc).__name__}: {exc}")

    def accepts(self, key: str, chunk_id: str) -> bool:
        """이 청크에서의 매칭을 개체 key로 인정할지."""
        if not self.ok or key not in self.entity_vecs:
            return True                      # 판정 대상이 아니면 그대로 통과
        idx = self.chunk_index.get(chunk_id)
        if idx is None:
            return True
        import numpy as np

        v = self.chunk_vecs[idx]
        v = v / (np.linalg.norm(v) + 1e-12)
        pos, neg = self.entity_vecs[key]
        return float(v @ pos) >= float(v @ neg)


def build(verbose: bool = True) -> dict:
    if not CHUNKS_DB.exists():
        raise FileNotFoundError(f"LTM 인덱스가 없습니다: {CHUNKS_DB}")

    pattern, surface_to_key = _matcher()
    ambiguous_surfaces = {s for e in ENTITIES for s in e.ambiguous}
    disambiguator = Disambiguator()
    rejected: Counter[str] = Counter()
    src = sqlite3.connect(CHUNKS_DB)
    rows = src.execute(
        "SELECT ch.doc_id, ch.chunk_id, ch.text, d.file_name, d.rel_path, d.stage "
        "FROM chunks ch JOIN documents d ON d.doc_id = ch.doc_id"
    ).fetchall()

    node_mentions: Counter[str] = Counter()
    node_docs: dict[str, set[str]] = defaultdict(set)
    doc_mentions: Counter[tuple[str, str]] = Counter()   # (doc_id, node_key)
    cooccur: Counter[tuple[str, str]] = Counter()
    doc_meta: dict[str, tuple[str, str, str]] = {}
    # 개체별로 실제 언급된 청크 id. resolve()의 의미 유사도 매칭용 centroid를
    # 만들 재료다 — 문자열 매칭과 별개로, "이 개체가 실제로 나온 문맥들의 평균"을
    # 벡터 하나로 남겨서, 표기가 안 겹쳐도 의미로 찾을 수 있게 한다.
    entity_chunks: dict[str, list[str]] = defaultdict(list)
    ENTITY_CHUNK_CAP = 200  # 한 개체당 centroid에 쓸 청크 상한 (계산량/편향 억제)

    for doc_id, chunk_id, text, file_name, rel_path, stage in rows:
        doc_meta[doc_id] = (file_name, rel_path, stage or "")
        found: list[str] = []
        for m in pattern.finditer(text or ""):
            surface = m.group(0)
            key = surface_to_key[surface]
            if surface in ambiguous_surfaces and not disambiguator.accepts(key, chunk_id):
                rejected[key] += 1        # 경쟁사/타사 문맥으로 판정 -> 이 개체가 아님
                continue
            found.append(key)
        if not found:
            continue
        for key in found:
            node_mentions[key] += 1
            node_docs[key].add(doc_id)
            doc_mentions[(doc_id, key)] += 1
        # 같은 청크 안 중복은 1회로 접고 쌍을 센다.
        for a, b in combinations(sorted(set(found)), 2):
            cooccur[(a, b)] += 1
        for key in set(found):
            if len(entity_chunks[key]) < ENTITY_CHUNK_CAP:
                entity_chunks[key].append(chunk_id)

    GRAPH_DB.parent.mkdir(parents=True, exist_ok=True)
    if GRAPH_DB.exists():
        GRAPH_DB.unlink()
    out = sqlite3.connect(GRAPH_DB)
    out.executescript(SCHEMA_SQL)

    # 개체 노드
    for ent in ENTITIES:
        out.execute(
            "INSERT INTO nodes (key, type, label, attrs, mentions, docs) VALUES (?,?,?,?,?,?)",
            (ent.key, ent.type, ent.label, json.dumps(ent.attrs, ensure_ascii=False),
             node_mentions.get(ent.key, 0), len(node_docs.get(ent.key, ()))),
        )
    # 문서 노드 — 개체가 하나라도 언급된 문서만 넣는다. 안 그러면 918개 중 대부분이
    # 아무 엣지도 없는 고립 노드가 된다.
    doc_ids = {d for d, _ in doc_mentions}
    for doc_id in doc_ids:
        file_name, rel_path, stage = doc_meta[doc_id]
        out.execute(
            "INSERT INTO nodes (key, type, label, attrs, mentions, docs) VALUES (?,?,?,?,?,?)",
            (f"doc:{doc_id}", "Document", file_name,
             json.dumps({"rel_path": rel_path, "stage": stage}, ensure_ascii=False), 0, 1),
        )

    known = {e.key for e in ENTITIES}
    for s, t, d, attrs in STRUCTURAL_EDGES:
        if s in known and d in known:
            out.execute(
                "INSERT OR REPLACE INTO edges (src, type, dst, weight, attrs) VALUES (?,?,?,?,?)",
                (s, t, d, 1.0, json.dumps(attrs, ensure_ascii=False)),
            )
    for (doc_id, key), w in doc_mentions.items():
        out.execute(
            "INSERT OR REPLACE INTO edges (src, type, dst, weight, attrs) VALUES (?,?,?,?,?)",
            (f"doc:{doc_id}", "MENTIONS", key, float(w), "{}"),
        )
    for (a, b), w in cooccur.items():
        out.execute(
            "INSERT OR REPLACE INTO edges (src, type, dst, weight, attrs) VALUES (?,?,?,?,?)",
            (a, "CO_OCCURS", b, float(w), "{}"),
        )

    out.commit()
    stats = summarize(out)
    stats["disambiguation"] = {
        "enabled": disambiguator.ok,
        "rejected_mentions": {k: v for k, v in rejected.most_common()},
    }
    out.close()
    src.close()

    stats["semantic_index"] = _build_entity_centroids(entity_chunks)

    if verbose:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    return stats


def _build_entity_centroids(entity_chunks: dict[str, list[str]]) -> dict:
    """개체별 centroid 벡터를 만들어 저장한다.

    resolve()는 표기가 사전 등록된 문자열과 정확히 겹칠 때만 개체를 찾는다.
    "경북대산학협력단"처럼 등록된 정식 명칭("경북대학교 산학협력단")과 한 글자만
    달라도 그 방식으로는 못 찾는다. centroid는 그 문자열 매칭을 대체하지 않고
    병행할 폴백이다 — 개체가 실제로 언급된 청크들의 임베딩을 평균 내서 벡터
    하나로 남겨두면, 표기가 안 겹쳐도 "의미가 그 개체 근처에 있는지"로 다시
    시도할 수 있다.

    청크 임베딩은 doc_rag가 이미 만들어 둔 것(embeddings.npy)을 그대로 쓴다 —
    Disambiguator와 같은 재료를 다른 목적으로 재사용하는 것뿐이라 별도 임베딩
    계산이 없다.
    """
    if not VECS_NPY.exists() or not VECS_IDS.exists():
        return {"enabled": False, "reason": "doc_rag 임베딩 인덱스 없음"}
    try:
        import numpy as np
    except ImportError:
        return {"enabled": False, "reason": "numpy 없음"}

    chunk_vecs = np.load(VECS_NPY)
    ids = json.loads(VECS_IDS.read_text(encoding="utf-8"))
    chunk_index = {cid: i for i, cid in enumerate(ids)}

    keys, vecs = [], []
    for key, chunk_ids in entity_chunks.items():
        idxs = [chunk_index[c] for c in chunk_ids if c in chunk_index]
        if not idxs:
            continue
        sub = chunk_vecs[idxs].astype("float32")
        sub /= np.linalg.norm(sub, axis=1, keepdims=True) + 1e-12
        centroid = sub.mean(axis=0)
        centroid /= np.linalg.norm(centroid) + 1e-12
        keys.append(key)
        vecs.append(centroid)

    if not keys:
        return {"enabled": False, "reason": "centroid를 만들 청크가 없음"}

    matrix = np.stack(vecs).astype("float32")
    ENTITY_VECS_NPY.parent.mkdir(parents=True, exist_ok=True)
    np.save(ENTITY_VECS_NPY, matrix)
    ENTITY_VECS_KEYS.write_text(json.dumps(keys, ensure_ascii=False), encoding="utf-8")
    return {"enabled": True, "entities": len(keys), "dim": int(matrix.shape[1])}


def summarize(conn: sqlite3.Connection) -> dict:
    q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
    return {
        "nodes": {t: n for t, n in q("SELECT type, COUNT(*) FROM nodes GROUP BY type ORDER BY 2 DESC")},
        "edges": {t: n for t, n in q("SELECT type, COUNT(*) FROM edges GROUP BY type ORDER BY 2 DESC")},
        "top_mentioned": [
            {"label": lbl, "type": t, "mentions": m, "docs": d}
            for lbl, t, m, d in q(
                "SELECT label, type, mentions, docs FROM nodes "
                "WHERE type != 'Document' ORDER BY mentions DESC LIMIT 12"
            )
        ],
        "isolated": [
            lbl for (lbl,) in q(
                "SELECT label FROM nodes WHERE type != 'Document' AND mentions = 0"
            )
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()
    if args.stats:
        if not GRAPH_DB.exists():
            print("그래프가 없습니다. python -m graph_rag.build 를 먼저 실행하세요.")
            return 1
        conn = sqlite3.connect(GRAPH_DB)
        print(json.dumps(summarize(conn), ensure_ascii=False, indent=2))
        conn.close()
        return 0
    build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

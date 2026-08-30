"""그래프 탐색 + 벡터 순위 결정.

회의정리 슬라이드 5의 하이브리드 방식이다.

    그래프 탐색  구조적 질의로 후보군을 좁힌다 ("조진수가 참여한 과제의 정량목표는?")
    벡터 유사도  좁혀진 문서 후보를 질의와의 임베딩 유사도로 재정렬한다

그래프만 쓰면 "연결돼 있다"까지만 알고 근거 문장을 못 준다. 벡터만 쓰면 "조진수의
소속" 같은 구조적 사실을 못 찾는다. 그래서 둘을 순서대로 쓴다.

임베딩은 doc_rag와 같은 모델(arctic-ko)을 쓴다. 이미 올라가 있으면 재사용한다.
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .build import GRAPH_DB
from .schema import ENTITIES

# resolve()의 문자열 완전일치가 실패했을 때만 도는 퍼지 매칭 폴백 문턱.
#
# "경북대산학협력단"(질문에 쓴 축약형) vs "경북대학교 산학협력단"(등록된 정식
# 명칭)처럼, 정식 명칭에서 한두 글자만 빠진 경우를 잡으려는 용도다. 의미 유사도
# (질문 임베딩 vs 개체 centroid)도 시도해봤는데, 이 코퍼스가 단일 프로젝트
# 문서 뭉치라 모든 개체의 centroid가 서로 비슷하게 뭉쳐 있어서 변별력이 없었다
# (정답과 오답의 코사인 차이가 0.001~0.1 수준). 반면 문자 단위 퍼지 매칭은
# 실측에서 뚜렷하게 갈렸다:
#     개체가 실제로 언급된 질문   상위 점수 0.800 ~ 1.000
#     개체 언급이 아예 없는 질문  상위 점수 0.333 ~ 0.400
# 그 사이를 문턱으로 두되, 0.6은 오답 후보(비슷한 이름의 다른 기관들)까지 같이
# 끌려온다 — "경북대산학협력단" 질의에서 정답(경북대, 0.800) 말고도 가천대·동국대
# 산협이 나란히 0.600으로 걸려 셋 다 채택돼버렸다. 그 동률 구간(0.6)을 피해
# 0.65로 둔다.
FUZZY_MATCH_THRESHOLD = 0.65
FUZZY_LEN_DELTA = 2   # 표기 길이 대비 앞뒤로 이만큼 짧거나 긴 부분 문자열까지 비교
FUZZY_MIN_LEN = 3     # 이보다 짧은 표기는 퍼지 매칭 대상에서 제외 (오탐 방지)

ROOT = Path(__file__).resolve().parents[1]
CHUNKS_DB = ROOT / "index" / "doc_rag" / "chunks.sqlite3"

# 구조 엣지는 사람이 정의한 사실이라 파생 엣지보다 신뢰도가 높다. 이웃 정렬에서
# 위로 올리기 위한 가중치.
STRUCTURAL = {"BELONGS_TO", "PARTICIPATES_IN", "PART_OF", "HAS_VERSION", "HAS_PART"}


@dataclass
class Node:
    key: str
    type: str
    label: str
    attrs: dict
    mentions: int = 0
    docs: int = 0


class GraphRetriever:
    def __init__(self, db_path: Path = GRAPH_DB):
        if not db_path.exists():
            raise FileNotFoundError(
                f"그래프 인덱스가 없습니다: {db_path}. `python -m graph_rag.build` 실행 필요."
            )
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._surface = self._surface_map()
        self._embedder = None
        self._embed_tried = False

    # ------------------------------------------------------------ 엔티티 해석

    def _surface_map(self) -> list[tuple[str, str]]:
        """(표기, key) 목록. 긴 표기부터 봐야 부분 일치가 긴 표기를 가리지 않는다."""
        pairs: list[tuple[str, str]] = []
        for ent in ENTITIES:
            for s in ent.surfaces:
                pairs.append((s, ent.key))
        return sorted(pairs, key=lambda p: len(p[0]), reverse=True)

    def resolve(self, text: str) -> list[Node]:
        """질의문에서 정의된 개체를 찾는다. 닫힌 어휘라 없는 개체는 안 나온다.

        먼저 문자열 완전일치를 시도하고(빠르고 오탐이 없다), 그걸로 하나도 못
        찾았을 때만 퍼지 매칭으로 한 번 더 시도한다 — 두 방식을 항상 같이
        돌리지 않는 이유는, 완전일치가 이미 찾은 걸 퍼지 매칭이 다시 건드릴
        필요가 없고(중복 계산), 완전일치 결과가 있다는 건 표기가 정확해서
        퍼지 매칭이 낄 자리가 없기 때문이다.
        """
        hits: list[str] = []
        remaining = text or ""
        for surface, key in self._surface:
            if surface and surface in remaining and key not in hits:
                hits.append(key)
                # 매칭된 표기를 지워서 "가천대학교 산학협력단"이 "가천대"로 또 잡히지 않게 한다.
                remaining = remaining.replace(surface, " ")
        if not hits:
            hits = self._resolve_fuzzy(text or "")
        return [n for n in (self.node(k) for k in hits) if n]

    def _resolve_fuzzy(self, text: str) -> list[str]:
        """완전일치가 실패했을 때만 쓰는 문자 단위 퍼지 매칭 폴백.

        각 개체의 표기(label + aliases)마다, 질의문 안에서 그 표기와 길이가
        비슷한 모든 부분 문자열을 훑어 가장 비슷한 값을 점수로 삼는다. 개체별
        최고 점수가 FUZZY_MATCH_THRESHOLD를 넘기면 후보로 채택한다. 여러 개체가
        문턱을 넘으면 전부 반환한다(완전일치와 동일하게 다중 개체를 허용).
        """
        best_by_key: dict[str, float] = {}
        for surface, key in self._surface:
            if len(surface) < FUZZY_MIN_LEN:
                continue
            score = self._fuzzy_surface_score(text, surface)
            if score > best_by_key.get(key, 0.0):
                best_by_key[key] = score
        return [key for key, score in best_by_key.items() if score >= FUZZY_MATCH_THRESHOLD]

    @staticmethod
    def _fuzzy_surface_score(text: str, surface: str) -> float:
        """text 안에서 surface와 가장 비슷한 부분 문자열의 유사도(0~1)."""
        target_len = len(surface)
        best = 0.0
        for wlen in range(max(FUZZY_MIN_LEN, target_len - FUZZY_LEN_DELTA),
                          target_len + FUZZY_LEN_DELTA + 1):
            if wlen > len(text):
                continue
            for start in range(0, len(text) - wlen + 1):
                window = text[start:start + wlen]
                ratio = difflib.SequenceMatcher(None, window, surface, autojunk=False).ratio()
                if ratio > best:
                    best = ratio
        return best

    def node(self, key: str) -> Node | None:
        row = self.conn.execute("SELECT * FROM nodes WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return Node(row["key"], row["type"], row["label"],
                    json.loads(row["attrs"] or "{}"), row["mentions"], row["docs"])

    # ------------------------------------------------------------ 그래프 탐색

    def neighbors(self, key: str, edge_types: list[str] | None = None,
                  node_types: list[str] | None = None, limit: int = 20) -> list[dict]:
        """이웃 개체. 구조 엣지를 먼저, 그다음 가중치 순."""
        sql = (
            "SELECT e.type AS etype, e.weight, e.attrs, "
            "       n.key, n.type AS ntype, n.label, n.mentions "
            "FROM edges e JOIN nodes n ON n.key = CASE WHEN e.src = ? THEN e.dst ELSE e.src END "
            "WHERE (e.src = ? OR e.dst = ?) AND n.type != 'Document'"
        )
        params: list[Any] = [key, key, key]
        if edge_types:
            sql += f" AND e.type IN ({','.join('?' * len(edge_types))})"
            params += edge_types
        if node_types:
            sql += f" AND n.type IN ({','.join('?' * len(node_types))})"
            params += node_types

        rows = self.conn.execute(sql, params).fetchall()
        out = [
            {
                "key": r["key"], "type": r["ntype"], "label": r["label"],
                "relation": r["etype"], "weight": r["weight"],
                "attrs": json.loads(r["attrs"] or "{}"),
                "structural": r["etype"] in STRUCTURAL,
            }
            for r in rows
        ]
        out.sort(key=lambda x: (not x["structural"], -x["weight"]))
        return out[:limit]

    def path(self, src: str, dst: str, max_hops: int = 3) -> list[dict] | None:
        """두 개체를 잇는 최단 경로 (BFS). 문서 노드는 건너뛴다."""
        if src == dst:
            return []
        seen = {src}
        queue: list[tuple[str, list[dict]]] = [(src, [])]
        for _ in range(max_hops):
            nxt: list[tuple[str, list[dict]]] = []
            for cur, trail in queue:
                for nb in self.neighbors(cur, limit=50):
                    if nb["key"] in seen:
                        continue
                    step = trail + [{
                        "from": cur, "relation": nb["relation"],
                        "to": nb["key"], "to_label": nb["label"], "weight": nb["weight"],
                    }]
                    if nb["key"] == dst:
                        return step
                    seen.add(nb["key"])
                    nxt.append((nb["key"], step))
            queue = nxt
            if not queue:
                break
        return None

    def documents_for(self, keys: list[str], limit: int = 30) -> list[dict]:
        """해당 개체들을 언급한 문서. 여러 개체를 모두 언급한 문서를 위로 올린다."""
        if not keys:
            return []
        marks = ",".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT e.src AS dockey, n.label, n.attrs, "
            f"       COUNT(DISTINCT e.dst) AS matched, SUM(e.weight) AS w "
            f"FROM edges e JOIN nodes n ON n.key = e.src "
            f"WHERE e.type = 'MENTIONS' AND e.dst IN ({marks}) "
            f"GROUP BY e.src ORDER BY matched DESC, w DESC LIMIT ?",
            [*keys, limit],
        ).fetchall()
        return [
            {"doc_id": r["dockey"].removeprefix("doc:"), "file_name": r["label"],
             "matched_entities": r["matched"], "mention_weight": r["w"],
             **json.loads(r["attrs"] or "{}")}
            for r in rows
        ]

    # ------------------------------------------------------------ 벡터 재정렬

    def _embed(self):
        if self._embed_tried:
            return self._embedder
        self._embed_tried = True
        try:
            from doc_rag.config import load_config
            from doc_rag.embedding_factory import build_embedder

            self._embedder = build_embedder(load_config())
        except Exception:  # noqa: BLE001 - 임베딩이 없어도 그래프 결과는 돌려준다
            self._embedder = None
        return self._embedder

    def rank_chunks(self, query: str, doc_ids: list[str], top_k: int = 5) -> list[dict]:
        """그래프가 좁혀준 문서들의 청크를 질의와의 임베딩 유사도로 재정렬한다."""
        if not doc_ids:
            return []
        src = sqlite3.connect(CHUNKS_DB)
        marks = ",".join("?" * len(doc_ids))
        rows = src.execute(
            f"SELECT ch.chunk_id, ch.text, ch.page_no, ch.headings, "
            f"       d.file_name, d.rel_path, d.stage "
            f"FROM chunks ch JOIN documents d ON d.doc_id = ch.doc_id "
            f"WHERE ch.doc_id IN ({marks})",
            doc_ids,
        ).fetchall()
        src.close()
        if not rows:
            return []

        embedder = self._embed()
        if embedder is None:
            picked = rows[:top_k]
            scores = [0.0] * len(picked)
        else:
            import numpy as np

            from doc_rag.config import load_config

            prefix = load_config()["embedding"].get("query_prefix", "")
            vecs = np.asarray(
                embedder.embed([prefix + query] + [r[1] for r in rows]), dtype="float32"
            )
            vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
            sims = vecs[1:] @ vecs[0]
            order = np.argsort(-sims)[:top_k]
            picked = [rows[i] for i in order]
            scores = [float(sims[i]) for i in order]

        return [
            {
                "chunk_id": r[0], "score": round(s, 4), "text": r[1],
                "source": {"file_name": r[4], "rel_path": r[5], "stage": r[6],
                           "page_no": r[2], "headings": json.loads(r[3] or "[]")},
            }
            for r, s in zip(picked, scores)
        ]

    # ------------------------------------------------------------ 상위 API

    def search(self, query: str, entities: list[str] | None = None,
               relation: str | None = None, hops: int = 1,
               top_k: int = 5) -> dict:
        """그래프로 좁히고 벡터로 순위를 정한다."""
        seeds = self.resolve(" ".join(entities)) if entities else self.resolve(query)
        if not seeds:
            return {
                "query": query, "resolved_entities": [], "facts": [],
                "result_count": 0, "results": [],
                "note": "질의에서 그래프에 정의된 개체를 찾지 못했습니다. "
                        "정의된 개체만 그래프에 있습니다(기관/인물/과제/과업/문서/제품/정량목표).",
            }

        edge_filter = [relation] if relation else None
        facts: list[dict] = []
        frontier = {s.key for s in seeds}
        visited = set(frontier)
        for _ in range(max(1, min(hops, 2))):
            nxt: set[str] = set()
            for key in frontier:
                for nb in self.neighbors(key, edge_types=edge_filter, limit=15):
                    src_node = self.node(key)
                    facts.append({
                        "from": src_node.label if src_node else key,
                        "relation": nb["relation"],
                        "to": nb["label"],
                        "to_type": nb["type"],
                        "weight": nb["weight"],
                        "structural": nb["structural"],
                        **({"attrs": nb["attrs"]} if nb["attrs"] else {}),
                    })
                    if nb["key"] not in visited:
                        nxt.add(nb["key"])
                        visited.add(nb["key"])
            frontier = nxt
            if not frontier:
                break

        docs = self.documents_for([s.key for s in seeds], limit=30)
        chunks = self.rank_chunks(query, [d["doc_id"] for d in docs], top_k=top_k)

        return {
            "query": query,
            "resolved_entities": [
                {"key": s.key, "type": s.type, "label": s.label,
                 "mentions": s.mentions, "docs": s.docs, "attrs": s.attrs}
                for s in seeds
            ],
            "facts": facts[:30],
            "result_count": len(chunks),
            "results": chunks,
        }

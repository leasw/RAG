"""문장 단위 메모리 저장소.

기존에는 대화 한 덩어리가 파일 하나였다. 이제는 **사실 문장 하나가 레코드 하나**다.
mem0가 사실 단위로 저장하고, Hermes가 "지속 가치 있는 사실"만 큐레이션해서 상시
주입하는 구조를 따른 것이다.

    STM  최근 STM_WINDOW_DAYS일 안에 발생한 대화에서 뽑은 사실 문장.
    MTM  STM 중 조회수 조건을 넘겨 승격된 것. 텍스트는 그대로고 계층 표시만 바뀐다.
    LTM  MTM 중 memory_promote.promote_to_ltm()의 4개 기준(출처·진실성·공유적합성·
         유용성)을 전부 통과한 것. doc_rag 문서 코퍼스와는 별개다 — 저 문서 코퍼스는
         "원래 문서였던 것"이고, 이 tier="ltm"은 "채팅에서 시작해 검증을 거쳐 영구
         지식으로 승격된 것"이다. 둘 다 개념상 장기기억이지만 원천이 다르다.

**최신성은 STM에 들어올 자격이다.** 창을 벗어난 사실은 나중에 밀려나는 것이 아니라
애초에 저장되지 않는다. STM이 "지금 굴러가는 맥락"이라면 오래된 사실은 그 정의에
해당하지 않기 때문이다. 과거 로그를 배치로 넣어도 창 밖이면 아무것도 쌓이지 않는다.

**중복을 허용한다.** 같은 사실이 다른 대화에서 다시 나오면 별도 레코드가 된다.
지우거나 합치지 않는다. 무엇이 같은 사실인지 판정할 검증 가능한 기준이 없어서다 —
실측에서 병합해야 할 쌍(코사인 0.789)보다 병합하면 안 되는 쌍(0.799)이 더 높게
나왔다. 잘못 합쳐 사실을 잃는 것보다 중복을 두는 편이 안전하다.

중복이 여러 번 나온다는 것 자체가 신호이기도 하다 — 여러 대화에서 반복 언급된
사실은 그만큼 자주 쓰인다는 뜻이고, 조회수도 각각 쌓인다.

검색 결과에서도 접지 않는다. 같은 문장이 상위 슬롯을 여럿 차지할 수 있다는 뜻인데,
그걸 감수한다. 접기 시작하면 "어디까지 같은 문장인가"를 정해야 하고, 그 판정이
결국 의미 유사도로 넘어간다 — 실측에서 병합해야 할 쌍(코사인 0.789)보다 병합하면
안 되는 쌍(0.799)이 더 높게 나온 그 문제로 돌아간다. 저장부터 검색까지 일관되게
중복을 허용하는 편이 규칙이 하나뿐이라 예측 가능하다.

원문 대화는 여기 없다. data/raw/chat, data/raw/ai_chat에 그대로 있고, 그쪽이
Hermes의 Session Search("무슨 일이 있었나")에 해당한다. 이 저장소는 Frozen Core에
가까운 "내가 아는 것"만 담는다.

검색은 임베딩 코사인이다. 문장 단위라 텍스트가 짧아서 키워드 토큰 겹침으로는
거의 안 걸린다 — 실제로 "방금 내가 물어본 게 뭐였지?"가 직전 턴 기록을 못 찾았다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS facts (
    id          TEXT PRIMARY KEY,
    tier        TEXT NOT NULL,          -- stm | mtm | ltm
    text        TEXT NOT NULL,
    source_type TEXT,                   -- ai_chat | team_chat
    source_id   TEXT,                   -- 세션/방 id
    date        TEXT,                   -- 사실이 발생한 날짜
    created_at  TEXT,
    promoted_at TEXT,
    origin      TEXT,                   -- live | synthetic
    meta        TEXT,
    vec         BLOB,
    views       REAL DEFAULT 0.0,       -- 답변 기여도 누적합 (승격 판단 기준)
    returns     INTEGER DEFAULT 0,      -- 검색 결과로 반환된 횟수 (진단용)
    last_seen   TEXT,
    last_used   TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_tier ON facts(tier);
CREATE INDEX IF NOT EXISTS idx_facts_source ON facts(source_id);
CREATE INDEX IF NOT EXISTS idx_facts_date ON facts(date);
"""

# STM 최신성 창(일). 이 안에 발생한 사실만 STM에 들어올 수 있다.
STM_WINDOW_DAYS = 7


@dataclass
class Fact:
    id: str
    tier: str
    text: str
    date: str = ""
    source_type: str = ""
    source_id: str = ""
    origin: str = ""
    views: float = 0.0
    returns: int = 0
    meta: dict[str, Any] | None = None


def _fact_id(source_id: str, text: str) -> str:
    import hashlib

    return hashlib.sha1(f"{source_id}\x00{text}".encode("utf-8")).hexdigest()[:16]


class FactMemory:
    """사실 문장 저장소. 검색은 벡터, 승격 판단은 조회수."""

    def __init__(self, root: Path, embed_fn=None, track_access: bool = True):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.db_path = root / "facts.sqlite3"
        # check_same_thread=False: 이 커넥션을 만든 스레드가 아닌 다른 스레드에서도
        # 쓸 수 있게 한다. 기본값(True)이면 웹 서버가 "답변 먼저 응답, 뒷정리는
        # 백그라운드 스레드"로 나뉠 때 "SQLite objects created in a thread can only
        # be used in that same thread"로 죽는다. 동시 접근은 여기서 막지 않으므로
        # 호출부(chat_server 등)가 락으로 직렬화해야 한다 — sqlite3 자체가 스레드
        # 안전하지 않은 게 아니라, 파이썬 sqlite3 모듈이 기본으로 더 보수적으로
        # 막아둔 것뿐이라 직렬화만 지키면 안전하다.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()
        self._embed_fn = embed_fn
        self._embed_tried = embed_fn is not None
        self.track_access = track_access

    # ------------------------------------------------------------ 임베딩

    def embed_fn(self):
        if not self._embed_tried:
            self._embed_tried = True
            try:
                from doc_rag.config import load_config
                from doc_rag.embedding_factory import build_embedder

                embedder = build_embedder(load_config())
                self._embed_fn = lambda texts: embedder.embed(list(texts))
            except Exception:  # noqa: BLE001 - 임베딩이 없으면 검색만 못 한다
                self._embed_fn = None
        return self._embed_fn

    def _vectors(self, texts: list[str]) -> np.ndarray | None:
        fn = self.embed_fn()
        if fn is None or not texts:
            return None
        vecs = np.asarray(fn(texts), dtype="float32")
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
        return vecs

    # ------------------------------------------------------------ 쓰기

    def stm_cutoff(self, today: str | None = None,
                   window_days: int = STM_WINDOW_DAYS) -> str:
        """STM 진입 자격의 하한 날짜. 이 날짜보다 이전 사실은 STM에 못 들어온다."""
        anchor = _date.fromisoformat(today) if today else _date.today()
        return (anchor - timedelta(days=window_days - 1)).isoformat()

    def add(self, facts: Iterable[dict[str, Any]], tier: str = "stm",
            today: str | None = None, window_days: int = STM_WINDOW_DAYS) -> list[str]:
        """사실 문장들을 넣는다. 반환값은 실제로 저장된 id 목록.

        tier="stm"이면 **최신성 자격**을 본다. 사실의 date가 창 밖이면 저장하지 않는다.
        나중에 밀어내는 것이 아니라 처음부터 안 받는다 — 최신성은 STM의 정의 자체다.

        중복은 허용한다. 다른 대화에서 나온 같은 문장은 각각 저장된다.
        같은 출처의 같은 문장만 건너뛰는데, 이건 중복 제거가 아니라 재실행 안전장치다.
        memory_ingest를 다시 돌려도 레코드가 불어나지 않게 한다.
        """
        items = [f for f in facts if str(f.get("text", "")).strip()]
        if not items:
            return []

        if tier == "stm":
            cutoff = self.stm_cutoff(today, window_days)
            fresh, stale = [], 0
            for f in items:
                d = str(f.get("date") or "")
                if d and d < cutoff:
                    stale += 1
                    continue
                fresh.append(f)
            self.last_rejected_stale = stale
            items = fresh
            if not items:
                return []
        else:
            self.last_rejected_stale = 0

        now = datetime.now().isoformat(timespec="seconds")
        vecs = self._vectors([f["text"] for f in items])
        added: list[str] = []
        for i, f in enumerate(items):
            fid = _fact_id(str(f.get("source_id", "")), f["text"])
            if self.conn.execute("SELECT 1 FROM facts WHERE id = ?", (fid,)).fetchone():
                continue
            self.conn.execute(
                "INSERT INTO facts (id, tier, text, source_type, source_id, date, "
                "created_at, origin, meta, vec) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (fid, tier, f["text"], f.get("source_type", ""), f.get("source_id", ""),
                 f.get("date", ""), now, f.get("origin", ""),
                 json.dumps(f.get("meta") or {}, ensure_ascii=False),
                 vecs[i].tobytes() if vecs is not None else None),
            )
            added.append(fid)
        self.conn.commit()
        return added

    def candidates_in_tier(self, vec, tier: str, top_n: int = 5):
        """해당 계층에서 벡터와 가까운 후보들. ADD/UPDATE 판정 전 1차 필터다.

        리랭커는 쌍마다 모델을 돌려야 해서 전수 비교가 비싸다. 코사인으로 몇 개만
        추린 뒤 그것만 리랭커에 넘긴다. 코사인은 여기서 순위 판정이 아니라 후보
        선별에만 쓰이므로, 앞서 확인한 "코사인으로는 같은 사실을 못 가린다"는 한계와
        충돌하지 않는다.
        """
        rows = self.conn.execute(
            "SELECT * FROM facts WHERE tier = ? AND vec IS NOT NULL", (tier,)
        ).fetchall()
        if not rows:
            return []
        mat = np.stack([np.frombuffer(r["vec"], dtype="float32") for r in rows])
        sims = mat @ vec
        order = np.argsort(-sims)[:top_n]
        return [(self._to_fact(rows[i]), float(sims[i])) for i in order]

    def update_fact(self, target_id: str, new_text: str, add_views: float = 0.0,
                    add_returns: int = 0) -> bool:
        """기존 레코드의 문장을 새 문장으로 갈아끼운다(UPDATE).

        id는 유지한다. 조회수는 합산해서 승계한다 — 같은 사실을 가리키던 두 레코드가
        하나가 되는 것이므로, 각자 쌓은 조회수도 합쳐지는 것이 맞다.
        """
        vecs = self._vectors([new_text])
        cur = self.conn.execute(
            "UPDATE facts SET text = ?, vec = ?, views = views + ?, returns = returns + ?, "
            "promoted_at = ? WHERE id = ?",
            (new_text, vecs[0].tobytes() if vecs is not None else None,
             float(add_views), int(add_returns),
             datetime.now().isoformat(timespec="seconds"), target_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def retier(self, fact_ids: list[str], tier: str) -> int:
        """계층 표시만 바꾼다. 텍스트도 조회수도 그대로 둔다.

        승격이 이것뿐인 이유는 사실이 이미 원자 단위이기 때문이다. 합치거나 다시 쓸
        일이 없으니 LLM을 부르지 않고, 결과가 결정적이다.
        """
        if not fact_ids:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        cur = self.conn.executemany(
            "UPDATE facts SET tier = ?, promoted_at = ? WHERE id = ?",
            [(tier, now, fid) for fid in fact_ids],
        )
        self.conn.commit()
        return cur.rowcount

    def absorb(self, keeper_id: str, victim_ids: list[str]) -> int:
        """중복 레코드를 지우고 그 조회수를 남는 레코드에 넘긴다(DELETE).

        같은 사실을 가리키던 레코드가 하나로 합쳐지는 것이므로, 각자 쌓은 조회수도
        합쳐지는 것이 맞다. 조회수를 버리면 여러 번 참조된 사실이 승격·폐기 판정에서
        갑자기 약해진다.

        문장은 남는 쪽 것을 그대로 둔다. 어느 표현이 나은지 판정할 기준이 없어서,
        조회수가 높은(=실제로 답변에 쓰인) 쪽을 남기는 것으로 갈음한다.
        """
        victims = [v for v in victim_ids if v != keeper_id]
        if not victims:
            return 0
        rows = self.conn.execute(
            f"SELECT views, returns FROM facts WHERE id IN ({','.join('?' * len(victims))})",
            victims,
        ).fetchall()
        self.conn.execute(
            "UPDATE facts SET views = views + ?, returns = returns + ? WHERE id = ?",
            (sum(float(r["views"]) for r in rows),
             sum(int(r["returns"]) for r in rows), keeper_id),
        )
        cur = self.conn.execute(
            f"DELETE FROM facts WHERE id IN ({','.join('?' * len(victims))})", victims
        )
        self.conn.commit()
        return cur.rowcount

    def drop(self, fact_ids: list[str]) -> int:
        if not fact_ids:
            return 0
        cur = self.conn.execute(
            f"DELETE FROM facts WHERE id IN ({','.join('?' * len(fact_ids))})", fact_ids
        )
        self.conn.commit()
        return cur.rowcount

    def credit(self, fact_id: str, influence: float) -> None:
        """최종 답변에 반영된 정도만큼 조회수를 올린다."""
        if influence <= 0:
            return
        self.conn.execute(
            "UPDATE facts SET views = views + ?, last_used = ? WHERE id = ?",
            (float(influence), datetime.now().isoformat(timespec="seconds"), fact_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------ 읽기

    def search(self, query: str, tier: str = "all", top_k: int = 5,
               source_id: str | None = None,
               tier_budget: dict[str, int] | None = None) -> list[Fact]:
        """임베딩 코사인 검색.

        tier가 구체적 계층("stm"/"mtm"/"ltm")이면 그 계층 안에서만 top_k개를 뽑는다.

        tier="all"이면 **계층마다 따로** 검색해서 합친다. 전체를 한 풀에 놓고 코사인
        top_k로 자르면 안 된다 — STM은 수천 건, MTM/LTM은 수십 건 수준이라(실측:
        STM 2886 / MTM 6 / LTM 58) 계층을 안 나누고 자르면 top_k가 사실상 항상
        STM으로만 채워지고, 조회수를 쌓아 힘들게 승격시킨 MTM·LTM 사실이 관련성이
        더 높아도 STM 물량에 밀려 안 뽑힌다.

        tier_budget으로 계층별 개수를 정한다(기본은 계층마다 top_k개씩). 계층마다
        그 개수만큼 따로 뽑아 그대로 합친다 — 계층 간에 서로 경쟁시켜 깎아내지
        않는다. 관련도가 낮아도 각 계층에서 최소 tier_budget[tier]개는 후보로
        들어온다는 뜻이다. 프롬프트에 실리는 총량은 그만큼 늘지만(기본 5+5+5=15),
        "이 질문에 LTM 확정 사실이 있는데도 안 보인다" 같은 누락을 막는 게 우선이다.
        """
        if tier != "all":
            return self._search_tier(query, tier, top_k, source_id)

        budget = tier_budget or {t: top_k for t in ("stm", "mtm", "ltm")}
        pooled: list[Fact] = []
        for t, k in budget.items():
            if k <= 0:
                continue
            pooled.extend(self._search_tier(query, t, k, source_id))
        return pooled

    def _search_tier(self, query: str, tier: str, top_k: int,
                      source_id: str | None = None) -> list[Fact]:
        """구체적 계층 하나 안에서 코사인 top_k. search()의 tier='all' 분기가
        계층마다 이걸 따로 부른다.

        중복을 접지 않는다. 같은 문장이 여러 건 있으면 그대로 여러 건 나온다.
        """
        sql = "SELECT * FROM facts WHERE vec IS NOT NULL AND tier = ?"
        params: list[Any] = [tier]
        if source_id:
            sql += " AND source_id = ?"
            params.append(source_id)
        rows = self.conn.execute(sql, params).fetchall()
        if not rows:
            return []

        qvec = self._vectors([query])
        if qvec is None:
            return [self._to_fact(r) for r in rows[:top_k]]

        mat = np.stack([np.frombuffer(r["vec"], dtype="float32") for r in rows])
        sims = mat @ qvec[0]

        order = np.argsort(-sims)[:top_k]
        picked = [rows[i] for i in order]
        if self.track_access:
            now = datetime.now().isoformat(timespec="seconds")
            self.conn.executemany(
                "UPDATE facts SET returns = returns + 1, last_seen = ? WHERE id = ?",
                [(now, r["id"]) for r in picked],
            )
            self.conn.commit()
        out = []
        for r, i in zip(picked, order):
            fact = self._to_fact(r)
            fact.meta = {**(fact.meta or {}), "score": round(float(sims[i]), 4)}
            out.append(fact)
        return out

    def vectors_for(self, facts: list[Fact]) -> np.ndarray | None:
        """주어진 사실들의 저장된 벡터 행렬. 하나라도 없으면 None."""
        if not facts:
            return None
        rows = self.conn.execute(
            f"SELECT id, vec FROM facts WHERE id IN ({','.join('?' * len(facts))})",
            [f.id for f in facts],
        ).fetchall()
        by_id = {r["id"]: r["vec"] for r in rows}
        if any(by_id.get(f.id) is None for f in facts):
            return None
        return np.stack([np.frombuffer(by_id[f.id], dtype="float32") for f in facts])

    def aged_out(self, keep_after: str, tier: str = "stm") -> list[Fact]:
        """최신성 창을 벗어난 사실들. date < keep_after."""
        rows = self.conn.execute(
            "SELECT * FROM facts WHERE tier = ? AND date < ? ORDER BY source_id, date",
            (tier, keep_after),
        ).fetchall()
        return [self._to_fact(r) for r in rows]

    def all_facts(self, tier: str | None = None) -> list[Fact]:
        sql = "SELECT * FROM facts"
        params: list[Any] = []
        if tier:
            sql += " WHERE tier = ?"
            params.append(tier)
        sql += " ORDER BY tier, date DESC, views DESC"
        return [self._to_fact(r) for r in self.conn.execute(sql, params)]

    def stats(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT tier, COUNT(*), ROUND(SUM(views),3), SUM(returns), "
            "       ROUND(AVG(LENGTH(text)),1) FROM facts GROUP BY tier"
        ).fetchall()
        return {
            r[0]: {"facts": r[1], "views": r[2] or 0, "returns": r[3] or 0,
                   "avg_chars": r[4]}
            for r in rows
        }

    @staticmethod
    def _to_fact(row: sqlite3.Row) -> Fact:
        return Fact(
            id=row["id"], tier=row["tier"], text=row["text"], date=row["date"] or "",
            source_type=row["source_type"] or "", source_id=row["source_id"] or "",
            origin=row["origin"] or "", views=float(row["views"]),
            returns=int(row["returns"]), meta=json.loads(row["meta"] or "{}"),
        )

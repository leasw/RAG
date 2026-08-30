"""지금 하는 대화를 원본 로그로 남기고, 사실 문장을 뽑아 STM에 넣는다.

에이전트와 나눈 대화도 채팅이다. 설계상 STM의 원천은 "채팅에서만 발생"이므로,
사용자가 질문하고 답을 받은 기록은 그 자리에서 STM이 되어야 한다. 그래야 다음 턴에
"아까 그거"가 성립한다.

두 곳에 쓴다.

    data/raw/ai_chat/<session_id>.jsonl   원문 그대로 (재현성·감사용)
    facts.sqlite3 (tier=stm)              뽑아낸 사실 문장

원본을 남기는 이유는 재현성이다. `python -m org_agent_mvp.memory_ingest`를 다시
돌리면 같은 사실이 나와야 하고, 그러려면 원본 로그가 있어야 한다.

묶는 단위는 세션이다. CLI 한 번 실행(대화형이면 종료할 때까지)이 한 세션이고,
원본 로그는 턴이 늘 때마다 같은 파일을 다시 쓴다. 사실 추출은 턴 단위로 한다 —
세션 전체를 매번 다시 넣으면 같은 사실이 반복 추출되고 비용도 턴마다 커진다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .fact_extractor import FactExtractor
from .fact_memory import FactMemory

ROOT = Path(__file__).resolve().parents[1]
RAW_AI = ROOT / "data" / "raw" / "ai_chat"


class SessionRecorder:
    """한 세션의 대화를 원본 로그로 남기고, 사실 문장을 뽑아 STM에 넣는다.

    저장이 두 갈래인 것은 Hermes의 계층 분리와 같다.

        data/raw/ai_chat/*.jsonl   원문 그대로 — "무슨 일이 있었나"
        facts.sqlite3 (tier=stm)   뽑아낸 사실 문장 — "내가 아는 것"

    원문을 STM에 통째로 넣지 않는다. 프롬프트에 실려 나가는 것은 사실 문장 쪽이라
    밀도가 높아야 하고, 원문은 필요할 때 raw 로그에서 찾으면 된다.
    """

    def __init__(self, user: str = "user", session_id: str | None = None,
                 raw_dir: Path = RAW_AI, memory: FactMemory | None = None,
                 extractor: FactExtractor | None = None):
        started = datetime.now()
        self.session_id = session_id or (
            f"s-live-{started.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
        )
        self.user = user
        self.date = started.strftime("%Y-%m-%d")
        self.raw_dir = raw_dir
        self.memory = memory
        self.extractor = extractor
        self.turns: list[tuple[str, str, str]] = []   # (hhmm, role, text)
        self.title = ""
        self.facts_added: list[str] = []

    # ------------------------------------------------------------ 기록

    def record_turn(self, question: str, answer: str) -> dict:
        now = datetime.now().strftime("%H:%M")
        self.turns.append((now, "user", question))
        self.turns.append((now, "assistant", answer or ""))
        if not self.title:
            self.title = question.strip()[:40]

        self._write_raw()

        # 이번 턴만 보고 사실을 뽑는다. 세션 전체를 매번 다시 넣으면 같은 사실이
        # 반복 추출되고 비용도 턴마다 커진다.
        added: list[str] = []
        if self.memory is not None and self.extractor is not None:
            facts = self.extractor.extract([("user", question), ("assistant", answer or "")])
            added = self.memory.add(
                [{
                    "text": f,
                    "source_type": "ai_chat",
                    "source_id": self.session_id,
                    "date": self.date,
                    "origin": "live",
                    "meta": {"turn": len(self.turns) // 2},
                } for f in facts],
                tier="stm",
            )
            self.facts_added.extend(added)
        return {"raw": str(self.raw_dir / f"{self.session_id}.jsonl"),
                "facts_added": len(added), "fact_ids": added}

    def _write_raw(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({
                "turn_id": f"{self.session_id}-{i:03d}",
                "ts": f"{self.date}T{hhmm}:00",
                "session_id": self.session_id,
                "user": self.user,
                "role": role,
                "text": text,
            }, ensure_ascii=False)
            for i, (hhmm, role, text) in enumerate(self.turns, start=1)
        ]
        (self.raw_dir / f"{self.session_id}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        self._update_index()

    def _update_index(self) -> None:
        path = self.raw_dir / "sessions.json"
        data: dict[str, Any] = {"sessions": []}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        sessions = [s for s in data.get("sessions", []) if s.get("session_id") != self.session_id]
        sessions.append({
            "session_id": self.session_id,
            "title": self.title,
            "user": self.user,
            "date": self.date,
            "start_ts": f"{self.date}T{self.turns[0][0]}:00",
            "end_ts": f"{self.date}T{self.turns[-1][0]}:00",
            "turn_count": len(self.turns),
            "user_turns": sum(1 for _, r, _ in self.turns if r == "user"),
            "file": f"{self.session_id}.jsonl",
            "origin": "live",
        })
        data["sessions"] = sessions
        data.setdefault(
            "note",
            "s-live-* 는 에이전트와 실제로 나눈 대화, 그 밖은 가상 생성 데이터.",
        )
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

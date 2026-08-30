"""로우 채팅 로그 -> STM 사실 문장 배치 적재.

    python -m org_agent_mvp.memory_ingest              # 활동이 있는 최근 한 주
    python -m org_agent_mvp.memory_ingest --anchor 2022-01-04
    python -m org_agent_mvp.memory_ingest --dry-run    # 무엇이 들어갈지만

실시간 경로(session_recorder)와 같은 일을 배치로 한다. 차이는 입력뿐이다.

    실시간  지금 나눈 한 턴          -> 사실 추출 -> STM
    배치    data/raw/의 과거 로그    -> 사실 추출 -> STM

원문은 여기서 옮기지 않는다. data/raw/에 그대로 있고, 그쪽이 Hermes의 Session
Search("무슨 일이 있었나")에 해당한다. 이 모듈은 "내가 아는 것"만 만든다.

묶는 단위가 다르다. 팀 채팅은 (방, 날짜) 하나가 한 덩어리이고, AI 대화는 세션 하나가
한 덩어리다. AI 세션은 하루에 여러 건이 있어도 맥락이 이어지지 않아 날짜로 합치면
안 되고, 반대로 팀 채팅방은 하루 안에서 대화가 이어지므로 쪼개면 안 된다.

덩어리 하나당 LLM을 한 번 부른다. 창을 넓게 잡으면 호출 수가 그만큼 늘어나므로
--max-groups로 상한을 둘 수 있다. 같은 (출처, 문장)은 중복 저장되지 않아서 다시
돌려도 안전하다.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from .config import AppConfig
from .fact_extractor import FactExtractor
from .fact_memory import FactMemory
from .openrouter_client import OpenRouterClient

ROOT = Path(__file__).resolve().parents[1]
RAW_CHAT = ROOT / "data" / "raw" / "chat"
RAW_AI = ROOT / "data" / "raw" / "ai_chat"

# STM 최신성 창. 진입 자격을 정의하는 fact_memory의 값을 그대로 쓴다.
from .fact_memory import STM_WINDOW_DAYS  # noqa: E402


def _read_jsonl(directory: Path) -> list[dict]:
    rows: list[dict] = []
    if not directory.exists():
        return rows
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _meta(directory: Path, name: str) -> dict:
    path = directory / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def pick_anchor(days: list[str], window: int, min_messages: int,
                counts: dict[str, int]) -> str:
    """활동이 있는 최근 한 주의 마지막 날.

    데이터의 마지막 날을 그대로 쓰면 꼬리에 남은 단발성 기록 때문에 거의 빈 창이
    잡힌다. 그래서 최신 날짜부터 거꾸로 내려가며, 창 안 메시지가 min_messages 이상인
    첫 날을 쓴다.
    """
    for anchor in reversed(days):
        start = (date.fromisoformat(anchor) - timedelta(days=window - 1)).isoformat()
        if sum(n for d, n in counts.items() if start <= d <= anchor) >= min_messages:
            return anchor
    return days[-1] if days else date.today().isoformat()


def _group(chat_rows: list[dict], ai_rows: list[dict],
           rooms: dict, sessions: dict) -> list[dict]:
    """추출 단위로 묶는다. 각 덩어리가 LLM 한 번에 대응한다."""
    room_name = {r["room_id"]: r["name"] for r in rooms.get("rooms", [])}
    title_of = {s["session_id"]: s["title"] for s in sessions.get("sessions", [])}

    buckets: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in chat_rows:
        buckets[("team_chat", r["room_id"], r["ts"][:10])].append(r)
    for r in ai_rows:
        buckets[("ai_chat", r["session_id"], r["ts"][:10])].append(r)

    groups = []
    for (source_type, source_id, day), rows in sorted(buckets.items()):
        rows.sort(key=lambda x: x["ts"])
        if source_type == "team_chat":
            label = room_name.get(source_id, source_id)
            turns = [("user", f"{r['sender']}: {r['text']}") for r in rows]
        else:
            label = title_of.get(source_id, source_id)
            turns = [(r["role"], r["text"]) for r in rows]
        groups.append({
            "source_type": source_type, "source_id": source_id, "date": day,
            "label": label, "turns": turns, "messages": len(rows),
        })
    return groups


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=STM_WINDOW_DAYS,
                    help="STM 최신성 기준. 설계안 기본값은 1주일.")
    ap.add_argument("--anchor", default="auto",
                    help="창의 마지막 날 (YYYY-MM-DD). auto면 활동이 있는 최근 한 주.")
    ap.add_argument("--min-messages", type=int, default=20,
                    help="auto 기준일 선택 시 창이 가져야 할 최소 메시지 수.")
    ap.add_argument("--max-groups", type=int, default=0,
                    help="추출할 덩어리 수 상한(=LLM 호출 수). 0이면 무제한.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    chat = _read_jsonl(RAW_CHAT)
    ai = _read_jsonl(RAW_AI)
    if not chat and not ai:
        print("로우 로그가 없습니다. data/raw/chat, data/raw/ai_chat 확인.")
        return 1

    counts: dict[str, int] = defaultdict(int)
    for row in chat + ai:
        counts[row["ts"][:10]] += 1
    days = sorted(counts)

    anchor = args.anchor
    if anchor == "auto":
        anchor = pick_anchor(days, args.window_days, args.min_messages, counts)
    start = (date.fromisoformat(anchor) - timedelta(days=args.window_days - 1)).isoformat()

    in_window = lambda r: start <= r["ts"][:10] <= anchor  # noqa: E731
    chat_w = [r for r in chat if in_window(r)]
    ai_w = [r for r in ai if in_window(r)]

    groups = _group(chat_w, ai_w, _meta(RAW_CHAT, "rooms.json"), _meta(RAW_AI, "sessions.json"))
    if args.max_groups:
        groups = groups[: args.max_groups]

    print(f"STM 최신성 기준: {start} ~ {anchor} ({args.window_days}일)")
    print(f"  팀 채팅 {len(chat_w)} / {len(chat)} 메시지")
    print(f"  AI 대화 {len(ai_w)} / {len(ai)} 턴")
    print(f"  추출 단위 {len(groups)}덩어리 (= LLM 호출 {len(groups)}회)\n")

    if args.dry_run:
        for g in groups:
            print(f"  [{g['date']}] {g['source_type']:10s} {g['label'][:40]:42s} "
                  f"{g['messages']:3d}메시지")
        print("\n(dry-run: 추출하지 않았습니다)")
        return 0

    config = AppConfig.load()
    memory = FactMemory(config.memory_root)
    extractor = FactExtractor(OpenRouterClient(config))

    total_facts = 0
    for i, g in enumerate(groups, start=1):
        facts = extractor.extract(g["turns"])
        added = memory.add(
            [{
                "text": f, "source_type": g["source_type"], "source_id": g["source_id"],
                "date": g["date"], "origin": "synthetic",
                "meta": {"label": g["label"]},
            } for f in facts],
            tier="stm",
        )
        total_facts += len(added)
        print(f"  [{i}/{len(groups)}] {g['label'][:38]:40s} "
              f"{g['messages']:3d}메시지 -> 사실 {len(facts):2d}개 (신규 {len(added)})")
        for f in facts[:3]:
            print(f"        · {f[:76]}")

    print(f"\n신규 사실 {total_facts}개")
    print("[계층 상태]", json.dumps(memory.stats(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""메모리 데이터를 뷰어용 JSON으로 내보낸다.

    python viewer/export_data.py

facts.sqlite3(STM/MTM 현재 상태), sim_archive/facts_archive.jsonl(생성·승격·
삭제 이력), data/raw/의 채팅방·세션 원문을 읽어 viewer/data.json 하나로 합친다.
뷰어(index.html)는 이 파일만 fetch해서 그린다.
"""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "memory_seed" / "facts.sqlite3"
ARCHIVE = ROOT / "data" / "sim_archive" / "facts_archive.jsonl"
RAW_CHAT = ROOT / "data" / "raw" / "chat"
RAW_AI = ROOT / "data" / "raw" / "ai_chat"
OUT = Path(__file__).resolve().parent / "data.json"


def load_facts():
    if not DB.exists():
        return []
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, tier, text, date, source_type, source_id, origin, "
        "views, returns, created_at, promoted_at, last_seen, last_used, meta "
        "FROM facts ORDER BY tier, views DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except json.JSONDecodeError:
            d["meta"] = {}
        out.append(d)
    conn.close()
    return out


def load_archive():
    if not ARCHIVE.exists():
        return []
    out = []
    for line in ARCHIVE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def load_rooms() -> dict:
    """팀 채팅방. key = room_id. 사실의 source_type='team_chat'/source_id가 여기로 연결된다."""
    meta_path = RAW_CHAT / "rooms.json"
    if not meta_path.exists():
        return {}
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    rooms = {}
    for r in meta.get("rooms", []):
        msgs = _read_jsonl(RAW_CHAT / r["file"])
        rooms[r["room_id"]] = {
            "id": r["room_id"], "name": r.get("name", r["room_id"]),
            "period": r.get("period", ""), "members": r.get("members", []),
            "message_count": len(msgs),
            "messages": [
                {"ts": m.get("ts"), "who": m.get("sender"), "text": m.get("text")}
                for m in msgs
            ],
        }
    return rooms


def load_sessions() -> dict:
    """AI 대화 세션. key = session_id. 사실의 source_type='ai_chat'/source_id가 여기로 연결된다."""
    meta_path = RAW_AI / "sessions.json"
    if not meta_path.exists():
        return {}
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    sessions = {}
    for s in meta.get("sessions", []):
        turns = _read_jsonl(RAW_AI / s["file"])
        sessions[s["session_id"]] = {
            "id": s["session_id"], "name": s.get("title", s["session_id"]),
            "date": s.get("date", ""), "user": s.get("user", ""),
            "origin": s.get("origin", ""),
            "message_count": len(turns),
            "messages": [
                {"ts": t.get("ts"),
                 "who": t.get("user") if t.get("role") == "user" else "assistant",
                 "role": t.get("role"), "text": t.get("text")}
                for t in turns
            ],
        }
    return sessions


def main():
    facts = load_facts()
    archive = load_archive()
    rooms = load_rooms()
    sessions = load_sessions()
    stats = {}
    for f in facts:
        stats.setdefault(f["tier"], {"facts": 0, "views": 0.0})
        stats[f["tier"]]["facts"] += 1
        stats[f["tier"]]["views"] += f["views"]

    OUT.write_text(json.dumps({
        "facts": facts,
        "archive": archive,
        "stats": stats,
        "rooms": rooms,
        "sessions": sessions,
    }, ensure_ascii=False, indent=None), encoding="utf-8")
    print(f"facts {len(facts)}건 / archive {len(archive)}건 / "
          f"채팅방 {len(rooms)}개 / AI 세션 {len(sessions)}개 -> {OUT}")


if __name__ == "__main__":
    main()

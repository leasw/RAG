"""사람 채팅 반 + 챗봇 채팅 반으로 STM을 채우고, MTM 200건까지 자연스럽게 승격시킨다.

이전 시도들의 문제:
    1차(sim_fill_mtm.py)   대화 자체를 지어내 STM에 바로 꽂음 -> 원본이 처음부터 가짜.
    2차(sim_real_chat.py)  실제 파이프라인을 태웠지만, 질문이 전부 "저번에 그거
                           얼마였지" 류의 메모리 재질문뿐이었다. search_documents /
                           search_graph 도구가 한 번도 안 걸렸다 -> 벡터RAG(LTM),
                           그래프RAG 순환이 빠진 반쪽짜리 시뮬레이션이었다.

이번엔 둘을 섞는다.

    사람 채팅 (절반)  LLM으로 팀 채팅 대화를 지어 SessionRecorder로 STM에 직접
                      적재한다(에이전트가 관여하지 않는, 진짜 사람 대 사람 대화이므로
                      도구 호출이 애초에 없다 — 그게 정상이다).
                      data/raw/chat/*.jsonl + rooms.json에도 남겨 뷰어의 "채팅방"
                      탭에서 team_chat으로 보이게 한다.

    챗봇 채팅 (절반)  실제 AgentRuntime.run()에 진짜 질문을 던진다. 질문은 세 종류를
                      섞는다 — 어느 게 걸릴지는 실제 검색이 정한다:
                        - 문서 질문   실제 2단계 과제 문서에 있는 내용(정량목표, 금액
                                      등)을 물어 search_documents 도구가 실제로
                                      걸리게 한다.
                        - 그래프 질문 실제 그래프 스키마의 기관/인물/과업 관계를 물어
                                      search_graph 도구가 실제로 걸리게 한다.
                        - 메모리 대화 새 주제 + 방금 대화의 자연스러운 후속 질문을
                                      섞어, STM/MTM 재조회로 조회수가 쌓이게 한다.

    python .scratch/sim_full_pipeline.py
"""
import json, random, re, sys
sys.path.insert(0, r"D:\LAB_RAG\Org-AI-Body")
import os
os.chdir(r"D:\LAB_RAG\Org-AI-Body")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

from datetime import date

from org_agent_mvp.config import AppConfig
from org_agent_mvp.fact_extractor import FactExtractor
from org_agent_mvp.fact_memory import FactMemory
from org_agent_mvp.openrouter_client import OpenRouterClient
from org_agent_mvp.session_recorder import SessionRecorder
from org_agent_mvp.__main__ import build_runtime

# ---------------------------------------------------------------- 보관용 아카이브
#
# facts.sqlite3는 실제 서비스 상태라 승격/중복정리/폐기로 지워지는 레코드가 있다.
# 이 시뮬레이션이 만든 것은 지워진 것까지 전부 남겨야 하므로, FactMemory 클래스
# 메서드를 감싸서 삭제 직전 상태를 별도 JSONL에 이어붙인다. memory_promote.run()
# (여기서는 agent_runtime._check_promotion을 통해 매 턴 호출됨)이 자기 자신의
# FactMemory 인스턴스를 새로 만들 수 있으므로, 인스턴스가 아니라 클래스 자체를
# 패치해야 어떤 인스턴스를 거치든 잡힌다.
ARCHIVE_PATH = "data/sim_archive/facts_archive.jsonl"


def _archive(event: str, facts: list, **extra) -> None:
    os.makedirs(os.path.dirname(ARCHIVE_PATH), exist_ok=True)
    ts_now = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    with open(ARCHIVE_PATH, "a", encoding="utf-8") as f:
        for fc in facts:
            row = {
                "event": event, "archived_at": ts_now,
                "id": fc.id, "tier": fc.tier, "text": fc.text, "date": fc.date,
                "source_type": fc.source_type, "source_id": fc.source_id,
                "origin": fc.origin, "views": fc.views, "returns": fc.returns,
            }
            row.update(extra)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _fetch_facts(memory, ids: list[str]) -> list:
    if not ids:
        return []
    rows = memory.conn.execute(
        f"SELECT * FROM facts WHERE id IN ({','.join('?' * len(ids))})", ids
    ).fetchall()
    return [memory._to_fact(r) for r in rows]


def install_archive_hooks() -> None:
    """FactMemory.add/retier/update_fact/absorb/drop을 감싸 아카이브를 남긴다."""
    orig_add = FactMemory.add
    orig_retier = FactMemory.retier
    orig_update_fact = FactMemory.update_fact
    orig_absorb = FactMemory.absorb
    orig_drop = FactMemory.drop

    def add(self, facts, tier="stm", **kw):
        ids = orig_add(self, facts, tier=tier, **kw)
        _archive("created", _fetch_facts(self, ids))
        return ids

    def retier(self, fact_ids, tier):
        before = _fetch_facts(self, fact_ids)
        n = orig_retier(self, fact_ids, tier)
        _archive("promoted", before, to_tier=tier)
        return n

    def update_fact(self, target_id, new_text, add_views=0.0, add_returns=0):
        before = _fetch_facts(self, [target_id])
        ok = orig_update_fact(self, target_id, new_text, add_views=add_views,
                               add_returns=add_returns)
        if before:
            _archive("updated", before, new_text=new_text)
        return ok

    def absorb(self, keeper_id, victim_ids):
        victims = [v for v in victim_ids if v != keeper_id]
        before = _fetch_facts(self, victims)
        n = orig_absorb(self, keeper_id, victim_ids)
        _archive("absorbed_duplicate", before, kept_id=keeper_id)
        return n

    def drop(self, fact_ids):
        before = _fetch_facts(self, fact_ids)
        n = orig_drop(self, fact_ids)
        _archive("dropped", before)
        return n

    FactMemory.add = add
    FactMemory.retier = retier
    FactMemory.update_fact = update_fact
    FactMemory.absorb = absorb
    FactMemory.drop = drop


TARGET_MTM = 50
MAX_ROUNDS = 120
TEAM_CONVOS_PER_ROUND = 3     # 사람 채팅: 라운드마다 새로 만드는 채팅방 수
CHATBOT_TURNS_PER_ROUND = 6   # 챗봇 채팅: 라운드마다 던지는 질문 수

# ---------------------------------------------------------------- 사람 채팅
TEAM_TOPICS = [
    "예산 집행 및 이월", "인건비 정산", "정량목표 달성률", "회의 일정 조율",
    "담당자 변경", "협약변경 절차", "시범서비스 설문 결과", "장애물 인식 성능 개선",
    "제품 펌웨어 업데이트", "출장 및 외부 미팅", "보고서 작성 마감", "데이터 라벨링 진행",
    "테스트베드 운영", "특허 출원 검토", "산학협력 계약", "인력 채용 진행상황",
    "품질 이슈 대응", "고객 피드백 정리", "예산 정산 감사 대비", "차년도 계획 수립",
    "센서 캘리브레이션", "배터리 수명 테스트", "앱 UI 개선", "음성 안내 콘텐츠 제작",
    "협력기관 미팅", "성과 지표 보고", "인프라 서버 점검", "보안 점검 결과",
    "사용자 교육 자료", "현장 실증 일정",
]

TEAM_GEN_PROMPT = """다음은 한 조직에서 실제로 오갈 법한 업무 팀 채팅 대화다.
주제: {topic}

짧은 대화 3~5개를 만들어라. 각 대화는 사용자 발화 1~2개 + 어시스턴트(동료) 응답 1개로
구성. 구체적인 수치, 날짜, 담당자 이름(가상), 결정 사항을 하나씩 포함해라. 실제 공식
문서에는 없는, 내부 채팅에서만 오갈 법한 세부사항으로 만들어라.

반드시 아래 JSON 형식만 출력해라:
{{"conversations": [{{"question": "...", "answer": "..."}}]}}
"""


def _parse_json_block(text):
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def gen_team_convo(client, topic):
    msg = client.chat(
        [{"role": "system", "content": "너는 조직 내부 업무 대화를 생성하는 데이터 생성기다."},
         {"role": "user", "content": TEAM_GEN_PROMPT.format(topic=topic)}],
        tools=[],
    )
    data = _parse_json_block(msg.get("content")) or {}
    out = []
    for c in data.get("conversations", []):
        q, a = str(c.get("question", "")).strip(), str(c.get("answer", "")).strip()
        if q and a:
            out.append((q, a))
    return out


class TeamChatWriter:
    """팀 채팅 대화를 STM에 적재하면서 data/raw/chat/에도 남긴다(뷰어용)."""

    def __init__(self, memory: FactMemory, extractor: FactExtractor):
        self.memory = memory
        self.extractor = extractor
        self.raw_dir = ROOT_RAW_CHAT
        self.room_seq = self._next_seq()

    @staticmethod
    def _next_seq():
        existing = list(ROOT_RAW_CHAT.glob("room-*.jsonl"))
        nums = [int(p.stem.split("-")[1]) for p in existing if p.stem.split("-")[1].isdigit()]
        return (max(nums) + 1) if nums else 1

    def write_room(self, topic: str, pairs: list[tuple[str, str]]) -> dict:
        room_id = f"room-{self.room_seq:03d}"
        self.room_seq += 1
        today = date.today().isoformat()
        rows = []
        hh = 9
        for i, (q, a) in enumerate(pairs):
            ts = f"{today}T{hh:02d}:{(i * 7) % 60:02d}:00"
            rows.append({"msg_id": f"{room_id}-{i:02d}", "ts": ts, "room_id": room_id,
                         "sender": "팀원", "text": q})
            rows.append({"msg_id": f"{room_id}-{i:02d}b", "ts": ts, "room_id": room_id,
                         "sender": "동료", "text": a})
        (self.raw_dir / f"{room_id}.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        self._update_rooms_json(room_id, topic, len(rows))

        added_total = 0
        for q, a in pairs:
            facts = self.extractor.extract([("user", q), ("assistant", a)])
            added = self.memory.add(
                [{"text": f, "source_type": "team_chat", "source_id": room_id,
                  "date": today, "origin": "synthetic", "meta": {"topic": topic}}
                 for f in facts], tier="stm")
            added_total += len(added)
        return {"room_id": room_id, "facts_added": added_total, "messages": len(rows)}

    def _update_rooms_json(self, room_id, topic, msg_count):
        meta_path = self.raw_dir / "rooms.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {"rooms": []}
        meta.setdefault("rooms", [])
        meta["rooms"].append({
            "room_id": room_id, "name": topic, "period": date.today().isoformat(),
            "members": ["팀원", "동료"], "message_count": msg_count,
            "file": f"{room_id}.jsonl",
        })
        meta["room_count"] = len(meta["rooms"])
        meta["message_count"] = sum(r["message_count"] for r in meta["rooms"])
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


ROOT_RAW_CHAT = None  # main()에서 채운다


# ---------------------------------------------------------------- 챗봇 채팅
# 실제 문서/그래프에 있는 내용을 물어서, LLM이 search_documents / search_graph를
# 실제로 호출하도록 유도한다. 정답을 우리가 넣는 게 아니라, 에이전트가 실제 도구를
# 불러서 스스로 찾게 둔다.
DOC_QUESTIONS = [
    "이 과제 1차년도 정부출연금이 얼마였는지 문서에서 찾아줄 수 있어?",
    "장애물 감지 인식률 목표치가 몇 %였는지 문서 좀 확인해줘",
    "스마트 글래스 기능 신뢰성 1차년도 실적이 몇 %였는지 알려줘",
    "위험감지 센서 응답 속도 목표가 어떻게 되는지 문서에서 찾아줘",
    "가천대 인건비 이월 요청액이 얼마였는지 공식 문서 기준으로 알려줘",
    "시범서비스 3차 평가 결과가 문서상 몇 점이었는지 확인해줘",
    "데이터 전송 정확도 목표치와 1차년도 실적이 각각 몇 %였어?",
    "휴대 운용 시간 목표가 몇 시간이었는지 문서에서 찾아봐줘",
]

GRAPH_QUESTIONS = [
    "이 과제 연구책임자가 누구야?",
    "피씨티는 이 프로젝트에서 무슨 역할을 맡고 있어?",
    "가천대산학협력단이랑 관련된 연구원이 누구누구 있어?",
    "HMD는 몇 차년도에 개발하는 부분이야?",
    "이 프로젝트 전문기관이 어디야?",
    "선행기술 보유 기관으로 파악된 곳이 어디어디야?",
    "정정일 님은 어느 기관 소속이고 무슨 역할이야?",
    "이 과제의 특허 관련 협력 법률사무소가 어디어디 있어?",
]

MEMORY_TOPICS = TEAM_TOPICS  # 새 주제 질문 소재는 팀 채팅과 같은 업무 주제 풀 재사용

MEM_NEW_TOPIC_PROMPT = """사내 업무 챗봇에게 묻는 상황이다. 주제: {topic}

이 주제에 대해 실제 직원이 물어볼 법한 **자연스러운 구어체 질문 하나**를 만들어라.
구체적인 상황을 가정해라. 짧고 캐주얼하게, 질문 하나만 출력해라. 설명이나 따옴표 없이."""

MEM_FOLLOWUP_PROMPT = """방금 이런 대화를 나눴다.
질문: {q}
답변: {a}

이 대화에 이어지는 **자연스러운 후속 질문 하나**를 만들어라. 짧고 캐주얼하게,
질문 하나만 출력해라. 설명이나 따옴표 없이."""


def gen_llm_question(client, prompt) -> str:
    msg = client.chat(
        [{"role": "system", "content": "너는 자연스러운 업무 챗봇 질문을 만드는 도우미다."},
         {"role": "user", "content": prompt}],
        tools=[],
    )
    return (msg.get("content") or "").strip().strip('"').strip()


# 세션 하나가 몇 턴짜리인지. 실제 챗봇 사용처럼 짧게 묻고 끝내는 경우가 가장
# 흔하고, 가끔 대화가 길게 이어지는 정도로 분포를 잡는다.
SESSION_LENGTH_CHOICES = [1, 1, 2, 2, 2, 3, 3, 4]


def new_session(runtime, client) -> None:
    """새 AI 세션을 시작한다 — 진짜 사용자가 챗봇을 새로 켠 것처럼 별도 세션으로 남는다."""
    runtime.recorder = SessionRecorder(
        user="user", memory=runtime.memory, extractor=FactExtractor(client)
    )
    print(f"  [새 세션] {runtime.recorder.session_id}")


def pick_chatbot_question(client, session_history):
    """세 종류 중 하나를 고른다: 문서 질문 / 그래프 질문 / 메모리 대화(신규 or 이어가기).

    session_history는 "이번 세션 안에서" 오간 대화만 담는다 — 세션을 넘나들며
    이어가기를 하면 서로 다른 챗봇 세션이 내용상 이어져버려서, 실제 여러 세션을
    켰다 껐다 하는 모습과 달라진다. 그래서 세션이 바뀌면 이 목록도 비워야 한다
    (호출부 main()에서 그렇게 관리한다).
    """
    if session_history and random.random() < 0.7:
        prev_q, prev_a = random.choice(session_history)
        return "memory-followup", gen_llm_question(client, MEM_FOLLOWUP_PROMPT.format(q=prev_q, a=prev_a))

    kind = random.choices(
        ["doc", "graph", "memory"], weights=[0.25, 0.25, 0.50], k=1
    )[0]
    if kind == "doc":
        return "doc", random.choice(DOC_QUESTIONS)
    if kind == "graph":
        return "graph", random.choice(GRAPH_QUESTIONS)
    topic = random.choice(MEMORY_TOPICS)
    return "memory-new", gen_llm_question(client, MEM_NEW_TOPIC_PROMPT.format(topic=topic))


def main():
    install_archive_hooks()

    global ROOT_RAW_CHAT
    from pathlib import Path
    ROOT_RAW_CHAT = Path("data/raw/chat")
    ROOT_RAW_CHAT.mkdir(parents=True, exist_ok=True)

    config = AppConfig.load()
    qgen_client = OpenRouterClient(config)
    runtime = build_runtime(config, use_mock=False, record=True)
    team_writer = TeamChatWriter(runtime.memory, FactExtractor(qgen_client))

    existing = runtime.memory.all_facts()
    if existing and not os.path.exists(ARCHIVE_PATH):
        _archive("backfill_existing", existing)
        print(f"  [아카이브] 기존 사실 {len(existing)}건 백필")

    session_history = []   # 지금 열려있는 챗봇 세션 안에서만 오간 대화
    turns_left_in_session = 0
    round_no = 0
    totals = {"team_facts": 0, "chatbot_turns": 0, "promoted": 0}

    # 도구 호출 누적 카운터. 재시작해도 유지되도록 파일에서 이어 읽는다 — 인메모리
    # 변수로만 두면 스크립트를 다시 켤 때마다 0으로 리셋돼서, 실제 누적 호출
    # 횟수와 안 맞게 된다(이전에 그 문제가 실제로 있었다: 재시작 전 128턴이
    # 카운터에서 통째로 빠짐).
    STATS_PATH = "viewer/tool_call_stats.json"
    if os.path.exists(STATS_PATH):
        with open(STATS_PATH, encoding="utf-8") as f:
            prev = json.load(f)
        tool_calls = prev.get("tool_calls",
                              {"search_documents": 0, "search_graph": 0, "memory_prefetch": 0})
        totals["chatbot_turns"] = prev.get("chatbot_turns_cumulative", 0)
    else:
        tool_calls = {"search_documents": 0, "search_graph": 0, "memory_prefetch": 0}

    def save_tool_stats():
        with open(STATS_PATH, "w", encoding="utf-8") as f:
            json.dump({"tool_calls": tool_calls,
                       "chatbot_turns_cumulative": totals["chatbot_turns"]},
                      f, ensure_ascii=False, indent=2)

    while round_no < MAX_ROUNDS:
        round_no += 1
        stats = runtime.memory.stats()
        mtm_n = stats.get("mtm", {}).get("facts", 0)
        stm_n = stats.get("stm", {}).get("facts", 0)
        print(f"\n===== 라운드 {round_no} | MTM {mtm_n}/{TARGET_MTM}  (STM {stm_n}) =====", flush=True)
        if mtm_n >= TARGET_MTM:
            print("목표 달성.")
            break

        # ---- 사람 채팅 (절반) ----
        for i in range(TEAM_CONVOS_PER_ROUND):
            topic = random.choice(TEAM_TOPICS)
            try:
                pairs = gen_team_convo(qgen_client, topic)
            except Exception as exc:
                print(f"  [팀채팅 생성 실패] {topic}: {type(exc).__name__}: {exc}")
                continue
            if not pairs:
                continue
            info = team_writer.write_room(topic, pairs)
            totals["team_facts"] += info["facts_added"]
            print(f"  [사람] {info['room_id']} ({topic}) 대화 {len(pairs)}개 -> 사실 {info['facts_added']}개")

        # ---- 챗봇 채팅 (절반) ----
        for _ in range(CHATBOT_TURNS_PER_ROUND):
            if turns_left_in_session <= 0:
                new_session(runtime, qgen_client)
                session_history = []
                turns_left_in_session = random.choice(SESSION_LENGTH_CHOICES)

            kind, question = pick_chatbot_question(qgen_client, session_history)
            if not question:
                turns_left_in_session -= 1
                continue
            try:
                result = runtime.run(question)
            except Exception as exc:
                print(f"  [챗봇 응답 실패] {type(exc).__name__}: {exc}")
                turns_left_in_session -= 1
                continue
            answer = result.get("answer") or ""
            totals["chatbot_turns"] += 1
            session_history.append((question, answer))
            turns_left_in_session -= 1
            promoted = result.get("promoted")
            tag = ""
            if promoted:
                n = promoted.get("add", 0) + promoted.get("update", 0)
                totals["promoted"] += n
                tag = f" -> 승격 {n}건"

            # 이번 턴에 실제로 호출된 도구 집계. 메모리 프리페치는 도구 목록에는
            # 없지만(_prefetch_memory가 매 턴 자동으로 하는 것) 실제 순환 경로의
            # 일부이므로 같이 센다 — 매 턴 정확히 1회 호출된다.
            for tc in (result.get("trace") or {}).get("tool_calls", []):
                name = tc.get("tool")
                if name in tool_calls:
                    tool_calls[name] += 1
            tool_calls["memory_prefetch"] += 1
            save_tool_stats()

            print(f"  [챗봇/{kind}] Q: {question[:50]}")
            print(f"             A: {answer[:80]}{tag}")

    final = runtime.memory.stats()
    print("\n===== 종료 =====")
    print(f"사람 채팅 신규 사실 {totals['team_facts']}건 / "
          f"챗봇 질문 {totals['chatbot_turns']}건 / 이번 실행 승격 {totals['promoted']}건")
    print("[도구 호출 횟수]", json.dumps(tool_calls, ensure_ascii=False))
    print("[최종 계층 상태]", json.dumps(final, ensure_ascii=False))


if __name__ == "__main__":
    main()

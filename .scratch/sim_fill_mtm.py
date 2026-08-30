"""MTM이 100건 찰 때까지 대화 시뮬레이션을 반복한다 (조회수는 실제로 쌓는다).

    1) LLM으로 팀 채팅 대화 배치를 주제별로 생성, SessionRecorder로 STM에 적재
       (실제 사실 추출, tier=stm, date=오늘 — 진입 게이트 그대로 통과)
    2) 같은 주제를 반복해서 "재질문" -> memory.search()로 실제 임베딩 검색 ->
       답변 텍스트 생성 -> attribute()로 실제 코사인 계산 -> memory.credit()으로
       실제 조회수 누적. 임의로 숫자를 주입하지 않는다.
    3) memory_promote.run()을 실행 — 조회수 임계(3.0) 넘긴 사실은 창과 무관하게
       즉시 승격된다. STM 최신성 창(7일) 판정용 "현재 시각"은 실제 시계가 아니라
       시뮬레이션 시계를 쓴다: STM에 사실이 100건 쌓일 때마다 5시간씩 흐른다.
       실제 대화라면 그만큼 대화량이 쌓이는 데 걸릴 시간이라고 보는 셈이다. 이게
       없으면(실제 시계 기준) 7일이 지나는 사실이 하나도 없어 조회수 미달 사실이
       STM에 영원히 쌓이기만 하고 폐기되지 않는다.
    4) MTM 100건이 될 때까지 반복
"""
import json, re, sys
sys.path.insert(0, r"D:\LAB_RAG\Org-AI-Body")
import os
os.chdir(r"D:\LAB_RAG\Org-AI-Body")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

from org_agent_mvp.config import AppConfig
from org_agent_mvp.fact_extractor import FactExtractor
from org_agent_mvp.fact_memory import FactMemory
from org_agent_mvp.openrouter_client import OpenRouterClient
from org_agent_mvp.session_recorder import SessionRecorder
from org_agent_mvp.attribution import attribute
from org_agent_mvp import memory_promote
from datetime import datetime, timedelta

# ---------------------------------------------------------------- 보관용 아카이브
#
# facts.sqlite3는 실제 서비스 상태라 승격/중복정리/폐기로 지워지는 레코드가 있다.
# 이 시뮬레이션이 만든 것은 지워진 것까지 전부 남겨야 하므로, FactMemory 클래스
# 메서드를 감싸서 삭제 직전 상태를 별도 JSONL에 이어붙인다.
#
# 클래스 자체를 패치하는 이유: memory_promote.run()이 매 라운드 자기 자신의
# FactMemory 인스턴스를 새로 만들기 때문에, 이 스크립트가 들고 있는 인스턴스만
# 감싸서는 승격/폐기 이벤트를 못 잡는다. 클래스를 패치하면 어느 인스턴스로
# 호출되든 잡힌다.
ARCHIVE_PATH = "data/sim_archive/facts_archive.jsonl"


def _archive(event: str, facts: list, **extra) -> None:
    os.makedirs(os.path.dirname(ARCHIVE_PATH), exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    with open(ARCHIVE_PATH, "a", encoding="utf-8") as f:
        for fc in facts:
            row = {
                "event": event, "archived_at": ts,
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
        before = _fetch_facts(self, fact_ids)  # 승격 직전 상태(옛 tier·조회수)
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


TARGET_MTM = 100
BATCH_CONVOS = 6           # 라운드마다 새로 만드는 대화 세션 수
RECALLS_PER_ROUND = 25     # 라운드마다 기존 STM 사실을 재질문해서 조회수를 쌓는 횟수
MAX_ROUNDS = 60

# STM에 사실이 이만큼 새로 쌓일 때마다 시뮬레이션 시계가 이만큼 흐른다.
# (재시작해도 이어지도록 파일에 누적 카운터를 둔다.)
STM_FACTS_PER_TICK = 100
HOURS_PER_TICK = 5
COUNTER_FILE = ".scratch/stm_fact_counter.txt"


def _load_counter() -> int:
    try:
        return int(open(COUNTER_FILE, encoding="utf-8").read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _save_counter(n: int) -> None:
    with open(COUNTER_FILE, "w", encoding="utf-8") as f:
        f.write(str(n))


def sim_now(total_stm_created: int) -> datetime:
    ticks = total_stm_created // STM_FACTS_PER_TICK
    return datetime.now() + timedelta(hours=ticks * HOURS_PER_TICK)

TOPICS = [
    "예산 집행 및 이월", "인건비 정산", "정량목표 달성률", "회의 일정 조율",
    "담당자 변경", "협약변경 절차", "시범서비스 설문 결과", "장애물 인식 성능 개선",
    "제품 펌웨어 업데이트", "출장 및 외부 미팅", "보고서 작성 마감", "데이터 라벨링 진행",
    "테스트베드 운영", "특허 출원 검토", "산학협력 계약", "인력 채용 진행상황",
    "품질 이슈 대응", "고객 피드백 정리", "예산 정산 감사 대비", "차년도 계획 수립",
    "센서 캘리브레이션", "배터리 수명 테스트", "앱 UI 개선", "음성 안내 콘텐츠 제작",
    "협력기관 미팅", "성과 지표 보고", "인프라 서버 점검", "보안 점검 결과",
    "사용자 교육 자료", "현장 실증 일정",
]

GEN_PROMPT = """다음은 한 조직에서 실제로 오갈 법한 업무 팀 채팅 대화다.
주제: {topic}

짧은 대화 3~5개를 만들어라. 각 대화는 사용자 발화 1~2개 + 어시스턴트(동료) 응답 1개로 구성.
구체적인 수치, 날짜, 담당자 이름(가상), 결정 사항을 하나씩 포함해라. 실제 공식 문서에는
없는, 내부 채팅에서만 오갈 법한 세부사항으로 만들어라.

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


def gen_batch(client, topic):
    msg = client.chat(
        [{"role": "system", "content": "너는 조직 내부 업무 대화를 생성하는 데이터 생성기다."},
         {"role": "user", "content": GEN_PROMPT.format(topic=topic)}],
        tools=[],
    )
    data = _parse_json_block(msg.get("content")) or {}
    out = []
    for c in data.get("conversations", []):
        q, a = str(c.get("question", "")).strip(), str(c.get("answer", "")).strip()
        if q and a:
            out.append((q, a))
    return out


def recall_and_credit(memory, embed_fn, fact) -> float:
    """기존 사실 하나를 다시 물어보는 상황을 흉내내, 실제 검색+귀속으로 조회수를 쌓는다.

    재질문 = 그 사실 문장 자체를 질의로 던진다(그 사실을 다시 찾는 대화 흐름 재현).
    답변은 사실 텍스트를 그대로 인용하는 형태로 둔다 — LLM 답변 생성을 매번 부르면
    비용이 커지고, 여기서 검증하려는 것은 "검색 -> 귀속 -> credit" 배관이지 문장
    생성 품질이 아니다.
    """
    hits = memory.search(fact.text, tier="stm", top_k=5)
    if not hits:
        return 0.0
    answer = f"확인 결과, {fact.text}"
    cards = [{"record_id": h.id, "source": "memory", "label": "", "text": h.text} for h in hits]
    scores = attribute(answer, cards, embed_fn=embed_fn)
    credited = 0.0
    for sc in scores:
        if sc.influence > 0:
            memory.credit(sc.record_id, sc.influence)
            if sc.record_id == fact.id:
                credited = sc.influence
    return credited


def main():
    install_archive_hooks()

    config = AppConfig.load()
    client = OpenRouterClient(config)
    memory = FactMemory(config.memory_root)
    extractor = FactExtractor(client)
    embed_fn = memory.embed_fn()

    # 아카이브 설치 이전(이번 세션 초반)에 이미 만들어진 사실은 훅이 못 잡았으니
    # 지금 남아있는 것을 한 번 스냅샷으로 채워 넣는다. 이미 지워진 것까지 되살릴
    # 수는 없지만, 최소한 지금 존재하는 것만은 누락 없이 보관한다.
    existing = memory.all_facts()
    if existing and not os.path.exists(ARCHIVE_PATH):
        _archive("backfill_existing", existing)
        print(f"  [아카이브] 기존 사실 {len(existing)}건 백필")

    round_no = 0
    total_turns = 0
    total_recalls = 0
    total_stm_created = _load_counter()

    while round_no < MAX_ROUNDS:
        round_no += 1
        stats = memory.stats()
        mtm_n = stats.get("mtm", {}).get("facts", 0)
        stm_n = stats.get("stm", {}).get("facts", 0)
        print(f"\n===== 라운드 {round_no} | MTM {mtm_n}/{TARGET_MTM}  (STM {stm_n}) =====", flush=True)
        if mtm_n >= TARGET_MTM:
            print("목표 달성.")
            break

        # 1) 새 대화 생성 -> STM 적재
        for i in range(BATCH_CONVOS):
            topic = TOPICS[(round_no * BATCH_CONVOS + i) % len(TOPICS)]
            try:
                pairs = gen_batch(client, topic)
            except Exception as exc:
                print(f"  [gen 실패] {topic}: {type(exc).__name__}: {exc}")
                continue
            if not pairs:
                print(f"  [{topic}] 생성 0건")
                continue
            recorder = SessionRecorder(user="sim", memory=memory, extractor=extractor)
            added_total = 0
            for q, a in pairs:
                r = recorder.record_turn(q, a)
                added_total += r["facts_added"]
            total_turns += len(pairs)
            total_stm_created += added_total
            _save_counter(total_stm_created)
            print(f"  [{topic}] 대화 {len(pairs)}개 -> 사실 {added_total}개 (세션 {recorder.session_id})")

        now = sim_now(total_stm_created)
        print(f"  누적 STM 생성 {total_stm_created}건 -> 시뮬레이션 시각 {now.isoformat(timespec='minutes')}"
              f" (실제 대비 +{(now - datetime.now())})")

        # 2) 기존 STM 사실을 반복 재질문해서 조회수를 실제로 쌓는다.
        #    조회수가 이미 높은 것 위주가 아니라, 승격 임계(3.0)에 못 미친 것들에
        #    고르게 재질문 기회를 준다 -> 조회수 낮은 순으로 우선.
        stm_facts = sorted(memory.all_facts("stm"), key=lambda f: f.views)
        n_recall = min(RECALLS_PER_ROUND, len(stm_facts))
        credited_n = 0
        for f in stm_facts[:n_recall]:
            c = recall_and_credit(memory, embed_fn, f)
            total_recalls += 1
            if c > 0:
                credited_n += 1
        print(f"  재질문 {n_recall}건 수행 (그중 자기 자신에게 귀속된 건 {credited_n}개)")

        # 3) 승격. 조회수 3.0 넘긴 사실은 시각과 무관하게 즉시 승격되고,
        #    창(7일)을 벗어난 조회수 미달 사실은 폐기된다. "지금이 며칠인지"는
        #    시뮬레이션 시계(위에서 계산한 now)를 쓴다.
        print("\n  -- 승격 실행 --")
        try:
            memory_promote.run(today=now.date().isoformat())
        except Exception as exc:
            print(f"  [승격 실패] {type(exc).__name__}: {exc}")

    final = memory.stats()
    print("\n===== 종료 =====")
    print(f"총 생성 대화 턴: {total_turns} / 총 재질문 횟수: {total_recalls}")
    print("[최종 계층 상태]", json.dumps(final, ensure_ascii=False))


if __name__ == "__main__":
    main()

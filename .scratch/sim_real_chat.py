"""실제 에이전트 파이프라인(질문 -> LLM 응답 -> STM 저장 -> 즉시 승격)으로, 계속
이어지는 챗봇 대화방 하나를 만든다. 조회수를 노려서 특정 사실을 골라 재질문하지
않는다 — 그냥 자연스럽게 대화를 계속 이어가고, 그 흐름 속에서 기존 STM/신규 사실이
번갈아 다시 언급되면서 조회수가 쌓이고, 임계치를 넘긴 것들이 알아서(이번 세션에
agent_runtime.py에 붙여둔 턴 종료 시 즉시 승격 훅으로) MTM으로 올라가게 둔다.

    python .scratch/sim_real_chat.py

한 턴은 둘 중 하나다.
    새 주제   TOPICS 중 하나를 골라 완전히 새로운 질문을 만든다 (신규 STM 사실 생성)
    이어가기  방금 전 대화 몇 개 중 하나를 골라 자연스러운 후속 질문을 만든다
              (관련 있으면 기존 STM 사실이 다시 걸려 조회수가 오른다)

이어가기 비율(FOLLOWUP_RATE)만 조절할 뿐, 어떤 사실이 다시 걸릴지는 실제 검색이
정하게 둔다 — 우리가 사실을 골라서 질문을 역산하지 않는다.

전부 하나의 세션(하나의 대화방)에 쌓인다. 실제로 한 사람이 챗봇과 계속 대화하는
상황을 흉내내는 것이라, 세션을 매번 새로 만들 이유가 없다.
"""
import json, random, sys
sys.path.insert(0, r"D:\LAB_RAG\Org-AI-Body")
import os
os.chdir(r"D:\LAB_RAG\Org-AI-Body")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

from org_agent_mvp.config import AppConfig
from org_agent_mvp.openrouter_client import OpenRouterClient
from org_agent_mvp.__main__ import build_runtime

TARGET_MTM = 200
TURNS_PER_ROUND = 12
MAX_ROUNDS = 100
FOLLOWUP_RATE = 0.45      # 이번 턴이 "이어가기"일 확률
HISTORY_WINDOW = 25       # 이어가기 후보로 볼 최근 대화 개수

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

NEW_TOPIC_PROMPT = """사내 업무 챗봇에게 묻는 상황이다. 주제: {topic}

이 주제에 대해 실제 직원이 물어볼 법한 **자연스러운 구어체 질문 하나**를 만들어라.
구체적인 상황을 가정해라(예: 진행 상황 확인, 수치 문의, 일정 확인, 담당자 문의 등
그때그때 다르게). 짧고 캐주얼하게, 질문 하나만 출력해라. 설명이나 따옴표 없이."""

FOLLOWUP_PROMPT = """방금 이런 대화를 나눴다.
질문: {q}
답변: {a}

이 대화에 이어지는 **자연스러운 후속 질문 하나**를 만들어라. 같은 주제를 더
파고들거나, 관련해서 궁금해질 법한 것을 물어라. 짧고 캐주얼하게, 질문 하나만
출력해라. 설명이나 따옴표 없이."""


def gen_question(client, prompt) -> str:
    msg = client.chat(
        [{"role": "system", "content": "너는 자연스러운 업무 챗봇 질문을 만드는 도우미다."},
         {"role": "user", "content": prompt}],
        tools=[],
    )
    return (msg.get("content") or "").strip().strip('"').strip()


def main():
    config = AppConfig.load()
    qgen_client = OpenRouterClient(config)
    runtime = build_runtime(config, use_mock=False, record=True)
    print(f"[세션] {runtime.recorder.session_id}")

    history = []  # [(question, answer), ...] 최근 대화 (이어가기 후보)
    round_no = 0
    total_asked = 0
    total_promoted = 0

    while round_no < MAX_ROUNDS:
        round_no += 1
        stats = runtime.memory.stats()
        mtm_n = stats.get("mtm", {}).get("facts", 0)
        stm_n = stats.get("stm", {}).get("facts", 0)
        print(f"\n===== 라운드 {round_no} | MTM {mtm_n}/{TARGET_MTM}  (STM {stm_n}) =====", flush=True)
        if mtm_n >= TARGET_MTM:
            print("목표 달성.")
            break

        for _ in range(TURNS_PER_ROUND):
            do_followup = history and random.random() < FOLLOWUP_RATE
            try:
                if do_followup:
                    prev_q, prev_a = random.choice(history[-HISTORY_WINDOW:])
                    question = gen_question(qgen_client, FOLLOWUP_PROMPT.format(q=prev_q, a=prev_a))
                else:
                    topic = random.choice(TOPICS)
                    question = gen_question(qgen_client, NEW_TOPIC_PROMPT.format(topic=topic))
            except Exception as exc:
                print(f"  [질문 생성 실패] {type(exc).__name__}: {exc}")
                continue
            if not question:
                continue

            try:
                result = runtime.run(question)
            except Exception as exc:
                print(f"  [응답 실패] {type(exc).__name__}: {exc}")
                continue

            answer = result.get("answer") or ""
            total_asked += 1
            history.append((question, answer))

            promoted = result.get("promoted")
            tag = ""
            if promoted:
                n = promoted.get("add", 0) + promoted.get("update", 0)
                total_promoted += n
                tag = f" -> 승격 {n}건"
            kind = "이어가기" if do_followup else "새 주제"
            print(f"  [{kind}] Q: {question[:50]}")
            print(f"           A: {answer[:80]}{tag}")

    final = runtime.memory.stats()
    print("\n===== 종료 =====")
    print(f"총 질문 {total_asked}건 / 이번 실행에서 승격 {total_promoted}건")
    print("[최종 계층 상태]", json.dumps(final, ensure_ascii=False))


if __name__ == "__main__":
    main()

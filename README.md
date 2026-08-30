# Org AI Agent MVP

조직 내 회의 기록, 최근 문서, 공식 기준 문서를 근거로 답변하는 작은 에이전트 MVP입니다. Hermes Agent의 ReAct loop와 tool result 재주입 구조를 참고해, `retrieve_memory` 도구와 STM / MTM / LTM 메모리 계층을 실험합니다.

## Core Flow

```text
User question
-> LLM-first ReAct loop
-> retrieve_memory tool call
-> STM / MTM / LTM memory search
-> evidence cards
-> LLM re-check
-> final answer with sources
```

현재 구현은 **prefetch RAG 없이** LLM이 먼저 판단하고 필요한 경우 메모리 도구를 호출하는 구조입니다. 검색은 벡터 DB가 아니라 seed 파일을 대상으로 한 keyword 기반 top-k 검색입니다.

## Quick Start

API 키 없이 구조만 확인하려면 mock 모드로 실행합니다.

```powershell
python -m org_agent_mvp --mock --verbose --trace --question "오늘 회의 결정이 공식 계획서와 충돌해?"
```

OpenRouter를 사용하려면 `.env.example`을 복사해 `.env`를 만들고 `OPENROUTER_API_KEY`를 입력합니다.

```powershell
copy .env.example .env
notepad .env
python -m org_agent_mvp --verbose --trace --question "아까 회의에서 다음 일정 뭐였지?"
```

기본 모델:

```env
OPENROUTER_MODEL=google/gemini-3.7-flash
```

## Useful Options

| Option | Description |
|---|---|
| `--mock` | OpenRouter 호출 없이 deterministic mock LLM 사용 |
| `--verbose` | LLM 판단과 tool 실행 과정을 터미널에 출력 |
| `--trace` | 실행 후 요약 trace JSON 출력 |
| `--save-log` | 한 턴의 실행 로그를 `logs/turns/*.json`에 저장 |
| `--question`, `-q` | 단일 질문 실행 |

로그를 남기는 예시:

```powershell
python -m org_agent_mvp --mock --verbose --trace --save-log --question "A 과제 예산 검토에서 보완해야 할 점이 뭐야?"
```

로그의 핵심 필드:

```text
summary
reasoning_steps
tool_executions
trace
answer
transcript
debug_events
```

`reasoning_steps`는 모델의 내부 chain-of-thought가 아니라, 실제 assistant 응답과 tool call 결과를 바탕으로 만든 관찰 가능한 판단 요약입니다.

## Memory Tiers

| Tier | Purpose | Examples |
|---|---|---|
| STM | 오늘/최근 대화, 회의, action item | "아까 회의에서 다음 일정 뭐였지?" |
| MTM | 최근 한 달 문서, 회의록, 제안서 초안 | "최근 제안서 초안 일정이 뭐야?" |
| LTM | 공식 계획서, 조직 기준, 장기 지식 | "공식 계획서 기준 마일스톤 알려줘" |

## Project Structure

```text
org_agent_mvp/
  org_agent_mvp/
    __main__.py
    agent_runtime.py
    config.py
    memory_store.py
    mock_llm.py
    openrouter_client.py
    prompts.py
    schemas.py
  memory_seed/
    stm/
    mtm/
    ltm/
  .env.example
  .gitignore
  README.md
```

`docs/`, `logs/`, `.env`는 로컬 작업용으로 Git 추적에서 제외합니다.

## Example Questions

```text
아까 회의에서 다음 일정 뭐였지?
오늘 회의 결정이 공식 계획서와 충돌해?
A 과제 예산 검토에서 보완해야 할 점이 뭐야?
B 과제 시범 분석 결과는 언제 공유하기로 했어?
MTM 문서를 LTM으로 승격하는 기준이 뭐야?
A 과제 수정 일정표에서 공식 마일스톤과 내부 목표일이 어떻게 달라?
```

## Next Steps

- Query Analyzer 추가
- Prefetch RAG 추가
- keyword search를 BM25 / vector / hybrid retrieval로 확장
- MTM -> LTM 승격 후보 추천 workflow 구현
- test question set과 turn log 기반 평가 체계 구축

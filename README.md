# 조직지식 에이전트 MVP

Hermes Agent의 구조적 아이디어를 참고해 만든 작은 조직지식 에이전트 MVP입니다.

핵심 흐름:

```text
User Query
→ LLM-first ReAct Loop
→ retrieve_memory tool call
→ STM / MTM / LTM seed memory 검색
→ evidence card 반환
→ LLM 재판단
→ 최종 답변 + 출처
```

## 실행

키 없이 구조만 확인:

```powershell
cd C:\ine_project\중기청_조직지식AI플랫폼\org_agent_mvp
python -m org_agent_mvp --mock --verbose --trace --question "오늘 회의 결정이 공식 계획서와 충돌해?"
```

OpenRouter 연결:

```powershell
cd C:\ine_project\중기청_조직지식AI플랫폼\org_agent_mvp
copy .env.example .env
notepad .env
python -m org_agent_mvp --verbose --trace --question "아까 회의에서 다음 일정 뭐였지?"
```

`.env`의 `OPENROUTER_API_KEY=` 오른쪽에 키를 넣으면 됩니다. 지금은 비워둔 상태입니다.

기본 모델은 한국어 응답 품질을 고려해 Gemma 무료 모델로 고정했습니다.

```env
OPENROUTER_MODEL=google/gemma-4-31b-it:free
```

다른 무료 모델을 쓰고 싶으면 OpenRouter 모델 페이지에서 `:free`가 붙은 모델 ID로 바꾸면 됩니다.

대화형 실행:

```powershell
python -m org_agent_mvp --mock --verbose
```

`--verbose`는 실행 중 LLM 호출, tool call, 검색 결과를 실시간으로 보여줍니다. `--trace`는 실행이 끝난 뒤 JSON trace를 출력합니다.

턴 전체 로그 저장:

```powershell
python -m org_agent_mvp --mock --verbose --trace --save-log --question "A 과제 예산 검토에서 보완해야 할 점이 뭐야?"
```

`--save-log`를 쓰면 `logs/turns/*.json`에 한 턴의 실행 로그가 저장됩니다. 핵심 흐름은 `reasoning_steps`와 `tool_executions`에서 먼저 보고, 전체 원문 transcript와 디버그 이벤트는 `transcript`, `debug_events`에서 확인하면 됩니다.

## 구조

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
  docs/
    code-flow.md
    operation-flow.md
```

코드 기준 호출 흐름은 [docs/code-flow.md](docs/code-flow.md)에 정리되어 있습니다. 실행 흐름이나 tool call 구조를 수정하면 이 문서도 함께 갱신해야 합니다.

## 예시 질문

```text
B 과제 시범 분석 결과는 언제 공유하기로 했어?
A 과제 예산 검토에서 보완해야 할 점이 뭐야?
MTM 문서를 LTM으로 승격하는 기준이 뭐야?
8월 25일 기관 협의 안건은 뭐야?
A 과제 수정 일정표에서 공식 마일스톤과 내부 목표일이 어떻게 달라?
B 과제 리스크 보고에서 가장 큰 위험은 뭐야?
예산 산정 기준에 따르면 외부 자문비는 어떻게 근거를 대야 해?
팀장 메모에서 제안서 초안에 추가하라고 한 내용이 뭐야?
```

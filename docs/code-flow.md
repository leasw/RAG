# 코드 기준 동작 흐름

작성일: 2026-08-19

이 문서는 `org_agent_mvp`가 실제로 어떤 파일의 어떤 함수 순서로 동작하는지 정리한 코드 기준 흐름 문서다.

중요: 이후 실행 흐름, tool call 구조, memory 검색 방식, 로그 저장 방식이 바뀌면 이 문서도 함께 수정해야 한다.

---

## 1. 예시 실행 명령

```powershell
cd C:\ine_project\중기청_조직지식AI플랫폼\org_agent_mvp
python -m org_agent_mvp --mock --verbose --trace --save-log --question "A 과제 예산 검토에서 보완해야 할 점이 뭐야?"
```

이 명령은 다음을 수행한다.

```text
Mock LLM 사용
실시간 ReAct 이벤트 출력
실행 후 trace 출력
턴 전체 JSON 로그 저장
단일 질문 실행
```

OpenRouter를 실제로 호출하려면 `--mock`을 제거한다.

---

## 2. 전체 호출 흐름 요약

```text
python -m org_agent_mvp
→ __main__.py: main()
→ config.py: AppConfig.load()
→ __main__.py: build_runtime()
→ memory_store.py: MemoryStore.__init__() / _load_all()
→ __main__.py: ask_once()
→ agent_runtime.py: AgentRuntime.run()
→ prompts.py: SYSTEM_PROMPT 사용
→ schemas.py: RETRIEVE_MEMORY_TOOL 사용
→ mock_llm.py 또는 openrouter_client.py: chat()
→ agent_runtime.py: tool_calls 확인
→ agent_runtime.py: _execute_tool_call()
→ memory_store.py: retrieve()
→ memory_store.py: _score() / _evidence_card()
→ agent_runtime.py: role=tool message 생성
→ mock_llm.py 또는 openrouter_client.py: chat() 재호출
→ agent_runtime.py: final answer 반환
→ agent_runtime.py: TurnLogger.write()
→ __main__.py: 답변 / trace / log path 출력
```

한 줄로 정리하면:

```text
__main__.py가 실행면,
agent_runtime.py가 Hermes식 ReAct loop,
memory_store.py가 retrieve_memory tool의 실제 검색기,
openrouter_client.py가 실제 LLM 호출부,
TurnLogger가 한 턴의 transcript 저장소 역할이다.
```

---

## 3. 실행 진입점

파일:

```text
org_agent_mvp/__main__.py
```

진입 코드:

```python
if __name__ == "__main__":
    raise SystemExit(main())
```

흐름:

```text
__main__.py
→ main()
→ build_parser()
→ AppConfig.load()
→ build_runtime()
→ ask_once() 또는 interactive()
```

---

## 4. CLI 인자 파싱

파일:

```text
org_agent_mvp/__main__.py
```

함수:

```python
build_parser()
```

지원 옵션:

| 옵션 | 역할 |
|---|---|
| `--question`, `-q` | 단일 질문 실행 |
| `--mock` | OpenRouter 대신 deterministic mock LLM 사용 |
| `--trace` | 실행 후 요약 trace JSON 출력 |
| `--verbose`, `-v` | 실행 중 LLM/tool 이벤트 실시간 출력 |
| `--save-log` | 한 턴 전체 JSON 로그 저장 |
| `--log-dir` | 로그 저장 디렉터리 지정 |

---

## 5. 설정 로드

파일:

```text
org_agent_mvp/config.py
```

함수:

```python
AppConfig.load()
```

동작:

```text
.env 파일 읽기
→ OPENROUTER_API_KEY 읽기
→ OPENROUTER_MODEL 읽기
→ OPENROUTER_BASE_URL 읽기
→ memory_root 설정
→ max_tool_calls 설정
```

현재 기본 모델:

```text
google/gemma-4-31b-it:free
```

메모리 루트:

```text
org_agent_mvp/memory_seed
```

---

## 6. 런타임 생성

파일:

```text
org_agent_mvp/__main__.py
```

함수:

```python
build_runtime(config, use_mock)
```

내부 생성 객체:

```python
client = MockLLMClient() if use_mock else OpenRouterClient(config)
memory_store = MemoryStore(config.memory_root)
return AgentRuntime(config=config, client=client, memory_store=memory_store)
```

의미:

```text
--mock 있음 → MockLLMClient 사용
--mock 없음 → OpenRouterClient 사용
```

---

## 7. Seed memory 로드

파일:

```text
org_agent_mvp/memory_store.py
```

클래스:

```python
MemoryStore
```

초기화:

```python
MemoryStore(config.memory_root)
```

내부 흐름:

```text
MemoryStore.__init__()
→ self._load_all()
→ stm / mtm / ltm 폴더 순회
→ .md / .json / .txt 파일 로드
→ _load_document()
→ JSON 또는 Markdown frontmatter 파싱
→ MemoryDocument 객체 목록 생성
```

대상 폴더:

```text
memory_seed/stm/
memory_seed/mtm/
memory_seed/ltm/
```

---

## 8. 질문 실행

파일:

```text
org_agent_mvp/__main__.py
```

함수:

```python
ask_once(runtime, question, show_trace, verbose, log_dir)
```

핵심 호출:

```python
result = runtime.run(
    question,
    event_callback=print_event if verbose else None,
    log_dir=log_dir,
)
```

의미:

```text
--verbose 있음 → print_event()가 실시간 이벤트 출력
--save-log 있음 → log_dir이 설정되어 JSON 로그 저장
```

---

## 9. AgentRuntime 시작

파일:

```text
org_agent_mvp/agent_runtime.py
```

함수:

```python
AgentRuntime.run()
```

초기 messages:

```python
messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content": user_query},
]
```

시스템 프롬프트 위치:

```text
org_agent_mvp/prompts.py
```

상수:

```python
SYSTEM_PROMPT
```

프롬프트 주요 정책:

```text
아까 / 오늘 / 방금 질문은 STM 우선
최근 문서 / 초안 질문은 MTM 우선
공식 기준 / 최종 문서는 LTM 우선
필요하면 retrieve_memory tool 사용
근거 부족 시 부족하다고 말하기
```

---

## 10. TurnLogger 시작

파일:

```text
org_agent_mvp/agent_runtime.py
```

클래스:

```python
TurnLogger
```

생성 조건:

```text
--save-log 사용 시 생성
```

생성 코드:

```python
turn_logger = TurnLogger(log_dir) if log_dir else None
```

첫 이벤트:

```python
self._emit(event_callback, turn_logger, "turn_start", {"query": user_query})
```

verbose 출력:

```text
[turn] 사용자 질문: ...
```

---

## 11. LLM 호출 루프

파일:

```text
org_agent_mvp/agent_runtime.py
```

함수:

```python
AgentRuntime.run()
```

핵심 loop:

```python
for _ in range(self.config.max_tool_calls + 1):
    trace.llm_calls += 1
    assistant_message = self.client.chat(messages, self.tools)
```

여기서 `self.tools`는 다음 tool schema를 담는다.

파일:

```text
org_agent_mvp/schemas.py
```

상수:

```python
RETRIEVE_MEMORY_TOOL
```

LLM 호출 시 전달되는 주요 요소:

```text
system prompt
user query
이전 assistant/tool messages
retrieve_memory tool schema
```

---

## 12. LLM 클라이언트

Mock 모드:

```text
org_agent_mvp/mock_llm.py
```

함수:

```python
MockLLMClient.chat()
```

실제 OpenRouter 호출:

```text
org_agent_mvp/openrouter_client.py
```

함수:

```python
OpenRouterClient.chat()
```

OpenRouter 요청:

```text
POST {OPENROUTER_BASE_URL}/chat/completions
```

payload 개념:

```json
{
  "model": "google/gemma-4-31b-it:free",
  "messages": [],
  "tools": [],
  "tool_choice": "auto",
  "temperature": 0.2
}
```

---

## 13. LLM이 tool_call을 반환한 경우

파일:

```text
org_agent_mvp/agent_runtime.py
```

함수:

```python
AgentRuntime.run()
```

tool call 확인:

```python
tool_calls = assistant_message.get("tool_calls") or []
```

tool call이 있으면:

```python
messages.append(assistant_message)
result_message = self._execute_tool_call(...)
messages.append(result_message)
```

이 구조가 ReAct loop다.

```text
LLM 판단
→ assistant tool_call
→ tool 실행
→ role=tool result
→ LLM 재호출
```

---

## 14. Tool 실행

파일:

```text
org_agent_mvp/agent_runtime.py
```

함수:

```python
_execute_tool_call()
```

동작:

```python
function = tool_call.get("function", {})
name = function.get("name")
args = json.loads(function.get("arguments") or "{}")
```

예시 tool call:

```json
{
  "name": "retrieve_memory",
  "arguments": {
    "tier": "mtm",
    "query": "A 과제 예산 검토에서 보완해야 할 점",
    "filters": {
      "project": "A 과제"
    },
    "top_k": 5,
    "reason": "최근 예산 검토 근거가 필요함"
  }
}
```

꺼내는 값:

```text
tier
query
filters
top_k
reason
```

---

## 15. MemoryStore 검색

파일:

```text
org_agent_mvp/memory_store.py
```

함수:

```python
MemoryStore.retrieve()
```

호출:

```python
result = self.memory_store.retrieve(
    tier=tier,
    query=query,
    filters=args.get("filters") or {},
    top_k=int(args.get("top_k") or self.config.default_top_k),
)
```

내부 흐름:

```text
retrieve()
→ tier에 맞는 문서 후보 선택
→ _matches_filters()
→ _score()
→ 점수순 정렬
→ _evidence_card()
→ results 반환
```

---

## 16. Query expansion

파일:

```text
org_agent_mvp/memory_store.py
```

함수:

```python
_expand_query()
```

무료 모델이 영어 query를 만들 수 있어서, 기본 한영 확장을 수행한다.

예:

```text
earlier → 아까 오늘 최근
meeting → 회의 회의록 대화
next → 다음 차기 후속
steps → 일정 action item 담당자 할일
official → 공식 최종 승인 기준
```

따라서 모델이 다음처럼 tool query를 만들어도:

```text
next steps from meeting earlier today
```

한국어 seed memory에서 관련 STM 문서를 찾을 수 있다.

---

## 17. Evidence card 생성

파일:

```text
org_agent_mvp/memory_store.py
```

함수:

```python
_evidence_card()
```

검색 결과를 다음 형태로 정규화한다.

```json
{
  "evidence_id": "ev_mtm_2026-08-18-a-project-budget-review",
  "tier": "MTM",
  "source_type": "budget_review",
  "title": "2026-08-18 A 과제 예산 검토 메모",
  "summary": "...",
  "quote": "...",
  "source_ref": {
    "document_id": "2026-08-18-a-project-budget-review.md",
    "path": "mtm/2026-08-18-a-project-budget-review.md"
  },
  "confidence": 0.82
}
```

---

## 18. Tool result message 생성

파일:

```text
org_agent_mvp/agent_runtime.py
```

함수:

```python
_execute_tool_call()
```

반환 message:

```python
return {
    "role": "tool",
    "tool_call_id": tool_call_id,
    "name": "retrieve_memory",
    "content": json.dumps(result, ensure_ascii=False),
}
```

이 message가 다시 `messages`에 추가된다.

다음 LLM 호출은 다음 구조를 보게 된다.

```text
system
user
assistant with tool_calls
tool with retrieve_memory result
```

---

## 19. LLM 재호출

파일:

```text
org_agent_mvp/agent_runtime.py
```

함수:

```python
AgentRuntime.run()
```

tool result를 붙인 뒤 loop 상단으로 돌아간다.

```python
assistant_message = self.client.chat(messages, self.tools)
```

LLM은 두 가지 중 하나를 선택한다.

```text
근거 충분 → 최종 답변
근거 부족 → 추가 tool_call
```

---

## 20. 최종 답변

파일:

```text
org_agent_mvp/agent_runtime.py
```

함수:

```python
AgentRuntime.run()
```

tool call이 없으면 final answer로 본다.

```python
if not tool_calls:
    content = assistant_message.get("content") or ""
    trace.stopped_reason = "final_answer"
    return {
        "answer": content,
        "trace": self._trace_dict(trace),
        "messages": messages + [assistant_message],
    }
```

verbose 출력:

```text
[llm #2] 최종 답변 생성
[final] 답변 준비 완료
```

---

## 21. Trace 생성

파일:

```text
org_agent_mvp/agent_runtime.py
```

함수:

```python
_trace_dict()
```

예시:

```json
{
  "llm_calls": 2,
  "tool_calls": [
    {
      "tool": "retrieve_memory",
      "tier": "all",
      "query": "A 과제 예산 검토에서 보완해야 할 점이 뭐야?",
      "reason": "사용자 질문에 대한 ALL 근거 확인",
      "result_count": 5
    }
  ],
  "final_sources": [
    "2026-08-19-budget-followup.json",
    "2026-08-18-a-project-budget-review.md"
  ],
  "stopped_reason": "final_answer"
}
```

---

## 22. Turn log 저장

파일:

```text
org_agent_mvp/agent_runtime.py
```

클래스 / 함수:

```python
TurnLogger
_write_turn_log()
```

사용 조건:

```text
--save-log
```

저장 위치:

```text
logs/turns/YYYYMMDD-HHMMSS-xxxxxxxx.json
```

로그에 포함되는 내용:

```text
turn_id
user_query
summary
reasoning_steps
tool_executions
trace
answer
transcript
debug_events
```

`reasoning_steps` 예시:

```text
step 1: decision=tool_call, next_action=execute_tool
step 2: decision=tool_call, next_action=execute_tool
step 3: decision=final_answer, next_action=return_answer
```

`reasoning_steps.reasoning_summary`는 모델 내부 chain-of-thought 원문이 아니다. OpenRouter Chat Completions에서 관찰 가능한 assistant 응답, `tool_calls` 유무, tool 인자의 `reason`, 현재까지 모인 출처 수를 기준으로 런타임이 만든 판단 요약이다.

`debug_events` 예시:

```text
turn_start
llm_call_start
llm_decision
llm_call_end
tool_call_start
tool_call_end
final_answer
```

`llm_call_start` 이벤트에는 로그 중복을 줄이기 위해 `transcript` 전체가 아니라 `message_roles`만 저장한다. 한 턴의 실제 전체 transcript는 최상위 `transcript`에 한 번 저장된다.

`transcript` 예시:

```text
system
user
assistant with tool_calls
tool with retrieve_memory result
assistant final answer
```

---

## 23. Hermes와의 대응 관계

이 MVP는 Hermes 코드를 직접 복사하지 않고, 구조적 아이디어를 축소 반영했다.

| Hermes 구조 | org_agent_mvp 구조 |
|---|---|
| 실행면 CLI / TUI / Gateway | `__main__.py` CLI |
| `AIAgent.run_conversation()` | `AgentRuntime.run()` |
| `conversation_loop.py` | `AgentRuntime.run()` 내부 loop |
| `tool_executor.py` | `_execute_tool_call()` |
| tool registry / schema | `schemas.py`의 `RETRIEVE_MEMORY_TOOL` |
| `role=tool` result 재주입 | `_execute_tool_call()` 반환 message |
| session / message 저장 | `TurnLogger` JSON log |
| memory / context 주입 | `MemoryStore.retrieve()` evidence card |

---

## 24. 현재 구조의 한계

현재 구조는 MVP 검증용이다.

- 검색은 vector DB가 아니라 keyword 기반이다.
- tool call reason은 모델이 만든 문자열이므로 품질이 들쭉날쭉할 수 있다.
- `response_format` 강제 JSON은 사용하지 않는다.
- 무료 모델이 영어 query를 만들 수 있어 query expansion으로 보완한다.
- 실제 개인정보나 기밀 문서는 무료 endpoint에 넣으면 안 된다.
- `logs/turns/*.json`에는 전체 메시지와 tool result가 저장되므로 운영 시 접근 권한과 보존 기간이 필요하다.

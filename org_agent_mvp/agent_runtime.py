from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

from .config import AppConfig
from .fact_memory import FactMemory
from .prompts import SYSTEM_PROMPT
from .attribution import attribute
from .doc_rag_tool import DocSearchTool
from .graph_tool import GraphSearchTool
from .ltm_tool import LtmMemoryTool
from .schemas import SEARCH_DOCUMENTS_TOOL, SEARCH_GRAPH_TOOL, SEARCH_LTM_TOOL
from .session_recorder import SessionRecorder
from . import memory_promote


class ChatClient(Protocol):
    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        ...


def _for_replay(assistant_message: dict[str, Any]) -> dict[str, Any]:
    """assistant 응답을 다음 요청에 다시 넣을 수 있는 형태로 줄인다.

    Gemini 3.x는 응답에 `reasoning_details`(그 안에 reasoning.encrypted = thought
    signature)를 실어 보내는데, 이걸 그대로 되돌려보내면 tool 결과가 사이에 끼는 순간
    "Corrupted thought signature" 400이 난다. 서명이 그 시점의 대화 상태에 묶여 있어
    재생이 안 되는 것으로 보인다.

    추론 필드는 다음 턴에 필요한 정보가 아니므로 떼고 OpenAI 호환 최소 형태만 보낸다.
    원본은 trace와 turn log에 그대로 남으므로 디버깅에는 지장이 없다.
    """
    kept = {"role": assistant_message.get("role", "assistant")}
    if assistant_message.get("content") is not None:
        kept["content"] = assistant_message["content"]
    if assistant_message.get("tool_calls"):
        kept["tool_calls"] = assistant_message["tool_calls"]
    return kept


@dataclass
class AgentTrace:
    llm_calls: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    final_sources: list[str] = field(default_factory=list)
    stopped_reason: str = ""
    # 이번 턴에 도구가 돌려준 근거 카드 전부. 최종 답변이 나온 뒤 기여도 계산에 쓴다.
    cards: list[dict[str, Any]] = field(default_factory=list)
    attribution: list[dict[str, Any]] = field(default_factory=list)
    attribution_method: str = ""
    memory_prefetch: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnLogger:
    log_dir: Path
    turn_id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8])
    events: list[dict[str, Any]] = field(default_factory=list)
    reasoning_steps: list[dict[str, Any]] = field(default_factory=list)
    tool_executions: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append(
            {
                "index": len(self.events) + 1,
                "event": event,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "payload": payload,
            }
        )

    def write(self, data: dict[str, Any]) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{self.turn_id}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def record_reasoning_step(self, payload: dict[str, Any]) -> None:
        self.reasoning_steps.append(
            {
                "step": len(self.reasoning_steps) + 1,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                **payload,
            }
        )

    def record_tool_execution(self, payload: dict[str, Any]) -> None:
        self.tool_executions.append(
            {
                "step": len(self.tool_executions) + 1,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                **payload,
            }
        )


class AgentRuntime:
    def __init__(
        self,
        config: AppConfig,
        client: ChatClient,
        memory: FactMemory,
        doc_search: DocSearchTool | None = None,
        graph_search: GraphSearchTool | None = None,
        recorder: SessionRecorder | None = None,
    ):
        self.config = config
        self.client = client
        self.memory = memory
        self.doc_search = doc_search if doc_search is not None else DocSearchTool()
        self.graph_search = graph_search if graph_search is not None else GraphSearchTool()
        self.ltm_search = LtmMemoryTool(memory)
        # 이번 대화를 STM으로 남기는 기록기. None이면 기록하지 않는다(테스트/평가용).
        self.recorder = recorder
        # 도구는 셋. 작업 메모리(STM/MTM)는 도구가 아니라 매 턴 반드시 경유하는
        # 전처리 파이프라인이라 여기 없다 — _prefetch_memory()가 담당한다. LTM은
        # 이미 검증된 안정 지식이라 STM/MTM과 달리 매 턴 볼 필요가 없어서 도구로
        # 뺐다 — search_ltm_memory가 담당한다.
        self.tools = [SEARCH_DOCUMENTS_TOOL, SEARCH_GRAPH_TOOL, SEARCH_LTM_TOOL]
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "search_documents": self.doc_search.run,
            "search_graph": self.graph_search.run,
            "search_ltm_memory": self.ltm_search.run,
        }
        self._embed_fn: Callable[[Any], Any] | None = None
        self._embed_tried = False

    def _embedder(self):
        """기여도 계산용 임베딩 함수. 없으면 None (텍스트 귀속도로만 계산).

        search_documents가 이미 올려둔 임베더가 있으면 그걸 재사용한다. 같은 모델을
        두 번 GPU에 올릴 이유가 없다.
        """
        if self._embed_tried:
            return self._embed_fn
        self._embed_tried = True

        retriever = getattr(self.doc_search, "_retriever", None)
        embedder = getattr(retriever, "embedder", None)
        if embedder is None:
            try:
                from doc_rag.config import load_config
                from doc_rag.embedding_factory import build_embedder

                embedder = build_embedder(load_config())
            except Exception:  # noqa: BLE001 - 임베딩이 없어도 계산은 되어야 한다
                return None
        self._embed_fn = lambda texts: embedder.embed(list(texts))
        return self._embed_fn

    def _prefetch_memory(self, user_query: str) -> list:
        """STM/MTM 사실 조회. 도구가 아니라 매 턴 무조건 지나가는 파이프라인이다.

        메모리는 "지금 굴러가는 맥락"이라 모델이 필요 여부를 판단할 대상이 아니다.
        도구로 두면 실측상 절반의 턴에서 건너뛰어졌고, 그 결과 "아까 얘기한 그거"류
        질문에서 맥락이 빠진 채 답이 나갔다. 그래서 판단을 없애고 항상 넣는다.

        tier는 항상 all이다 — STM/MTM 중 어디를 볼지도 모델이 정할 일이 아니다.
        다만 "all"이 두 계층을 한 풀에 놓고 자르는 게 아니라 계층마다 따로
        config.memory_tier_budget개씩 뽑아 합치는 것이므로(fact_memory.search 참고),
        갓 승격된 MTM 사실이 STM 물량에 밀려 안 보이는 일이 없다.

        LTM은 여기 없다 — search_ltm_memory 도구로 뺐다(__init__ 참고).
        """
        return self.memory.search(user_query, tier="all",
                                   tier_budget=self.config.memory_tier_budget)

    @staticmethod
    def _render_memory(facts: list) -> str:
        if not facts:
            return "[작업 메모리 STM/MTM] 이번 질문과 관련된 기억이 없습니다."
        lines = ["[작업 메모리 STM/MTM — 자동 조회, 도구 호출 아님]",
                 "아래는 이전 대화에서 뽑아 둔 사실 문장이다. 원문 대화가 아니다.",
                 "검증까지 끝난 확정 지식(LTM)이 더 필요하면 search_ltm_memory를 써라."]
        for f in facts:
            score = (f.meta or {}).get("score", "")
            lines.append(f"- ({f.tier.upper()}, {f.date}, sim={score}) {f.text}")
        return "\n".join(lines)

    def _recent_turns_messages(self) -> list[dict[str, Any]]:
        """이번 세션에서 실제로 오간 원문 대화를 최근 것부터 최대
        config.recent_turns_char_budget자까지 실제 user/assistant 메시지로
        되돌려준다. Letta의 recall 큐(FIFO)와 같은 역할 — STM/MTM 사실 문장으로
        요약되지 않은 원문 맥락(말투, 직전 전개)을 이걸로 메운다.

        recorder.turns는 SessionRecorder가 이번 프로세스 안에서 오간 턴을
        (hhmm, role, text)로 계속 쌓아둔 것이다. 이번 턴(user_query)은 아직
        여기 없다 — record_to_stm()이 답변까지 나온 뒤에 붙이기 때문에, run()
        시작 시점에는 항상 "이전" 턴들만 들어있다.

        recorder가 없으면(테스트/평가용 등) 빈 리스트를 준다 — 원문 맥락 없이도
        동작은 되어야 한다.
        """
        if self.recorder is None or not self.recorder.turns:
            return []
        budget = self.config.recent_turns_char_budget
        picked: list[dict[str, Any]] = []
        total = 0
        for _hhmm, role, text in reversed(self.recorder.turns):
            total += len(text)
            if total > budget and picked:
                break
            picked.append({"role": role, "content": text})
        picked.reverse()
        return picked

    def run(
        self,
        user_query: str,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
        log_dir: Path | None = None,
        defer_post: bool = False,
    ) -> dict[str, Any]:
        """defer_post=True면 답변이 나온 뒤에 하는 STM 기록·승격 확인을 바로 하지
        않고 result["finish"]에 콜백으로 담아 돌려준다. 답변 자체는 LLM 추론
        루프만 끝나면 나오는데, STM 기록(FactExtractor가 LLM을 한 번 더 부름)과
        승격 확인(_check_promotion, 리랭커 첫 호출 시 모델 로드)이 뒤에 동기로
        붙어 있어서 CLI에서는 티가 안 나지만, HTTP 응답을 그 시점까지 붙들면
        답변이 이미 준비됐는데도 사용자가 계속 기다리게 된다. 웹 서버 쪽에서
        답변을 먼저 돌려주고 result["finish"]()를 백그라운드 스레드로 나중에
        불러 마무리하도록 이 옵션을 둔다. CLI(__main__.py)는 기존처럼
        defer_post 없이 불러 동작이 그대로다.
        """
        trace = AgentTrace()
        turn_logger = TurnLogger(log_dir) if log_dir else None
        seen_tier_queries: set[tuple[str, str, str]] = set()

        self._emit(event_callback, turn_logger, "turn_start", {"query": user_query})

        facts = self._prefetch_memory(user_query)
        trace.memory_prefetch = {
            "tier": "all",
            "result_count": len(facts),
            "facts": [{"id": f.id, "tier": f.tier, "text": f.text,
                       "score": (f.meta or {}).get("score")} for f in facts],
        }
        for f in facts:
            trace.cards.append({
                "tool": "memory_pipeline",
                "record_id": f.id,
                "title": f.text[:60],
                "content": f.text,
                "source_ref": {"document_id": f.source_id, "path": f"{f.tier}/{f.id}"},
            })
        self._emit(event_callback, turn_logger, "memory_prefetch", trace.memory_prefetch)

        recent_turns = self._recent_turns_messages()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._render_memory(facts)},
        ]
        if recent_turns:
            messages.append({
                "role": "user",
                "content": "[최근 원문 대화 — 오래된 쪽부터, 문자 수 상한을 넘기면 앞부분이 잘림]",
            })
            messages.extend(recent_turns)
        messages.append({"role": "user", "content": user_query})
        for _ in range(self.config.max_tool_calls + 1):
            trace.llm_calls += 1
            message_roles = [message.get("role", "") for message in messages]
            self._emit(
                event_callback,
                turn_logger,
                "llm_call_start",
                {
                    "llm_call": trace.llm_calls,
                    "message_count": len(messages),
                    "tool_count": len(self.tools),
                    "message_roles": message_roles,
                },
            )
            assistant_message = self.client.chat(messages, self.tools)
            tool_calls = assistant_message.get("tool_calls") or []
            decision_payload = self._build_decision_payload(
                llm_call=trace.llm_calls,
                decision="tool_call" if tool_calls else "final_answer",
                message_roles=message_roles,
                assistant_message=assistant_message,
                source_count=len(trace.final_sources),
            )
            if turn_logger:
                turn_logger.record_reasoning_step(decision_payload)
            self._emit(event_callback, turn_logger, "llm_decision", decision_payload)
            self._emit(
                event_callback,
                turn_logger,
                "llm_call_end",
                {
                    "llm_call": trace.llm_calls,
                    "has_tool_calls": bool(tool_calls),
                    "tool_call_count": len(tool_calls),
                    "content_preview": (assistant_message.get("content") or "")[:160],
                    "assistant_message": assistant_message,
                },
            )

            if not tool_calls:
                content = assistant_message.get("content") or ""
                trace.stopped_reason = "final_answer"
                self._score_attribution(content, trace)
                self._emit(
                    event_callback,
                    turn_logger,
                    "final_answer",
                    {
                        "stopped_reason": trace.stopped_reason,
                        "source_count": len(trace.final_sources),
                    },
                )
                result = {
                    "answer": content,
                    "trace": self._trace_dict(trace),
                    "messages": messages + [assistant_message],
                }
                def finish() -> None:
                    self._record_to_stm(user_query, content, result)
                    self._check_promotion(trace, result)
                    self._write_turn_log(turn_logger, user_query, result)

                if defer_post:
                    result["finish"] = finish
                else:
                    finish()
                return result

            messages.append(_for_replay(assistant_message))
            for tool_call in tool_calls:
                result_message = self._execute_tool_call(
                    tool_call, seen_tier_queries, trace, event_callback, turn_logger
                )
                messages.append(result_message)

            if len(trace.tool_calls) >= self.config.max_tool_calls:
                trace.stopped_reason = "max_tool_calls"
                self._emit(
                    event_callback,
                    turn_logger,
                    "tool_limit_reached",
                    {"max_tool_calls": self.config.max_tool_calls},
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Tool call limit reached. Use the collected evidence to answer. "
                            "If evidence is insufficient, say so clearly."
                        ),
                    }
                )

        trace.stopped_reason = "loop_exhausted"
        self._emit(event_callback, turn_logger, "loop_exhausted", {})
        result = {
            "answer": "도구 호출 제한에 도달했지만 최종 답변을 생성하지 못했습니다.",
            "trace": self._trace_dict(trace),
            "messages": messages,
        }
        self._write_turn_log(turn_logger, user_query, result)
        return result

    def _execute_tool_call(
        self,
        tool_call: dict[str, Any],
        seen_tier_queries: set[tuple[str, str, str]],
        trace: AgentTrace,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
        turn_logger: TurnLogger | None = None,
    ) -> dict[str, Any]:
        function = tool_call.get("function", {})
        name = function.get("name")
        tool_call_id = tool_call.get("id", "unknown_tool_call")
        if name not in self.handlers:
            content = {"error": f"Unsupported tool: {name}"}
            if turn_logger:
                turn_logger.record_tool_execution(
                    {
                        "tool": str(name),
                        "tool_call_id": tool_call_id,
                        "status": "unsupported_tool",
                        "error": content["error"],
                    }
                )
            return {"role": "tool", "tool_call_id": tool_call_id, "content": json.dumps(content)}

        try:
            args = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            content = {"error": f"Invalid JSON arguments: {exc}"}
            if turn_logger:
                turn_logger.record_tool_execution(
                    {
                        "tool": str(name),
                        "tool_call_id": tool_call_id,
                        "status": "invalid_arguments",
                        "error": content["error"],
                    }
                )
            return {"role": "tool", "tool_call_id": tool_call_id, "content": json.dumps(content)}

        # retrieve_memory는 tier로, search_documents는 stage로 범위를 좁힌다.
        tier = str(args.get("tier") or args.get("stage") or "all").lower()
        query = str(args.get("query", "")).strip()
        self._emit(
            event_callback,
            turn_logger,
            "tool_call_start",
            {
                "tool": name,
                "tool_call_id": tool_call_id,
                "raw_tool_call": tool_call,
                "arguments": args,
                "tier": tier,
                "query": query,
                "filters": args.get("filters") or {},
                "top_k": int(args.get("top_k") or self.config.default_top_k),
                "reason": args.get("reason", ""),
            },
        )
        repeat_key = (str(name), tier, query.lower())
        if repeat_key in seen_tier_queries:
            result = {
                "query": query,
                "tier": tier,
                "result_count": 0,
                "results": [],
                "warning": "Repeated tier/query blocked.",
            }
        else:
            seen_tier_queries.add(repeat_key)
            result = self.handlers[name](args)
        self._emit(
            event_callback,
            turn_logger,
            "tool_call_end",
            {
                "tool": name,
                "tool_call_id": tool_call_id,
                "tier": tier,
                "query": query,
                "result_count": result.get("result_count", 0),
                "result": result,
                "sources": [
                    card.get("source_ref", {}).get("document_id", "")
                    for card in result.get("results", [])
                ],
                "warning": result.get("warning", ""),
            },
        )
        sources = [
            card.get("source_ref", {}).get("document_id", "")
            for card in result.get("results", [])
        ]
        if turn_logger:
            turn_logger.record_tool_execution(
                {
                    "tool": name,
                    "tool_call_id": tool_call_id,
                    "status": "completed",
                    "tier": tier,
                    "query": query,
                    "filters": args.get("filters") or {},
                    "top_k": int(args.get("top_k") or self.config.default_top_k),
                    "reason": args.get("reason", ""),
                    "result_count": result.get("result_count", 0),
                    "sources": sources,
                    "warning": result.get("warning", ""),
                }
            )

        trace.tool_calls.append(
            {
                "tool": name,
                "tier": tier,
                "query": query,
                "reason": args.get("reason", ""),
                "result_count": result.get("result_count", 0),
            }
        )
        for card in result.get("results", []):
            source = card.get("source_ref", {}).get("document_id")
            if source and source not in trace.final_sources:
                trace.final_sources.append(source)
            if card.get("record_id"):
                trace.cards.append({"tool": str(name), **card})

        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": json.dumps(result, ensure_ascii=False),
        }

    def _score_attribution(self, answer: str, trace: AgentTrace) -> None:
        """최종 답변에 각 근거가 얼마나 반영됐는지를 실수로 매기고 누적한다.

        조회수(+1)는 검색에 걸렸다는 사실만 남긴다. 여기서 계산하는 influence는 답변
        텍스트 중 그 근거로 설명되는 비중이라, 반환됐지만 무시된 근거를 걸러낸다.
        메모리 계층 카드만 누적한다 — 문서 코퍼스(LTM)는 승격 대상이 아니다.
        """
        if not trace.cards:
            return

        # 같은 기록이 여러 번 반환됐으면 한 장으로 합친다. 중복 카운트를 막는다.
        merged: dict[str, dict[str, Any]] = {}
        for card in trace.cards:
            rid = card["record_id"]
            if rid not in merged:
                merged[rid] = card

        payload = []
        for rid, card in merged.items():
            ref = card.get("source_ref", {})
            payload.append({
                "record_id": rid,
                "source": "memory" if card["tool"] == "memory_pipeline" else "documents",
                "label": card.get("title") or ref.get("document_id", rid),
                "text": card.get("content") or card.get("quote") or card.get("summary", ""),
            })

        scores = attribute(answer, payload, embed_fn=self._embedder())
        trace.attribution_method = scores[0].method if scores else ""
        trace.attribution = [
            {
                "record_id": s.record_id,
                "source": s.source,
                "label": s.label,
                "influence": s.influence,
            }
            for s in sorted(scores, key=lambda x: x.influence, reverse=True)
        ]

        for s in scores:
            if s.source == "memory":
                self.memory.credit(s.record_id, s.influence)

    def _record_to_stm(self, question: str, answer: str, result: dict[str, Any]) -> None:
        """방금 나눈 대화를 STM에 남기고 메모리를 다시 읽는다.

        재적재를 같이 하는 이유는 대화형 모드다. 다음 턴의 메모리 프리페치가 방금
        저장한 기록을 봐야 "아까 그거"가 성립한다.
        """
        if self.recorder is None:
            return
        try:
            info = self.recorder.record_turn(question, answer)
            result["stm_facts_added"] = info["facts_added"]
            result["stm_fact_ids"] = info["fact_ids"]
            result["raw_log_path"] = info["raw"]
        except Exception as exc:  # noqa: BLE001 - 기록 실패가 답변을 막으면 안 된다
            result["stm_record_error"] = f"{type(exc).__name__}: {exc}"

    def _check_promotion(self, trace: AgentTrace, result: dict[str, Any]) -> None:
        """이번 턴이 끝날 때 STM -> MTM 승격 여부를 즉시 확인한다.

        승격 조건(조회수 >= 임계치)을 이번 턴에 건드린 사실만 확인한다 — 이번 턴에
        새로 추출된 사실(방금 막 생겼으니 조회수 0, 대부분 해당 안 됨)과 이번 턴
        답변에 실제로 인용돼 조회수가 오른 기존 사실(대부분 여기서 걸림). 나머지
        STM 전체를 매 턴 훑지 않는다 — memory_promote.promote_touched()가 그렇게
        짜여 있다.

        폐기·MTM 중복 정리는 대화 흐름과 무관한 유지보수라 여기서 하지 않는다.
        memory_promote.run()을 배치로 별도 실행해서 처리한다.
        """
        touched = set(result.get("stm_fact_ids") or [])
        touched.update(
            a["record_id"] for a in trace.attribution if a["source"] == "memory"
        )
        if not touched:
            return
        try:
            counts = memory_promote.promote_touched(self.memory, list(touched))
            if counts["add"] or counts["update"]:
                result["promoted"] = counts
        except Exception as exc:  # noqa: BLE001 - 승격 실패가 답변을 막으면 안 된다
            result["promotion_error"] = f"{type(exc).__name__}: {exc}"

    def _trace_dict(self, trace: AgentTrace) -> dict[str, Any]:
        return {
            "llm_calls": trace.llm_calls,
            "memory_prefetch": trace.memory_prefetch,
            "tool_calls": trace.tool_calls,
            "final_sources": trace.final_sources,
            "attribution": trace.attribution,
            "attribution_method": trace.attribution_method,
            "stopped_reason": trace.stopped_reason,
        }

    def _build_decision_payload(
        self,
        llm_call: int,
        decision: str,
        message_roles: list[str],
        assistant_message: dict[str, Any],
        source_count: int,
    ) -> dict[str, Any]:
        tool_calls = assistant_message.get("tool_calls") or []
        if tool_calls:
            summarized_calls = [self._summarize_tool_call(tool_call) for tool_call in tool_calls]
            reasons = [
                str(call.get("reason", "")).strip()
                for call in summarized_calls
                if str(call.get("reason", "")).strip()
            ]
            return {
                "llm_call": llm_call,
                "decision": decision,
                "reasoning_summary": " / ".join(reasons)
                or "모델이 현재 컨텍스트만으로는 답변 근거가 부족하다고 판단해 메모리 검색을 선택했다.",
                "next_action": "execute_tool",
                "message_roles": message_roles,
                "tool_calls": summarized_calls,
            }

        content = assistant_message.get("content") or ""
        return {
            "llm_call": llm_call,
            "decision": decision,
            "reasoning_summary": "모델이 추가 tool_call 없이 지금까지 모인 컨텍스트와 근거로 최종 답변이 가능하다고 판단했다.",
            "next_action": "return_answer",
            "message_roles": message_roles,
            "source_count_before_answer": source_count,
            "content_preview": str(content)[:220],
        }

    def _summarize_tool_call(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        function = tool_call.get("function", {})
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {"_raw_arguments": raw_args}
        return {
            "tool_call_id": tool_call.get("id", "unknown_tool_call"),
            "tool": function.get("name", ""),
            "tier": args.get("tier", args.get("stage", "")),
            "query": args.get("query", ""),
            "filters": args.get("filters") or {},
            "top_k": args.get("top_k", self.config.default_top_k),
            "reason": args.get("reason", ""),
        }

    def _emit(
        self,
        event_callback: Callable[[str, dict[str, Any]], None] | None,
        turn_logger: TurnLogger | None,
        event: str,
        payload: dict[str, Any],
    ) -> None:
        if turn_logger:
            turn_logger.record(event, payload)
        if event_callback:
            event_callback(event, payload)

    def _write_turn_log(
        self,
        turn_logger: TurnLogger | None,
        user_query: str,
        result: dict[str, Any],
    ) -> None:
        if not turn_logger:
            return
        path = turn_logger.write(
            {
                "turn_id": turn_logger.turn_id,
                "user_query": user_query,
                "summary": {
                    "stopped_reason": result["trace"]["stopped_reason"],
                    "llm_calls": result["trace"]["llm_calls"],
                    "tool_call_count": len(result["trace"]["tool_calls"]),
                    "source_count": len(result["trace"]["final_sources"]),
                },
                "reasoning_steps": turn_logger.reasoning_steps,
                "tool_executions": turn_logger.tool_executions,
                "trace": result["trace"],
                "answer": result["answer"],
                "transcript": result["messages"],
                "debug_events": turn_logger.events,
            }
        )
        result["turn_log_path"] = str(path)

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent_runtime import AgentRuntime
from .config import AppConfig
from .memory_store import MemoryStore
from .mock_llm import MockLLMClient
from .openrouter_client import OpenRouterClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Organization knowledge agent MVP")
    parser.add_argument("--question", "-q", help="Single question to ask.")
    parser.add_argument("--mock", action="store_true", help="Use deterministic mock LLM.")
    parser.add_argument("--trace", action="store_true", help="Print JSON trace.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print live ReAct events.")
    parser.add_argument("--save-log", action="store_true", help="Save full turn log as JSON.")
    parser.add_argument("--log-dir", type=Path, help="Directory for saved turn logs.")
    return parser


def build_runtime(config: AppConfig, use_mock: bool) -> AgentRuntime:
    client = MockLLMClient() if use_mock else OpenRouterClient(config)
    memory_store = MemoryStore(config.memory_root)
    return AgentRuntime(config=config, client=client, memory_store=memory_store)


def print_event(event: str, payload: dict) -> None:
    if event == "turn_start":
        print(f"\n[turn] 사용자 질문: {payload['query']}")
    elif event == "llm_call_start":
        print(
            f"[llm #{payload['llm_call']}] 호출 시작 "
            f"(messages={payload['message_count']}, tools={payload['tool_count']})"
        )
    elif event == "llm_call_end":
        if payload["has_tool_calls"]:
            print(f"[llm #{payload['llm_call']}] tool_call {payload['tool_call_count']}개 요청")
        else:
            print(f"[llm #{payload['llm_call']}] 최종 답변 생성")
    elif event == "llm_decision":
        print(f"[reasoning #{payload['llm_call']}] decision={payload['decision']}")
        print(f"              {payload['reasoning_summary']}")
    elif event == "tool_call_start":
        print(f"[tool] {payload['tool']} 시작")
        print(f"       tier={payload['tier']} top_k={payload['top_k']}")
        print(f"       query={payload['query']}")
        if payload.get("filters"):
            print(f"       filters={json.dumps(payload['filters'], ensure_ascii=False)}")
        if payload.get("reason"):
            print(f"       reason={payload['reason']}")
    elif event == "tool_call_end":
        print(f"[tool] 검색 완료: {payload['result_count']}건")
        for source in payload.get("sources", [])[:5]:
            print(f"       source={source}")
        if payload.get("warning"):
            print(f"       warning={payload['warning']}")
    elif event == "tool_limit_reached":
        print(f"[policy] tool call 제한 도달: {payload['max_tool_calls']}회")
    elif event == "final_answer":
        print(f"[final] 답변 준비 완료 (sources={payload['source_count']})")
    elif event == "loop_exhausted":
        print("[final] loop exhausted")


def ask_once(
    runtime: AgentRuntime,
    question: str,
    show_trace: bool,
    verbose: bool,
    log_dir: Path | None,
) -> None:
    result = runtime.run(
        question,
        event_callback=print_event if verbose else None,
        log_dir=log_dir,
    )
    print("\n[답변]\n")
    print(result["answer"])
    if result.get("turn_log_path"):
        print(f"\n[turn log]\n{result['turn_log_path']}")
    if show_trace:
        print("\n[trace]\n")
        print(json.dumps(result["trace"], ensure_ascii=False, indent=2))


def interactive(
    runtime: AgentRuntime,
    show_trace: bool,
    verbose: bool,
    log_dir: Path | None,
) -> None:
    print("조직지식 에이전트 MVP입니다. 종료하려면 exit 또는 quit를 입력하세요.")
    while True:
        question = input("\n질문> ").strip()
        if question.lower() in {"exit", "quit"}:
            return
        if not question:
            continue
        ask_once(runtime, question, show_trace, verbose, log_dir)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    config = AppConfig.load()
    try:
        runtime = build_runtime(config, use_mock=args.mock)
    except ValueError as exc:
        print(str(exc))
        print("OpenRouter 키를 .env에 넣거나 --mock 옵션으로 구조를 먼저 확인하세요.")
        return 2
    log_dir = None
    if args.save_log:
        log_dir = args.log_dir or (config.project_root / "logs" / "turns")

    if args.question:
        ask_once(runtime, args.question, args.trace, args.verbose, log_dir)
    else:
        interactive(runtime, args.trace, args.verbose, log_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from typing import Any


RETRIEVE_MEMORY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "retrieve_memory",
        "description": (
            "Search the agent's chat-derived memory — the two volatile tiers. "
            "STM holds chat from the last week (team rooms and AI sessions); "
            "MTM holds older chat records that were retrieved often enough to be "
            "promoted, i.e. context that keeps mattering. "
            "Use this for questions about what was just said or decided, what is in "
            "flight, and who owns what. "
            "For official plans, company standards, budgets, evaluation results and "
            "any finalized deliverable, use search_documents instead — that corpus "
            "is the agent's long-term memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": ["stm", "mtm", "all"],
                    "description": (
                        "Memory tier to search. 'stm' = last week's chat, "
                        "'mtm' = older but repeatedly referenced chat, 'all' = both."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "Natural-language search query.",
                },
                "filters": {
                    "type": "object",
                    "description": "Optional metadata filters.",
                    "properties": {
                        "project": {"type": "string"},
                        "date_range": {"type": "string"},
                        "document_types": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "source_type": {"type": "string"},
                        "status": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "Maximum number of evidence cards to return (default 5).",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this retrieval is needed.",
                },
            },
            "required": ["tier", "query", "reason"],
            "additionalProperties": False,
        },
    },
}


SEARCH_LTM_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_ltm_memory",
        "description": (
            "Search chat-derived long-term memory — facts that started as chat "
            "(unlike search_documents, which is the original document corpus) but "
            "passed verification (traceable source + truthful + useful) and were "
            "promoted out of STM/MTM into a stable, confirmed tier. "
            "Use this when the working-memory block (STM/MTM, given automatically "
            "every turn) doesn't have the answer but the question is still about "
            "something that was likely settled and repeatedly referenced in past "
            "conversations — not about the official document corpus itself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query, preferably Korean.",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "Number of facts to return (default 5).",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this LTM lookup is needed.",
                },
            },
            "required": ["query", "reason"],
            "additionalProperties": False,
        },
    },
}


SEARCH_DOCUMENTS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_documents",
        "description": (
            "Vector + BM25 hybrid search over the 지식서비스산업핵심기술개발사업 "
            "project document corpus (사업계획서, 단계/연차 보고서, 평가결과 및 "
            "반영내역서, 회의자료, 협약/정산 서류, 시장·기술 조사자료). "
            "Use this when the question needs facts from the actual project "
            "deliverables — budgets, milestones, evaluation comments, 정량목표, "
            "기관/책임자, 기술 사양 — rather than short-term conversation memory. "
            "Every hit comes back with file path, page and section so the answer "
            "can cite its source."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query, preferably Korean.",
                },
                "stage": {
                    "type": "string",
                    "enum": ["1단계", "2단계", "15_단계 평가 결과", "all"],
                    "description": "Restrict to one project stage folder. Omit or 'all' to search everything.",
                },
                "formats": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["hwp", "pdf", "pptx", "xlsx", "docx", "txt"]},
                    "description": "Restrict to certain source file formats.",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "Number of evidence cards to return (default 5).",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this document search is needed.",
                },
            },
            "required": ["query", "reason"],
            "additionalProperties": False,
        },
    },
}


SEARCH_GRAPH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_graph",
        "description": (
            "Traverse the knowledge graph built over the LTM document corpus. "
            "The graph holds only predefined entity types — Organization, Person, "
            "Project, Task, Document, Product, Metric (정량목표) — and the relations "
            "between them (BELONGS_TO, PARTICIPATES_IN, PART_OF, HAS_VERSION, "
            "HAS_PART, MENTIONS, CO_OCCURS). "
            "Use this for structural questions: who belongs to which organization, "
            "which organizations participate in the project, how a product breaks "
            "down into versions and parts, which 정량목표 a 과업 is measured by, "
            "which entities co-occur and in which documents. "
            "It returns both the relation facts and the source chunks behind them. "
            "For plain 'what does the document say' questions use search_documents "
            "instead — this tool only knows the defined entities."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language question, preferably Korean.",
                },
                "entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Entity names to start from (e.g. 조진수, 주식회사 피씨티, "
                        "Low Vision Smart Glass, 장애물 감지). Omit to auto-detect "
                        "them from the query."
                    ),
                },
                "relation": {
                    "type": "string",
                    "enum": ["BELONGS_TO", "PARTICIPATES_IN", "PART_OF", "HAS_VERSION",
                             "HAS_PART", "CO_OCCURS", "any"],
                    "description": "Restrict traversal to one relation type. 'any' for all.",
                },
                "hops": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2,
                    "description": "How many hops to expand from the seed entities.",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "Number of source chunks to return (default 5).",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this graph lookup is needed.",
                },
            },
            "required": ["query", "reason"],
            "additionalProperties": False,
        },
    },
}

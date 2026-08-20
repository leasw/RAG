from __future__ import annotations

from typing import Any


RETRIEVE_MEMORY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "retrieve_memory",
        "description": (
            "Search organization memory. Use this whenever the user asks about "
            "recent meetings, schedules, action items, documents, official plans, "
            "company standards, project facts, or source-grounded answers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": ["stm", "mtm", "ltm", "all"],
                    "description": "Memory tier to search.",
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
                    "maximum": 10,
                    "description": "Maximum number of evidence cards to return.",
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


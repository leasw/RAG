from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    memory_root: Path
    api_key: str
    model: str
    base_url: str
    app_name: str
    site_url: str
    max_tool_calls: int = 3
    max_same_tier_calls: int = 1
    default_top_k: int = 5
    # 매 턴 자동 프리페치(tier="all")에서 계층별로 따로 뽑을 개수. STM은 수천 건,
    # MTM은 수십 건 수준이라 계층을 안 나누고 하나의 top_k로 자르면 사실상 항상
    # STM만 뽑힌다 — 계층마다 이 개수만큼 독립적으로 뽑아 합친다.
    # fact_memory.FactMemory.search()의 tier_budget 인자로 그대로 넘어간다.
    #
    # LTM은 여기 없다 — LTM은 이미 검증(출처·진실성·유용성)까지 끝난 안정된 지식이라
    # STM/MTM처럼 "지금 굴러가는 맥락"이 아니다. 매 턴 무조건 볼 필요가 없으니
    # search_ltm_memory 도구로 빼서 모델이 필요하다고 판단할 때만 부르게 한다.
    memory_tier_budget: dict = None  # type: ignore[assignment]  # __post_init__에서 채움
    # 이번 세션에서 실제로 오간 원문 대화를 최근 것부터 얼마나 컨텍스트에 그대로
    # 실어 보낼지(문자 수 기준 FIFO). Letta의 recall 큐와 같은 역할이다 — 사실
    # 문장(STM/MTM)은 "기억할 가치가 있다"고 판정된 것만 남지만, 그 판정에 안 걸린
    # 어투·전개·직전 몇 마디 같은 것은 원문이 아니면 아예 사라진다. 그 손실을
    # 메우려고 최근 원문 몇 턴을 별도로 유지한다. 문자 수 상한을 넘기면 오래된
    # 턴부터 밀어낸다.
    recent_turns_char_budget: int = 4000

    def __post_init__(self):
        if self.memory_tier_budget is None:
            object.__setattr__(self, "memory_tier_budget", {"stm": 5, "mtm": 5})

    @classmethod
    def load(cls) -> "AppConfig":
        load_dotenv(PROJECT_ROOT / ".env")
        return cls(
            project_root=PROJECT_ROOT,
            memory_root=PROJECT_ROOT / "memory_seed",
            api_key=os.environ.get("OPENROUTER_API_KEY", "").strip(),
            model=os.environ.get(
                "OPENROUTER_MODEL", "google/gemini-3.7-flash"
            ).strip(),
            base_url=os.environ.get(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ).rstrip("/"),
            app_name=os.environ.get("OPENROUTER_APP_NAME", "Org Agent MVP"),
            site_url=os.environ.get("OPENROUTER_SITE_URL", "http://localhost"),
        )

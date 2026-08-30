"""그래프에 올릴 엔티티 정의.

범위는 LTM(doc_rag 문서 코퍼스)뿐이다. STM/MTM 채팅 기록은 그래프에 넣지 않는다 —
원천이 다르고 수명주기도 달라서, 섞으면 "확정된 조직 구조"와 "지금 논의 중인 것"이
같은 엣지로 보인다.

**닫힌 어휘(closed vocabulary)다.** 여기 정의된 개체만 노드가 된다. 코퍼스에서 자동
발견하지 않는 이유는 정밀도다 — 정규식으로 3자 한글을 인명으로 잡으면 "기술개",
"과정명", "현황표" 같은 것이 사람이 되고, 그래프 전체가 오염된다.

사전의 빈도는 실제 코퍼스(26,586청크, 2,006만 자) 실측값이다. 0건인 표기는 넣지 않았다.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EntityDef:
    key: str                       # 그래프 노드 id
    type: str
    label: str                     # 표시용 이름
    aliases: tuple[str, ...] = ()  # 본문에서 이 표기를 찾으면 이 노드로 귀속
    attrs: dict = field(default_factory=dict)
    # 동음이의어/용어 혼동 방지용. ambiguous에 적힌 표기로 매칭되면 문자열만으로
    # 확정하지 않고, 청크 벡터를 context(맞는 문맥)와 negative(틀린 문맥) 양쪽과
    # 비교해 더 가까운 쪽으로 판정한다. 절대 문턱이 아니라 판별식이라 보정이 필요 없다.
    ambiguous: tuple[str, ...] = ()
    context: str = ""
    negative: str = ""

    @property
    def surfaces(self) -> tuple[str, ...]:
        """본문 탐색에 쓸 표기 전체. 긴 것부터 봐야 부분 일치가 긴 표기를 가리지 않는다."""
        return tuple(sorted({self.label, *self.aliases}, key=len, reverse=True))


# ---------------------------------------------------------------- 기관
# 별칭에 "PCT"는 넣지 않는다. 이 코퍼스는 특허 문서가 많아 PCT가 특허협력조약
# (Patent Cooperation Treaty)을 뜻하는 경우가 섞인다 — 479건 중 상당수가 그렇다.

ORGANIZATIONS = [
    EntityDef("org:가천대산협", "Organization", "가천대학교 산학협력단",
              ("가천대학교", "가천대"), {"role": "주관(1단계)/공동연구개발(2단계)"}),
    EntityDef("org:피씨티", "Organization", "주식회사 피씨티",
              ("㈜피씨티", "피씨티"), {"role": "참여(1단계)/주관(2단계)"}),
    EntityDef("org:산기평", "Organization", "한국산업기술평가관리원",
              ("산기평", "KEIT"), {"role": "전문기관"}),
    EntityDef("org:동국대산협", "Organization", "동국대학교 산학협력단", (), {"role": "선행기술 보유"}),
    EntityDef("org:순천향대산협", "Organization", "순천향대학교 산학협력단"),
    EntityDef("org:경북대산협", "Organization", "경북대학교 산학협력단", (), {"role": "선행기술 보유"}),
    EntityDef("org:그린광학", "Organization", "그린광학", (), {"role": "선행기술 보유"}),
    EntityDef("org:파이특허", "Organization", "파이특허법률사무소", ("파이특허",), {"role": "특허"}),
    EntityDef("org:태창특허", "Organization", "태창특허법률사무소", ("태창특허",), {"role": "특허"}),
    EntityDef("org:제일특허", "Organization", "제일특허법인", ("제일특허",), {"role": "특허"}),
    EntityDef("org:온유특허", "Organization", "온유특허", (), {"role": "특허"}),
    EntityDef("org:토비스랩", "Organization", "토비스랩", (), {"role": "디자인 컨설팅"}),
]

# ---------------------------------------------------------------- 인물
# 코퍼스에 실제로 등장하는 인물만 넣는다. 자동 추출하지 않고 수동 시드로 고정한다
# (회의정리 슬라이드 5의 "초기 시드: 조직도 수동 입력"과 같은 방식).

PEOPLE = [
    EntityDef("person:조진수", "Person", "조진수", (),
              {"org": "org:가천대산협", "role": "연구책임자", "dept": "컴퓨터공학과"}),
    EntityDef("person:정정일", "Person", "정정일", (),
              {"org": "org:피씨티", "role": "총괄책임자"}),
    EntityDef("person:정인철", "Person", "정인철", (), {"org": "org:가천대산협", "role": "연구원"}),
    EntityDef("person:이상웅", "Person", "이상웅", (), {"org": "org:가천대산협", "role": "연구원"}),
    EntityDef("person:임수빈", "Person", "임수빈", (), {"org": "org:가천대산협", "role": "연구원"}),
    EntityDef("person:김갑열", "Person", "김갑열", (), {"org": "org:피씨티", "role": "참여연구원"}),
    EntityDef("person:최동호", "Person", "최동호", (), {"org": "", "role": "전문가활용 자문"}),
]

# ---------------------------------------------------------------- 과제
# 단계·연차는 별도 노드로 두지 않는다. 시간 축과 기능 축이 한 타입에 섞이면 그래프가
# 엉켜서, 단계/연차는 엣지 속성(phase)으로 붙인다.

PROJECTS = [
    EntityDef("project:20012260", "Project",
              "시야확보 주변위험 보조 및 잔존시력의 정보인지 능력 발달을 가능케 하는 "
              "Low Vision Smart Glass 및 서비스 개발",
              ("20012260", "202006380002"),
              {"program": "지식서비스산업핵심기술개발사업(BI연계형)",
               "period": "2020.07.01~2022.12.31"}),
]

# ---------------------------------------------------------------- 과업 (기능 축)

TASKS = [
    EntityDef("task:활동보조", "Task", "비대면 실내외 활동보조"),
    EntityDef("task:시기능훈련", "Task", "시기능 인지 능력 발달 훈련"),
    EntityDef("task:위험감지", "Task", "보행 위험감지 보조"),
    EntityDef("task:장애물감지", "Task", "장애물 감지"),
    EntityDef("task:도우미서비스", "Task", "도우미 서비스"),
    EntityDef("task:시각보정", "Task", "증강현실 시각보정", ("증강현실형 시각보정", "시각보정")),
    EntityDef("task:시범서비스", "Task", "시범서비스"),
]

# ---------------------------------------------------------------- 제품

PRODUCTS = [
    EntityDef(
        "product:lvsg", "Product", "Low Vision Smart Glass",
        ("스마트 글래스", "스마트글래스"), {"level": "product"},
        # "스마트 글래스"는 2,859청크에 나오는데 그중 238청크가 경쟁사 제품을 가리킨다
        # (삼성 Gear VR 릴루미노, Oxsight, OrCam, eSight 등 시장조사 문단).
        ambiguous=("스마트 글래스", "스마트글래스"),
        context="본 과제에서 개발하는 저시력자용 Low Vision Smart Glass 제품과 그 구성, "
                "개발 목표와 시제품 사양",
        negative="OrCam eSight Gear VR 릴루미노 Oxsight 등 국내외 경쟁사 타사 시각보조 "
                 "제품 소개와 시장 현황 조사 및 선행기술 분석",
    ),
    EntityDef("product:light", "Product", "Light 버전", ("Light버전",), {"level": "version"}),
    EntityDef("product:pro", "Product", "Pro 버전", ("Pro버전",), {"level": "version"}),
    EntityDef(
        "product:hmd", "Product", "HMD", (), {"level": "part"},
        # HMD도 마찬가지다. 1,517청크 중 198청크가 타사 HMD 제품 소개다.
        ambiguous=("HMD",),
        context="본 과제 2차년도에 개발하는 HMD 기반 증강현실 영상 출력 시스템",
        negative="타사 HMD 제품 사례와 국외 개발 기술 관련 제품 특징 비교표",
    ),
    EntityDef("product:display", "Product", "디스플레이부", (), {"level": "part"}),
    EntityDef("product:controller", "Product", "컨트롤러부", (), {"level": "part"}),
    EntityDef("product:glasses", "Product", "안경부", (), {"level": "part"}),
    EntityDef("product:mainboard", "Product", "메인보드부", (), {"level": "part"}),
    EntityDef("product:microdisplay", "Product", "마이크로디스플레이", (), {"level": "part"}),
    EntityDef("product:sensor", "Product", "위험감지 센서", (), {"level": "part"}),
]

# ---------------------------------------------------------------- 정량목표

METRICS = [
    EntityDef("metric:신뢰성", "Metric", "스마트 글래스 기능 신뢰성",
              (), {"target": "90%", "year1": "85%"}),
    EntityDef("metric:운용시간", "Metric", "휴대 운용 시간", (), {"target": "4H", "year1": "3H"}),
    EntityDef("metric:센서응답", "Metric", "위험감지 센서 응답 속도",
              (), {"target": "1sec 이내", "year1": "2sec 이내"}),
    EntityDef("metric:알고리즘정확도", "Metric", "위험감지 알고리즘 정확도", (), {"target": "90%"}),
    EntityDef("metric:인식률", "Metric", "장애물 감지 인식률", (), {"target": "90%"}),
    EntityDef("metric:전송정확도", "Metric", "데이터 전송 정확도",
              (), {"target": "90%", "year1": "85%"}),
    EntityDef("metric:동시접속", "Metric", "동시 접속량", (), {"target": "100건/초"}),
]

ENTITIES: list[EntityDef] = (
    ORGANIZATIONS + PEOPLE + PROJECTS + TASKS + PRODUCTS + METRICS
)

ENTITY_TYPES = ("Organization", "Person", "Project", "Task", "Document", "Product", "Metric")


# ---------------------------------------------------------------- 관계
#
# 두 종류를 구분해서 만든다.
#
#   구조 엣지  사람이 정의한 사실. 문서에 명시가 없어도 참이다(소속, 제품 계층 등).
#   파생 엣지  코퍼스에서 계산한 것. MENTIONS는 문서-개체, CO_OCCURS는 개체-개체.
#
# 문장에 서술형으로 들어 있는 관계("피씨티가 2단계 주관기관으로 변경")는 규칙으로
# 잡히지 않는다. 그건 LLM 추출이 필요한 영역이라 이번 범위에서 제외했다.

EDGE_TYPES = (
    "BELONGS_TO",       # Person -> Organization   (구조)
    "PARTICIPATES_IN",  # Organization -> Project  (구조)
    "PART_OF",          # Task -> Project          (구조)
    "HAS_VERSION",      # Product -> Product       (구조)
    "HAS_PART",         # Product -> Product       (구조)
    "MENTIONS",         # Document -> Entity       (파생, weight=언급 수)
    "CO_OCCURS",        # Entity <-> Entity        (파생, weight=동시 등장 청크 수)
)

# 구조 엣지: (from, type, to, attrs)
STRUCTURAL_EDGES: list[tuple[str, str, str, dict]] = [
    ("org:가천대산협", "PARTICIPATES_IN", "project:20012260",
     {"phase": "1단계 주관 / 2단계 공동연구개발"}),
    ("org:피씨티", "PARTICIPATES_IN", "project:20012260",
     {"phase": "1단계 참여 / 2단계 주관"}),
    # 제품 계층
    ("product:lvsg", "HAS_VERSION", "product:light", {"phase": "1차년도"}),
    ("product:lvsg", "HAS_VERSION", "product:pro", {"phase": "2차년도"}),
    *[("product:lvsg", "HAS_PART", p.key, {}) for p in PRODUCTS if p.attrs.get("level") == "part"],
]
# 소속과 과업 귀속은 사전 attrs에서 자동 생성한다(중복 기재를 피하려고).
STRUCTURAL_EDGES += [
    (p.key, "BELONGS_TO", p.attrs["org"], {"role": p.attrs.get("role", "")})
    for p in PEOPLE if p.attrs.get("org")
]
STRUCTURAL_EDGES += [(t.key, "PART_OF", "project:20012260", {}) for t in TASKS]


def by_key() -> dict[str, EntityDef]:
    return {e.key: e for e in ENTITIES}

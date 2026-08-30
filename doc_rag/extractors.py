"""파일 형식별 텍스트 추출.

형식마다 최적 도구가 달라서 경로가 셋으로 갈립니다. 청킹 단계는 모든 경로가
Docling으로 합류하므로 이후는 동일하게 동작합니다.

  1. Docling 네이티브 (pdf/docx/pptx/xlsx/html/md/csv)
     -> DocumentConverter가 레이아웃/표 구조를 이해한 DoclingDocument를 만듭니다.
  2. HWP (한글 5.0 바이너리) — Docling 2.122가 지원하지 않는 형식
     -> pyhwp의 hwp5html(내장 XSLT)로 XHTML을 얻고, lxml로 직접 순회해
        표는 Markdown 표로, 문단은 heading 복원된 텍스트로 만듭니다.
        그 Markdown을 다시 Docling MD 백엔드에 태웁니다.
  3. 평문 (txt)
     -> Markdown으로 감싸 2번과 같은 경로를 탑니다.

HWP 도구 선정 근거는 `doc_rag/README.md`의 "HWP 인제스천 도구 비교" 참고.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

# Docling이 직접 변환하는 확장자 -> 우리가 쓰는 format 키
DOCLING_EXTS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".csv": "txt",
    ".html": "txt",
    ".htm": "txt",
    ".md": "txt",
}
HWP_EXTS = {".hwp": "hwp"}
XLS_EXTS = {".xls": "xlsx"}
TEXT_EXTS = {".txt": "txt"}

SUPPORTED_EXTS = set(DOCLING_EXTS) | set(HWP_EXTS) | set(XLS_EXTS) | set(TEXT_EXTS)

# 텍스트가 없거나(이미지/영상/3D) 압축 사본이라 건너뛰는 확장자.
SKIPPED_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".psd",
    ".mp4", ".mkv", ".avi", ".mov", ".wav", ".mp3",
    ".zip", ".7z", ".rar", ".tar", ".gz",
    ".stl", ".obj", ".stp", ".step", ".123dx", ".ps", ".eps",
    ".bak", ".tmp", ".lnk", ".exe", ".dll",
    ".doc", ".ppt",  # 레거시 OLE 포맷 - 이 코퍼스에는 사실상 없고 파서도 불안정
}


def format_of(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in DOCLING_EXTS:
        return DOCLING_EXTS[ext]
    if ext in HWP_EXTS:
        return HWP_EXTS[ext]
    if ext in XLS_EXTS:
        return XLS_EXTS[ext]
    if ext in TEXT_EXTS:
        return TEXT_EXTS[ext]
    return None


# ---------------------------------------------------------------- heading 복원


# 한글 공문서에서 흔한 개요 번호 패턴 -> Markdown heading 레벨
_HEADING_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"^제\s*\d+\s*[장부편]\s"), 1),
    (re.compile(r"^제\s*\d+\s*[절관]\s"), 2),
    (re.compile(r"^\d+\.\s*\S"), 2),
    (re.compile(r"^\d+\.\d+\.?\s*\S"), 3),
    (re.compile(r"^\d+\.\d+\.\d+\.?\s*\S"), 4),
    (re.compile(r"^[가-하]\.\s*\S"), 3),
    (re.compile(r"^[□■◇◆▣]\s*\S"), 3),
    (re.compile(r"^[○●◎]\s*\S"), 4),
    (re.compile(r"^[(（]\s*\d+\s*[)）]\s*\S"), 4),
]
_MAX_HEADING_LEN = 60

# "2020.07.01. ~ 2022.12.31." 같은 날짜/기간 줄이 번호 패턴에 걸리는 것을 막는다.
_DATE_LIKE = re.compile(r"^\s*\d{4}\s*[.\-/년]")
# 글자가 하나도 없는 줄(숫자/기호만)도 heading이 아니다.
_HAS_WORD = re.compile(r"[가-힣A-Za-z]")


def _as_markdown_heading(line: str) -> str | None:
    """개요 번호로 시작하고 충분히 짧은 줄만 heading으로 승격."""
    if len(line) > _MAX_HEADING_LEN or line.endswith(("다.", "요.", "임.", "함.")):
        return None
    if _DATE_LIKE.match(line) or not _HAS_WORD.search(line):
        return None
    for pattern, level in _HEADING_PATTERNS:
        if pattern.match(line):
            return "#" * min(level, 6) + " " + line
    return None


# ---------------------------------------------------------------- HWP

_XHTML_NS = "{http://www.w3.org/1999/xhtml}"
_NBSP = " "


def _flat_text(el) -> str:
    """엘리먼트 안의 모든 텍스트를 한 줄로 모은다(Markdown 표를 깨지 않도록)."""
    parts = [t.strip() for t in el.itertext() if t and t.strip()]
    text = " ".join(parts).replace(_NBSP, " ")
    return re.sub(r"\s+", " ", text).strip()


def _cell_text(el) -> str:
    return _flat_text(el).replace("|", "\\|")


def _table_to_markdown(table) -> str:
    """<table>을 Markdown 표로. 병합 셀(rowspan/colspan)의 값을 복제하지 않는다.

    Docling 기본 표 직렬화기(TripletTableSerializer / MarkdownTableSerializer)는
    병합 영역을 원본 값으로 채워 넣는다. 한국어 관공서 서식처럼 병합이 많은 표에서는
    같은 문장이 셀마다 반복돼("과제번호, 1 = 20012260. 과제번호, 2 = 20012260. ...")
    임베딩에 들어갈 텍스트가 노이즈로 뒤덮인다. 여기서는 값을 한 번만 쓰고 나머지
    병합 자리는 빈 칸으로 둬서 열 정렬만 유지한다.
    """
    rows: list[list[str]] = []
    for tr in table.iter(f"{_XHTML_NS}tr"):
        # 중첩 표의 <tr>은 바깥 표의 행으로 세지 않는다.
        # (hwp5html은 <tbody> 없이 <table><tr>을 바로 쓰지만, 방어적으로 조상을 훑는다)
        owner = next(
            (a for a in tr.iterancestors() if a.tag == f"{_XHTML_NS}table"), None
        )
        if owner is not table:
            continue
        cells: list[str] = []
        for td in tr:
            if td.tag not in (f"{_XHTML_NS}td", f"{_XHTML_NS}th"):
                continue
            cells.append(_cell_text(td))
            cells.extend([""] * (int(td.get("colspan", 1) or 1) - 1))
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def hwp_to_markdown(path: Path) -> str:
    """HWP 5.0 -> Markdown.

    경로: pyhwp hwp5html(내장 XSLT) -> XHTML -> lxml 직접 순회 -> Markdown

    바이너리 레코드에서 문단 텍스트만 긁는 방식(hwp5.binmodel ParaText)보다 이쪽이
    나은 이유는 표다. 이 코퍼스의 HWP는 사업계획서/반영내역서/연차보고서라서 내용의
    상당 부분이 표 안에 있는데(1단계보고서 하나에 표 50개, 셀 3,728개), 레코드 직독은
    표를 문단 나열로 평탄화해 "항목명"과 "값"의 대응이 사라진다.
    """
    from hwp5.hwp5html import HTMLTransform
    from hwp5.xmlmodel import Hwp5File
    from lxml import etree

    buf = io.BytesIO()
    HTMLTransform().transform_hwp5_to_xhtml(Hwp5File(str(path)), buf)
    tree = etree.fromstring(
        buf.getvalue(), parser=etree.XMLParser(recover=True, huge_tree=True)
    )
    body = tree.find(f"{_XHTML_NS}body")
    if body is None:
        return ""

    blocks: list[str] = []
    nested_tables: set[int] = set()

    for el in body.iter():
        if el.tag == f"{_XHTML_NS}table":
            if id(el) in nested_tables:
                continue
            # 중첩 표는 바깥 표의 셀 텍스트에 이미 포함되므로 따로 내보내지 않는다.
            for inner in el.iter(f"{_XHTML_NS}table"):
                if inner is not el:
                    nested_tables.add(id(inner))
            md = _table_to_markdown(el)
            if md:
                blocks.append(md)
        elif el.tag == f"{_XHTML_NS}p":
            if any(a.tag == f"{_XHTML_NS}table" for a in el.iterancestors()):
                continue  # 표 안 문단은 셀 텍스트로 이미 처리됨
            text = _flat_text(el)
            if text:
                blocks.append(_as_markdown_heading(text) or text)

    return "\n\n".join(blocks)


# ---------------------------------------------------------------- 레거시 XLS


def xls_to_markdown(path: Path) -> str:
    """레거시 .xls(BIFF) -> Markdown 표.

    Docling의 MsExcel 백엔드는 openpyxl 기반이라 OOXML(.xlsx)만 열 수 있고, 구형
    BIFF 파일에서는 "could not load document"로 실패한다. 이 형식만 xlrd로 읽는다.
    """
    import xlrd

    book = xlrd.open_workbook(str(path))
    blocks: list[str] = []
    for sheet in book.sheets():
        if sheet.nrows == 0:
            continue
        rows: list[list[str]] = []
        for r in range(sheet.nrows):
            cells = [str(sheet.cell_value(r, c)).replace("|", "\\|").strip()
                     for c in range(sheet.ncols)]
            if any(cells):
                rows.append(cells)
        if not rows:
            continue
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = [f"## {sheet.name}",
                 "| " + " | ".join(rows[0]) + " |",
                 "|" + "---|" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- 공통


@dataclass
class RawDoc:
    """Docling 변환에 넘길 입력. 원본 파일을 그대로 쓰거나, 재구성한 Markdown을 쓴다."""

    path: Path
    fmt: str
    markdown: str | None = None  # None이면 원본 파일을 Docling에 직접 넘김

    def as_docling_source(self):
        if self.markdown is None:
            return str(self.path)
        from docling.datamodel.base_models import DocumentStream

        return DocumentStream(
            name=self.path.stem + ".md",
            stream=io.BytesIO(self.markdown.encode("utf-8")),
        )


def prepare(path: Path) -> RawDoc | None:
    """파일 하나를 Docling에 넣을 수 있는 형태로 만든다. 지원하지 않으면 None."""
    fmt = format_of(path)
    if fmt is None:
        return None
    ext = path.suffix.lower()
    if ext in HWP_EXTS:
        md = hwp_to_markdown(path)
        return RawDoc(path, fmt, md) if md.strip() else None
    if ext in XLS_EXTS:
        md = xls_to_markdown(path)
        return RawDoc(path, fmt, md) if md.strip() else None
    if ext in TEXT_EXTS:
        text = path.read_text(encoding="utf-8", errors="replace")
        return RawDoc(path, fmt, text) if text.strip() else None
    return RawDoc(path, fmt, None)

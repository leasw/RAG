"""코퍼스 walk -> Docling 변환 -> 형식별 청킹 -> SQLite 적재.

문서 단위로 커밋하므로 중간에 끊겨도 다시 실행하면 이어서 진행합니다
(이미 'ok'이고 mtime이 그대로인 문서는 건너뜀).
"""

from __future__ import annotations

import hashlib
import time
import traceback
from pathlib import Path

from .chunking import ChunkerBank
from .extractors import SKIPPED_EXTS, SUPPORTED_EXTS, prepare
from .store import ChunkStore


def _sha256(path: Path, limit: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            if f.tell() >= limit:
                break
    return h.hexdigest()


def _doc_id(root: Path, path: Path) -> str:
    rel = path.relative_to(root).as_posix()
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]


def _stage_of(root: Path, path: Path) -> str:
    """코퍼스 최상위 폴더명(1단계 / 2단계 / 15_단계 평가 결과)을 메타로 남긴다."""
    parts = path.relative_to(root).parts
    return parts[0] if len(parts) > 1 else ""


def walk_corpus(roots: list[Path], cfg: dict) -> list[tuple[Path, Path]]:
    exclude = set(cfg["corpus"].get("exclude_dirs") or [])
    out: list[tuple[Path, Path]] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in exclude for part in path.relative_to(root).parts):
                continue
            # "~$문서.pptx" 는 Office가 파일을 열어둔 동안 만드는 잠금용 임시 파일이다.
            # 내용이 없어 파서가 항상 실패하므로 목록에서 아예 뺀다.
            if path.name.startswith("~$"):
                continue
            out.append((root, path))
    return out


def _pdf_options():
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    options = PdfPipelineOptions()
    options.do_table_structure = True          # 표를 구조로 인식 -> 청킹이 행을 안 쪼갬
    options.table_structure_options.do_cell_matching = True
    options.do_ocr = False                     # 이 코퍼스의 PDF는 전부 텍스트 레이어 보유
    return options


_ALLOWED_FORMATS = None


def _allowed_formats():
    from docling.datamodel.base_models import InputFormat

    return [
        InputFormat.PDF, InputFormat.DOCX, InputFormat.PPTX,
        InputFormat.XLSX, InputFormat.XLS,
        InputFormat.MD, InputFormat.HTML, InputFormat.CSV,
    ]


def build_converter():
    """기본 변환기. PDF는 docling-parse 백엔드(레이아웃/표 인식이 가장 정확)."""
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter, PdfFormatOption

    return DocumentConverter(
        allowed_formats=_allowed_formats(),
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=_pdf_options())},
    )


def build_fallback_converter():
    """PDF 폴백 변환기.

    docling-parse 백엔드는 연산자 스트림이 규격에서 벗어난 PDF에서 파싱을 포기한다
    (이 코퍼스에는 한글에서 내보낸 그런 PDF가 섞여 있다). pypdfium 백엔드는 관대해서
    같은 파일을 읽어낸다 — 대신 표 구조 인식 품질이 조금 떨어진다.
    """
    from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter, PdfFormatOption

    return DocumentConverter(
        allowed_formats=_allowed_formats(),
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=_pdf_options(), backend=PyPdfiumDocumentBackend
            )
        },
    )


def build_ocr_converter(cfg: dict):
    """스캔 PDF 전용 변환기 (EasyOCR, 한국어+영어, GPU).

    이 코퍼스의 PDF 552개 중 306개는 텍스트 레이어가 아예 없는 스캔/이미지 PDF다
    (신청용 계획서 PART I/II, 각종 증빙 서류 등). 텍스트 레이어가 있는 PDF에까지
    OCR을 돌리면 느리고 품질도 더 나빠지므로, 앞선 두 경로가 청크를 하나도
    못 뽑았을 때만 이 변환기로 재시도한다.
    """
    from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import EasyOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    ocr_cfg = cfg.get("ocr") or {}
    options = _pdf_options()
    options.do_ocr = True
    # OCR한 페이지에 표 구조 모델까지 태우면, 인식된 글자를 엉뚱한 격자에 끼워 맞춰
    # "평가구분, 1 = [단계평가. 평가구분, 2 = ..." 같은 쓰레기 셀이 쏟아진다.
    # 스캔 페이지는 줄 단위 평문으로 두는 쪽이 검색 품질이 훨씬 낫다.
    options.do_table_structure = False
    options.ocr_options = EasyOcrOptions(
        lang=list(ocr_cfg.get("lang") or ["ko", "en"]),
        force_full_page_ocr=True,
        use_gpu=ocr_cfg.get("use_gpu", True),
        confidence_threshold=float(ocr_cfg.get("confidence_threshold", 0.4)),
        scale=float(ocr_cfg.get("scale", 2.0)),
    )
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=options,
                                             backend=PyPdfiumDocumentBackend)
        },
    )


def ingest(cfg: dict, roots: list[Path], store: ChunkStore, limit: int | None = None,
           force: bool = False, verbose: bool = True) -> dict:
    converter = build_converter()
    fallback = None            # PDF 파싱 실패 시에만 지연 생성
    ocr = None                 # 스캔 PDF를 만났을 때만 지연 생성
    bank = ChunkerBank(cfg)
    max_bytes = int(cfg["corpus"].get("max_file_mb", 120)) * 1024 * 1024

    files = walk_corpus(roots, cfg)
    if limit:
        files = files[:limit]

    counters = {"ok": 0, "skipped": 0, "failed": 0, "duplicate": 0, "cached": 0, "chunks": 0}
    started = time.time()

    for n, (root, path) in enumerate(files, start=1):
        doc_id = _doc_id(root, path)
        rel = path.relative_to(root).as_posix()
        ext = path.suffix.lower()
        stat = path.stat()

        base = dict(
            doc_id=doc_id, path=str(path), rel_path=rel, file_name=path.name,
            stage=_stage_of(root, path), size_bytes=stat.st_size, mtime=stat.st_mtime,
        )

        if ext not in SUPPORTED_EXTS:
            reason = "unsupported_binary" if ext in SKIPPED_EXTS else "unknown_extension"
            store.upsert_document(**base, format=ext.lstrip(".") or "none", content_sha="",
                                  n_chunks=0, status="skipped", error=reason)
            store.commit()
            counters["skipped"] += 1
            continue

        if stat.st_size > max_bytes:
            store.upsert_document(**base, format=ext.lstrip("."), content_sha="",
                                  n_chunks=0, status="skipped", error="too_large")
            store.commit()
            counters["skipped"] += 1
            continue

        if not force:
            status, mtime = store.doc_status(doc_id)
            if status in ("ok", "skipped") and mtime == stat.st_mtime:
                counters["cached"] += 1
                continue

        sha = _sha256(path)
        twin = store.seen_sha(sha)
        if twin and twin != doc_id:
            # 같은 파일의 사본(하위 폴더 백업 등). 청크를 중복 적재하지 않는다.
            store.upsert_document(**base, format=ext.lstrip("."), content_sha=sha,
                                  n_chunks=0, status="skipped", error=f"duplicate_of:{twin}")
            store.commit()
            counters["duplicate"] += 1
            continue

        try:
            raw = prepare(path)
            if raw is None:
                store.upsert_document(**base, format=ext.lstrip("."), content_sha=sha,
                                      n_chunks=0, status="skipped", error="empty_text")
                store.commit()
                counters["skipped"] += 1
                continue

            try:
                result = converter.convert(raw.as_docling_source())
                backend = "docling-parse"
            except Exception:
                if raw.fmt != "pdf":
                    raise
                if fallback is None:
                    fallback = build_fallback_converter()
                result = fallback.convert(raw.as_docling_source())
                backend = "pypdfium"
            chunks = list(bank.chunk(result.document, raw.fmt))
            if not chunks and raw.fmt == "pdf":
                if ocr is None:
                    ocr = build_ocr_converter(cfg)
                result = ocr.convert(raw.as_docling_source())
                chunks = list(bank.chunk(result.document, raw.fmt))
                backend = "easyocr"
            store.replace_chunks(doc_id, chunks)
            store.upsert_document(**base, format=raw.fmt, content_sha=sha,
                                  n_chunks=len(chunks), status="ok",
                                  error="" if backend == "docling-parse" else f"backend:{backend}")
            store.commit()
            counters["ok"] += 1
            counters["chunks"] += len(chunks)
            if verbose:
                mark = "ok " if backend == "docling-parse" else "ok*"
                print(f"[{n}/{len(files)}] {mark} {raw.fmt:5s} {len(chunks):4d} chunks  {rel}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 한 파일 실패가 전체를 멈추면 안 된다
            store.upsert_document(**base, format=ext.lstrip("."), content_sha=sha,
                                  n_chunks=0, status="failed",
                                  error=f"{type(exc).__name__}: {exc}"[:500])
            store.commit()
            counters["failed"] += 1
            if verbose:
                print(f"[{n}/{len(files)}] FAIL      {rel}\n    {type(exc).__name__}: {exc}",
                      flush=True)
                traceback.print_exc(limit=1)

    counters["elapsed_s"] = round(time.time() - started, 1)
    return counters

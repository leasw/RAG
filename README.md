# doc_rag — 한국어 조직 문서 벡터 RAG

hwp/pdf/docx/pptx/xlsx가 섞인 실제 조직 문서 코퍼스를 대상으로 한 검색 파이프라인입니다.
형식마다 다른 파서(Docling / pyhwp / xlrd)로 텍스트를 뽑고, 구조(heading·표) 경계를
따라 청킹한 뒤 dense+sparse 하이브리드 검색으로 답합니다.

## 파이프라인

```
문서 파일
  ├─ pdf/docx/pptx/xlsx ─ Docling DocumentConverter (+ OCR 폴백, EasyOCR)
  ├─ hwp ──────────────── pyhwp(hwp5html) → XHTML → lxml 순회 → Markdown
  │                        (Docling이 못 읽는 형식이라 표를 직접 복원한다)
  └─ xls(구형 BIFF) ────── xlrd
                 │
                 ▼
     Docling HybridChunker (형식별 max_tokens/overlap, heading 경계 우선)
                 │
                 ▼
     SQLite(chunks.sqlite3) + embeddings.npy
                 │
질의 ─┬─ Dense: 로컬 임베더 (config/doc_rag.yaml에서 교체 가능) 코사인 top-k
      └─ Sparse: BM25+ top-k
                 │
            RRF 결합
                 │
      Cross-encoder 리랭커로 재정렬 → 최종 top-k
```

## 설치

```bash
pip install -r requirements-doc_rag.txt
```

`docling`/`pyhwp`/`easyocr`는 무거운 선택 의존성입니다 — hwp 인제스천이나 스캔본 OCR이
필요 없다면 설치를 건너뛰어도 검색(`doc_rag.query`)은 이미 빌드된 인덱스로 바로 동작합니다.

**설치 순서 주의**: `easyocr`이 torch를 CPU 버전으로 덮어쓰는 경우가 있습니다.
GPU를 쓸 계획이면 torch(CUDA 빌드)를 먼저 설치한 뒤 나머지를 설치하세요.

## 사용

```bash
# 인덱스 빌드 (원본 문서 → 청킹 → 임베딩)
python -m doc_rag.build_index

# 원본 문서 없이 임베딩만 다시 (chunks.sqlite3에 텍스트가 이미 있을 때)
python -m doc_rag.build_index --skip-ingest

# 검색
python -m doc_rag.query "질문"

# 현재 인덱스 통계
python -m doc_rag.build_index --stats
```

## 설정

`config/doc_rag.yaml`에서 다음을 조정합니다.

- `corpus.roots` — 인제스천 대상 문서 폴더
- `chunking.by_format` — 형식별 `max_tokens`/`overlap`
- `embedding` — 임베딩 모델(로컬/OpenRouter), `batch_size`, `max_seq_length`
- `reranker` — 리랭커 모델, `batch_size`
- `ocr` — 스캔본 PDF 폴백 옵션

GPU 메모리가 작으면(`embedding.batch_size`, `reranker.batch_size` 축소) +
`embedding.max_seq_length`를 실제 청크 최대 토큰 수에 맞춰 낮추는 것이 속도에
가장 크게 영향을 줍니다 — 임베딩 모델 기본값(수천 토큰)을 그대로 두면 청크
실제 길이(수백 토큰) 대비 어텐션 메모리를 제곱으로 낭비합니다.

## 알려진 이슈 / 주의사항

- **HWP 표 파싱**: `pyhwp` → Docling으로 재파싱하는 경로에서, 인접한 표 행 사이
  줄바꿈이 사라지는 경우가 있었습니다(`doc_rag/chunking.py`의
  `_repair_glued_table_rows`로 수정·완료). 표가 포함된 문서를 재파싱할 계획이면
  이 경로가 최신 상태인지 확인하세요.
- **병합 셀**: Docling 기본 표 직렬화기는 병합된 셀 값을 반복해서 채워 넣어
  임베딩 텍스트가 중복으로 오염됩니다. `doc_rag/table_serializer.py`의
  `CompactSerializerProvider`가 이를 막습니다 — 값은 병합 시작 위치에만 씁니다.
- **스캔본 PDF**: 일부 한글 PDF는 Docling 기본 백엔드가 파싱에 실패합니다.
  `pypdfium` 백엔드로 재시도하고, 그래도 실패하면(스캔 이미지) EasyOCR로 폴백합니다.
- **구형 xls(BIFF)**: Docling(openpyxl 기반)이 못 읽어 `xlrd`로 별도 처리합니다.

## 디렉터리

```
doc_rag/
  ingest.py              문서 → chunks.sqlite3 (형식별 컨버터 라우팅)
  extractors.py           hwp/xls 전용 추출기
  chunking.py             HybridChunker 래퍼 + 표 행 복구
  table_serializer.py     병합 셀 중복 방지 표 직렬화기
  embedding_factory.py    임베더 인스턴스 캐시(로컬/OpenRouter)
  local_embedder.py       로컬 sentence-transformers 임베더
  reranker.py             cross-encoder 리랭커
  sparse.py               BM25+
  fusion.py               RRF 결합
  store.py                chunks.sqlite3 + embeddings.npy 저장소
  retriever.py            dense+sparse+rerank 검색 파이프라인
  query.py                CLI 진입점
  build_index.py          인제스천+임베딩 CLI
config/doc_rag.yaml        설정
index/doc_rag/             빌드된 인덱스 (chunks.sqlite3는 추적, embeddings.npy는 재생성 가능해 제외)
requirements-doc_rag.txt
```

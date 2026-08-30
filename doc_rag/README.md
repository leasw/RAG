# doc_rag — 과제 문서 벡터 RAG

`D:/RAG` 임베딩 벤치마크에서 **nDCG@10 1위**였던 조합을
`20200504_지식서비스산업핵심기술개발사업` 문서 코퍼스에 그대로 적용한 검색 도구입니다.
에이전트에는 `search_documents` 아이템으로 붙습니다.

## 파이프라인

```
문서 파일
  ├─ pdf/docx/pptx/xlsx ─ Docling DocumentConverter
  ├─ hwp ─────────────── pyhwp -> heading 복원 Markdown -> Docling MD 백엔드
  └─ txt ─────────────── Markdown -> Docling MD 백엔드
                 │
                 ▼
     Docling HybridChunker (형식별 max_tokens / overlap)
                 │
                 ▼
     SQLite(chunks.sqlite3) + embeddings.npy
                 │
질의 ─┬─ Dense: google/gemini-embedding-2 (3072d) 코사인 top-30
      └─ Sparse: BM25+ top-30
                 │
            RRF (k=60)
                 │
      BAAI/bge-reranker-v2-m3 (로컬 GPU) 재정렬 -> top-8
```

벤치마크 근거: `D:/RAG/results/leaderboard.md`
— `('google/gemini-embedding-2', 'local:BAAI/bge-reranker-v2-m3')` nDCG@10 avg **0.8581**.

## 형식별 인제스천 / 청킹

| 형식 | 인제스천 경로 | max_tokens | overlap | 비고 |
|---|---|---:|---:|---|
| pdf  | Docling (docling-parse) | 512 | 64 | 표 구조 인식 on |
| pdf (파싱 실패) | Docling (pypdfium 백엔드) | 512 | 64 | 규격 벗어난 한글 출력 PDF 폴백 |
| pdf (스캔본) | Docling + EasyOCR ko/en, GPU | 512 | 64 | 텍스트 레이어가 없을 때만. 표 구조는 끔 |
| hwp  | pyhwp -> Markdown -> Docling | 640 | 80 | 개요 번호(제N장/N./가./□/○)를 heading으로 복원 |
| pptx | Docling | 320 | 32 | 슬라이드 텍스트가 짧고 조각나 있어 작게 |
| xlsx | Docling | 768 | 0 | 표는 행 경계에서만 자름 |
| txt  | Markdown -> Docling | 512 | 64 | |
| 이미지/영상/zip/3D/ps | 건너뜀 | | | `extractors.SKIPPED_EXTS` |

중복 파일(같은 내용의 백업 사본)은 SHA-256으로 걸러 한 번만 적재합니다.

## 사용법

```bash
pip install -r ../requirements-doc_rag.txt

python -m doc_rag.build_index                    # 전체 빌드 (중단 시 이어서 재실행)
python -m doc_rag.build_index --stats            # 문서/청크 통계와 실패 목록
python -m doc_rag.query "1단계 정량목표가 뭐야?"   # 검색만 단독 확인
```

빌드는 문서 하나 끝날 때마다 커밋하므로 중간에 끊겨도 다시 실행하면 남은 것만 처리합니다
(`--force`로 전체 재처리). 임베딩은 `cache/embeddings/*.sqlite3`에 (모델, 텍스트) 해시로
캐시돼서 재빌드 시 이미 계산한 청크는 API를 다시 부르지 않습니다.

## 설정

`config/doc_rag.yaml` 한 곳에서 코퍼스 경로, 임베딩/리랭커 모델, 형식별 청킹
파라미터, 검색 top-k, OCR 옵션을 관리합니다.

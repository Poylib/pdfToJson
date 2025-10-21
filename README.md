# PDF to JSON (Streamlit)

특허 PDF를 RAG에 맞게 변환하는 Streamlit 앱과 배치/정적 도구가 포함되어 있습니다.

## 요구 사항

- Python 3.9+
- macOS / Windows / Linux

## 설치

```bash
# (선택) 가상환경 생성 및 활성화
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 패키지 설치
pip3 install -r requirements.txt
```

## 실행 (Streamlit)

```bash
python3 -m streamlit run app.py
```

브라우저가 자동으로 열리지 않으면 `http://localhost:8501` 로 접속하세요.

## 사용 방법 (앱 탭)

- 특허 변환: 특허 문서를 RAG 최적화 스키마로 변환
  - 문서 JSON: 섹션(ABSTRACT/CLAIMS/DESCRIPTION 등), 청구항/의존관계, 기초 메타데이터
  - 청크 JSONL: RAG 인덱싱용 청크(섹션타입/청구항번호/토큰추정 등 메타 포함)
  - 두 결과를 각각 다운로드 가능
- 대량 변환: 여러 PDF 업로드 → 문서 JSON(파일별) + 전체 청크 JSONL을 ZIP으로 다운로드

## 배치 처리(batch_ingest)

여러 PDF를 폴더 단위로 일괄 변환하려면 배치 스크립트를 사용하세요.

```bash
python3 batch_ingest.py --in /path/to/pdfs --out /path/to/out
```

- 결과 구조
  - `/out/docs/{파일명}.patent.json`: 문서 단위 JSON
  - `/out/chunks/all.chunks.jsonl`: 전체 문서 청크를 한 파일에 JSONL로 누적
  - `/out/errors.jsonl`: 에러 발생 파일 목록(있을 경우)

### 특허 변환 문서/청크 개요

- 문서 JSON(예): `sections[{type,title,text}]`, `claims[{num,text,dependencies}]`, `metadata{ipc_codes,cpc_codes,...}`
- 청크 JSONL(예): `{chunk_id,text,section_type,claim_nums,page_range,tokens_est,doc_id}`

## RAG 인덱싱 가이드(요약)

1. 임베딩 모델 예시: bge-m3, e5-multilingual, OpenAI `text-embedding-3-large`
2. 하이브리드 검색 권장: BM25 + 밀집벡터 + 재랭킹
3. 메타 필터: `ipc_codes`, `cpc_codes`, `assignee`, `jurisdiction`, `publication_year` 등
4. 가중치: `CLAIMS` > `ABSTRACT` > `DESCRIPTION` (랭킹 재정렬 시 참조)

## 팁

- 항상 전체 페이지를 처리합니다(페이지 선택 없음).
- 대량 변환 시 오류는 `errors.jsonl`을 확인하세요.

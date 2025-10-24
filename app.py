import io
import json
from typing import Any, Dict, List, Optional

import streamlit as st

# 특허 전처리 모듈 및 zip 생성
from patent_processing import (
    chunks_to_jsonl,
)
import zipfile
from patent_processing import convert_pdf_bytes_to_patent_json


# 불필요해진 일반 변환 관련 함수/코드를 제거했습니다.


def _ui_patent_converter() -> None:
    st.caption("특허 문서에 최적화된 스키마(섹션/청구항/메타/청크)로 변환합니다.")

    col1, col2 = st.columns([3, 2])
    with col2:
        pretty_print = st.checkbox("문서 JSON 들여쓰기", value=True, key="pat_pretty")
        ensure_ascii = st.checkbox("ASCII만 사용", value=False, key="pat_ascii")

    uploaded_file = st.file_uploader("특허 PDF 업로드", type=["pdf"], accept_multiple_files=False, key="patent_uploader")
    if uploaded_file is None:
        st.info("특허 PDF를 업로드하세요.")
        return

    file_bytes = uploaded_file.getvalue()

    status = st.empty()
    progress = st.progress(0)
    try:
        status.write("PDF 처리 및 JSON 생성 중…")
        document, chunks = convert_pdf_bytes_to_patent_json(file_bytes, file_name=uploaded_file.name)
        progress.progress(100)
        status.write("완료")
    except Exception as err:
        st.error(f"특허 변환 중 문제가 발생했습니다: {err}")
        return

    # Count using new schema with backward compatibility
    try:
        sec_count = len(document.get("structure", {}).get("sections_index", []))
        if sec_count == 0:
            sec_count = len(document.get("sections", []))
    except Exception:
        sec_count = len(document.get("sections", []))

    try:
        claim_count = document.get("structure", {}).get("claims_count")
        if claim_count is None:
            claim_count = len(document.get("claims", []))
    except Exception:
        claim_count = len(document.get("claims", []))

    st.success(f"특허 변환 완료: 섹션 {sec_count}개, 청구항 {claim_count}개, 청크 {len(chunks)}개")

    indent_value: Optional[int] = 2 if pretty_print else None
    doc_json_str = json.dumps(document, ensure_ascii=ensure_ascii, indent=indent_value)
    from patent_processing import chunks_to_jsonl as _chunks_to_jsonl
    chunks_jsonl_str = _chunks_to_jsonl(chunks)

    tab_doc, tab_chunks = st.tabs(["문서 JSON", "청크 JSONL"])
    with tab_doc:
        if pretty_print:
            st.code(doc_json_str, language="json")
        else:
            st.json(document)
    with tab_chunks:
        st.code(chunks_jsonl_str, language="json")

    base = uploaded_file.name.rsplit(".", 1)[0]
    st.download_button(
        label="문서 JSON 다운로드",
        data=doc_json_str.encode("utf-8"),
        file_name=f"{base}.patent.json",
        mime="application/json",
        key="pat_doc_dl",
    )
    st.download_button(
        label="청크 JSONL 다운로드",
        data=chunks_jsonl_str.encode("utf-8"),
        file_name=f"{base}.chunks.jsonl",
        mime="application/json",
        key="pat_chunks_dl",
    )


def _ui_bulk_converter() -> None:
    st.caption("여러 특허 PDF를 동시에 변환하여 ZIP으로 제공합니다.")
    uploaded_files = st.file_uploader(
        "PDF 다중 업로드",
        type=["pdf"],
        accept_multiple_files=True,
        key="bulk_uploader",
    )
    if not uploaded_files:
        st.info("여러 PDF를 선택해 업로드하세요.")
        return

    if st.button("일괄 변환 시작", type="primary", key="bulk_start"):
        progress = st.progress(0)
        status = st.empty()

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            all_chunks: List[Dict[str, Any]] = []
            for idx, uf in enumerate(uploaded_files):
                status.write(f"처리 중: {uf.name} ({idx+1}/{len(uploaded_files)})")
                try:
                    # 문서 변환 (공용 변환기 사용)
                    doc, chunks = convert_pdf_bytes_to_patent_json(uf.getvalue(), file_name=uf.name)
                except Exception as err:
                    error_msg = json.dumps({"file": uf.name, "error": str(err)}, ensure_ascii=False)
                    zf.writestr(f"errors/{uf.name}.error.json", error_msg)
                    continue

                base = uf.name.rsplit(".", 1)[0]
                zf.writestr(f"docs/{base}.patent.json", json.dumps(doc, ensure_ascii=False, indent=2))
                all_chunks.extend(chunks)

                progress.progress(int(((idx + 1) / len(uploaded_files)) * 100))

            zf.writestr("chunks/all.chunks.jsonl", chunks_to_jsonl(all_chunks))

        status.write("압축 파일 생성 중…")
        zip_buffer.seek(0)
        st.download_button(
            label="ZIP 다운로드 (문서 JSON + 전체 청크 JSONL)",
            data=zip_buffer.getvalue(),
            file_name="patent_bulk_output.zip",
            mime="application/zip",
            key="bulk_zip_dl",
        )
        status.write("완료")


def main() -> None:
    st.set_page_config(page_title="Patent PDF → JSON", page_icon="📄", layout="wide")
    st.title("📄 특허 PDF → JSON 변환기")
    st.write("특허 문서를 RAG에 적합한 JSON/JSONL로 변환합니다.")

    tab_patent, tab_bulk = st.tabs(["특허 변환", "대량 변환"])

    with tab_patent:
        _ui_patent_converter()
    with tab_bulk:
        _ui_bulk_converter()


if __name__ == "__main__":
    main()

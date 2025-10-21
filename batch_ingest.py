import argparse
import io
import json
import os
import sys
from pathlib import Path
from typing import List, Tuple

from tqdm import tqdm

from patent_processing import convert_pdf_bytes_to_patent_json, chunks_to_jsonl


def _iter_pdf_files(input_dir: Path) -> List[Path]:
    return sorted([p for p in input_dir.rglob("*.pdf") if p.is_file()])


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def process_one_pdf(pdf_path: Path, out_docs: Path, out_chunks_all: Path) -> Tuple[bool, str]:
    try:
        file_bytes = pdf_path.read_bytes()
        document, chunks = convert_pdf_bytes_to_patent_json(file_bytes, file_name=pdf_path.name)

        base = pdf_path.stem
        doc_out_path = out_docs / f"{base}.patent.json"
        doc_out_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

        with out_chunks_all.open("a", encoding="utf-8") as fw:
            for line in chunks_to_jsonl(chunks).splitlines():
                fw.write(line + "\n")

        return True, "ok"
    except Exception as e:
        return False, str(e)


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch convert patent PDFs to JSON and JSONL (RAG-ready)")
    parser.add_argument("--in", dest="inp", required=True, help="Input directory containing PDFs")
    parser.add_argument("--out", dest="out", required=True, help="Output directory for docs/ and chunks/")

    args = parser.parse_args()
    in_dir = Path(args.inp).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()

    if not in_dir.exists() or not in_dir.is_dir():
        print(f"Input directory not found: {in_dir}", file=sys.stderr)
        sys.exit(1)

    out_docs = out_dir / "docs"
    out_chunks = out_dir / "chunks"
    _ensure_dir(out_docs)
    _ensure_dir(out_chunks)
    out_chunks_all = out_chunks / "all.chunks.jsonl"
    if out_chunks_all.exists():
        out_chunks_all.unlink()
    out_chunks_all.touch()

    pdf_files = _iter_pdf_files(in_dir)
    if not pdf_files:
        print("No PDF files found.")
        return

    errors = []
    for p in tqdm(pdf_files, desc="Processing PDFs"):
        ok, msg = process_one_pdf(p, out_docs, out_chunks_all)
        if not ok:
            errors.append({"file": str(p), "error": msg})

    if errors:
        err_path = out_dir / "errors.jsonl"
        with err_path.open("w", encoding="utf-8") as fw:
            for e in errors:
                fw.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"Completed with {len(errors)} errors. See: {err_path}")
    else:
        print("Completed successfully.")


if __name__ == "__main__":
    main()

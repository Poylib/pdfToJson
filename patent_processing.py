import io
import re
import json
import hashlib
from typing import Any, Dict, List, Optional, Tuple

import pdfplumber


def clean_text(text: str) -> str:
    """Basic cleanup for patent text.

    - Remove soft-hyphen
    - Heal hyphen-newline breaks
    - Normalize excessive blank lines
    """
    s = text.replace("\u00AD", "")
    s = re.sub(r"-\n(?=[a-z])", "", s)  # heal hyphen joins for lowercase english
    s = re.sub(r"\n{2,}", "\n\n", s)
    return s.strip()


SECTION_HEADERS: List[Tuple[str, str]] = [
    ("ABSTRACT", r"\babstract\b|요약"),
    ("CLAIMS", r"\bclaims?\b|청구항"),
    ("DESCRIPTION", r"\b(description|detailed description|specification)\b|설명서|상세한 설명"),
    ("BACKGROUND", r"\b(background|field of the invention)\b|배경"),
    ("SUMMARY", r"\bsummary\b|요약(서)?"),
    ("DRAWINGS", r"\bbrief description of the drawings\b|도면의 간단한 설명"),
]


def split_sections(full_text: str) -> List[Dict[str, Any]]:
    text = clean_text(full_text)
    lines = text.splitlines()
    sections: List[Dict[str, Any]] = []
    current = {"type": "UNKNOWN", "title": "", "text": []}  # type: ignore[dict-item]

    def flush() -> None:
        if current["text"]:
            sections.append(
                {
                    "type": current["type"],
                    "title": current["title"],
                    "text": "\n".join(current["text"]).strip(),
                }
            )

    for ln in lines:
        low = ln.lower().strip()
        matched: Optional[Tuple[str, str]] = None
        for t, pat in SECTION_HEADERS:
            if re.search(pat, low):
                matched = (t, ln.strip())
                break
        if matched:
            flush()
            current = {"type": matched[0], "title": matched[1], "text": []}
        else:
            current["text"].append(ln)
    flush()
    return sections


CLAIM_LINE = re.compile(r'^\s*(?:claim\s*)?(\d+)[\.\)]\s+', re.I)
KO_CLAIM_LINE = re.compile(r"^\s*청구항\s*(\d+)\s*[\.）)]?\s*")


def extract_claims(claims_text: str) -> List[Dict[str, Any]]:
    claims: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for ln in claims_text.splitlines():
        m = CLAIM_LINE.match(ln) or KO_CLAIM_LINE.match(ln)
        if m:
            if cur:
                claims.append(cur)
            cur = {"num": int(m.group(1)), "text": ln[m.end():].strip()}
        elif cur:
            cur["text"] += "\n" + ln.strip()
    if cur:
        claims.append(cur)

    # Basic dependency detection (references to other claims)
    for c in claims:
        refs = re.findall(r"(?:claim|제)\s*([0-9]+)", c["text"], flags=re.I)
        c["dependencies"] = sorted({int(x) for x in refs if int(x) != c["num"]})
    return claims


def _paragraphs(text: str) -> List[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _sliding_split(text: str, target_tokens: int, overlap_tokens: int) -> List[str]:
    # Token estimate: ~ 4 chars per token
    char_target = max(1, target_tokens * 4)
    char_overlap = max(0, overlap_tokens * 4)
    if char_overlap >= char_target:
        # keep at most 10% overlap to guarantee progress
        char_overlap = max(0, char_target // 10)
    step = max(1, char_target - char_overlap)
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        j = min(n, i + char_target)
        out.append(text[i:j])
        i += step
    return out


def chunk_for_rag(
    sections: List[Dict[str, Any]],
    claims: List[Dict[str, Any]],
    *,
    target_tokens: int = 600,
    overlap_tokens: int = 80,
) -> List[Dict[str, Any]]:
    def tlen(s: str) -> int:
        return max(1, len(s) // 4)

    chunks: List[Dict[str, Any]] = []

    # Claim-centric chunks
    for c in claims:
        txt = c["text"].strip()
        if tlen(txt) <= int(target_tokens * 1.5):
            chunks.append(
                {
                    "text": txt,
                    "section_type": "CLAIMS",
                    "claim_nums": [c["num"]],
                    "page_range": None,
                }
            )
        else:
            parts = _sliding_split(txt, target_tokens, overlap_tokens)
            for p in parts:
                chunks.append(
                    {
                        "text": p,
                        "section_type": "CLAIMS",
                        "claim_nums": [c["num"]],
                        "page_range": None,
                    }
                )

    # Section-based chunks
    for s in sections:
        if s["type"] in ("CLAIMS",):
            continue
        for p in _paragraphs(s["text"]):
            if tlen(p) <= target_tokens:
                chunks.append(
                    {
                        "text": p,
                        "section_type": s["type"],
                        "claim_nums": [],
                        "page_range": None,
                    }
                )
            else:
                for sp in _sliding_split(p, target_tokens, overlap_tokens):
                    chunks.append(
                        {
                            "text": sp,
                            "section_type": s["type"],
                            "claim_nums": [],
                            "page_range": None,
                        }
                    )

    # Add IDs and token estimates
    out: List[Dict[str, Any]] = []
    for i, ch in enumerate(chunks):
        h = hashlib.md5(ch["text"].encode("utf-8")).hexdigest()[:16]
        ch["chunk_id"] = f"c{i:06d}_{h}"
        ch["tokens_est"] = len(ch["text"]) // 4
        out.append(ch)
    return out


META_PATTERNS = {
    "publication_number": r"\b(PUB\s*NO\.?|publication\s*number)[:\s]*([A-Z]{2}\d+[A-Z]?\d*)",
    "application_number": r"\b(application\s*number|app\s*no\.?|appl\.?\s*no\.?)[:\s]*([A-Z]{2}\d+[A-Z]?\d*)",
    "ipc_codes": r"\bIPC\b[:\s]*([A-Z0-9/;\s,]+)",
    "cpc_codes": r"\bCPC\b[:\s]*([A-Z0-9/;\s,]+)",
}


def _normalize_kr_date(raw: str) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip()
    m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"(\d{4})[\./-](\d{1,2})[\./-](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"\b(\d{4})(\d{2})(\d{2})\b", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


_CODE_RE = re.compile(r"\b[A-H][0-9]{2}[A-Z]\s?[0-9]+/[0-9]+\b")


def _extract_kr_metadata(text: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if "대한민국특허청" in text or "(KR)" in text:
        meta["jurisdiction"] = "KR"

    # Publication/Application numbers and dates
    m = re.search(r"\(11\)\s*공개번호\s*([0-9]{2,4}-\d{5,})", text)
    if m:
        meta["publication_number"] = m.group(1)
    m = re.search(r"\(43\)\s*공개일자\s*([^\n]+)", text)
    if m:
        iso = _normalize_kr_date(m.group(1))
        if iso:
            meta["publication_date"] = iso

    m = re.search(r"\(21\)\s*출원번호\s*([0-9]{2,4}-\d{5,})", text)
    if m:
        meta["application_number"] = m.group(1)
    m = re.search(r"\(22\)\s*출원일자\s*([^\n]+)", text)
    if m:
        iso = _normalize_kr_date(m.group(1))
        if iso:
            meta["application_date"] = iso

    # Title
    m = re.search(r"\(54\)\s*발명의\s*명칭\s*([^\n]+)", text)
    if m:
        meta["title"] = m.group(1).strip()

    # Assignee (Applicant) and Inventors (line after labels)
    m = re.search(r"\(71\)\s*출원인\s*([^\n]+)", text)
    if m:
        meta["assignee"] = m.group(1).strip()
    m = re.search(r"\(72\)\s*발명자\s*([^\n]+)", text)
    if m:
        invent_raw = m.group(1).strip()
        # split by comma/ideographic comma or 2+ spaces
        parts = re.split(r"[,、，]|\s{2,}", invent_raw)
        meta["inventors"] = [p.strip() for p in parts if p.strip()]

    # IPC codes from (51) block
    m = re.search(r"\(51\)[\s\S]{0,600}", text)
    if m:
        ipc_block = m.group(0)
        codes = _CODE_RE.findall(ipc_block)
        if codes:
            meta["ipc_codes"] = sorted(list({c.replace("  ", " ").strip() for c in codes}))

    # CPC codes from (52) block
    m = re.search(r"\(52\)[\s\S]{0,600}", text)
    if m:
        cpc_block = m.group(0)
        codes = _CODE_RE.findall(cpc_block)
        if codes:
            meta["cpc_codes"] = sorted(list({c.replace("  ", " ").strip() for c in codes}))

    return meta


def extract_basic_metadata(text: str) -> Dict[str, Any]:
    # Prefer KR-specific extraction if possible
    kr_meta = _extract_kr_metadata(text)
    if kr_meta:
        return kr_meta

    # Fallback: simple English patterns
    meta: Dict[str, Any] = {}
    for k, pat in META_PATTERNS.items():
        m = re.search(pat, text, flags=re.I)
        if m:
            meta[k] = m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1)

    # prune empty
    return {k: v for k, v in meta.items() if v not in (None, "", [], {})}


def convert_pdf_bytes_to_patent_json(
    file_bytes: bytes,
    *,
    file_name: str,
    target_tokens: int = 600,
    overlap_tokens: int = 80,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Convert PDF bytes into a patent-oriented JSON doc and RAG chunks.

    Returns (doc, chunks). The doc is document-level JSON; chunks are ready for embedding.
    """
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        texts: List[str] = []
        for p in pdf.pages:
            txt = p.extract_text() or ""
            texts.append(txt)
        full_text = "\n".join(texts)

    cleaned = clean_text(full_text)
    sections = split_sections(cleaned)

    # Locate claims section content
    claims_text = ""
    for s in sections:
        if s.get("type") == "CLAIMS":
            claims_text = s.get("text", "")
            break
    claims = extract_claims(claims_text) if claims_text else []

    chunks = chunk_for_rag(sections, claims, target_tokens=target_tokens, overlap_tokens=overlap_tokens)

    # Metadata extraction (KR-aware, pruned)
    meta = extract_basic_metadata(cleaned)

    # Build document JSON
    doc_id = hashlib.md5((file_name + str(len(cleaned))).encode("utf-8")).hexdigest()
    document: Dict[str, Any] = {
        "doc_id": doc_id,
        "file_name": file_name,
        "num_sections": len(sections),
        "num_claims": len(claims),
        "metadata": meta,
        "sections": sections,
        "claims": claims,
    }

    # Back-reference document id in chunks
    for ch in chunks:
        ch["doc_id"] = doc_id

    return document, chunks


def chunks_to_jsonl(chunks: List[Dict[str, Any]]) -> str:
    return "\n".join(json.dumps(c, ensure_ascii=False) for c in chunks)

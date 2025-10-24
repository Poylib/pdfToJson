import io
import re
import json
import hashlib
from typing import Any, Dict, List, Optional, Tuple

import pdfplumber
try:
    from pdf2image import convert_from_bytes
    import pytesseract
    from PIL import Image
except Exception:  # optional OCR deps may not be installed at runtime
    convert_from_bytes = None  # type: ignore
    pytesseract = None  # type: ignore
    Image = None  # type: ignore


def clean_text(text: str) -> str:
    """Basic cleanup for patent text.

    - Remove soft-hyphen
    - Heal hyphen-newline breaks
    - Normalize excessive blank lines
    """
    s = text.replace("\u00AD", "")
    # Heal hyphen-newline across wrapped words (both cases)
    s = re.sub(r"-\n(?=[A-Za-z])", "", s)
    s = re.sub(r"\n{2,}", "\n\n", s)
    return s.strip()


SECTION_HEADERS: List[Tuple[str, str]] = [
    ("ABSTRACT", r"\babstract\b|요약|要約"),
    # JP: 特許請求の範囲 / 請求項
    ("CLAIMS", r"\bclaims?\b|\bwhat\s+is\s+claimed\b|청구항|特許請求の範囲|請求項"),
    # JP: 発明の詳細な説明 / 実施形態 / 実施例
    ("DESCRIPTION", r"\b(description|detailed\s+description|specification)\b|설명서|상세한 설명|発明の詳細な説明|実施形態|実施例"),
    # JP: 技術分野 / 背景技術
    ("BACKGROUND", r"\b(background|field\s+of\s+the\s+invention)\b|배경|技術分野|背景技術"),
    # JP: 課題 / 解決手段 / 効果 → 요약적 성격인 섹션도 포함
    ("SUMMARY", r"\bsummary\b|요약(서)?|課題(を解決するための手段)?|発明の効果"),
    # JP: 図面の簡単な説明
    ("DRAWINGS", r"\bbrief\s+description\s+of\s+the\s+drawings\b|도면의 간단한 설명|図面の簡単な説明"),
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
# JP: 【請求項1】 / 請求項１. / 請求項1） (全角数字 포함)
JA_CLAIM_LINE = re.compile(r"^\s*[【\[\(（]?\s*請求項\s*([0-9０-９]+)\s*[】\]\)）]?[\.．）)]?\s*")

def _normalize_fullwidth_digits(s: str) -> str:
    # Convert fullwidth digits to ASCII
    return s.translate(str.maketrans({
        '０': '0', '１': '1', '２': '2', '３': '3', '４': '4',
        '５': '5', '６': '6', '７': '7', '８': '8', '９': '9',
    }))

def _normalize_fullwidth_punct(s: str) -> str:
    """Normalize fullwidth punctuation and symbols common in JP patents.

    - Convert fullwidth dot/percent/slash/minus/comma to ASCII
    - Keep content otherwise unchanged
    """
    table = str.maketrans({
        '．': '.', '，': ',', '％': '%', '／': '/', '－': '-',
    })
    return _normalize_fullwidth_digits(s).translate(table)


def extract_claims(claims_text: str) -> List[Dict[str, Any]]:
    claims: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for ln in claims_text.splitlines():
        m = CLAIM_LINE.match(ln) or KO_CLAIM_LINE.match(ln) or JA_CLAIM_LINE.match(ln)
        if m:
            if cur:
                claims.append(cur)
            num_str = _normalize_fullwidth_digits(m.group(1))
            cur = {"num": int(num_str), "text": ln[m.end():].strip()}
        elif cur:
            cur["text"] += "\n" + ln.strip()
    if cur:
        claims.append(cur)

    # Basic dependency detection (references to other claims)
    for c in claims:
        # English/Korean references
        refs = re.findall(r"(?:claim|제)\s*([0-9]+)", c["text"], flags=re.I)
        # Japanese references: 請求項n / 第n項
        refs += [
            _normalize_fullwidth_digits(x)
            for x in re.findall(r"請求項\s*([0-9０-９]+)", c["text"]) + re.findall(r"第\s*([0-9０-９]+)\s*項", c["text"])
        ]
        try:
            nums = {int(x) for x in refs}
        except Exception:
            nums = set()
        c["dependencies"] = sorted({n for n in nums if n != c["num"]})
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
        if s["type"] in ("CLAIMS", "UNKNOWN"):
            continue
        for p in _paragraphs(s["text"]):
            # drop too-short noise paragraphs for non-critical sections
            min_tokens = 15
            allow_short = s["type"] in ("ABSTRACT",)
            if tlen(p) <= target_tokens:
                if allow_short or tlen(p) >= min_tokens:
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
                    if allow_short or tlen(sp) >= min_tokens:
                        chunks.append(
                            {
                                "text": sp,
                                "section_type": s["type"],
                                "claim_nums": [],
                                "page_range": None,
                            }
                        )

    # Add IDs and token estimates
    def _detect_lang(sample: str) -> str:
        if re.search(r"[가-힣]", sample):
            return "ko"
        # Japanese: Hiragana, Katakana, Kanji
        if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", sample):
            return "ja"
        return "en"

    def _weight_for_section(section_type: str) -> float:
        if section_type == "CLAIMS":
            return 1.3
        if section_type == "ABSTRACT":
            return 1.15
        if section_type == "BACKGROUND":
            return 0.95
        return 1.0

    def _extract_norm_numbers(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
        nums: List[Dict[str, Any]] = []
        units_found: List[str] = []
        t = _normalize_fullwidth_punct(text)
        # wt% or % (include fullwidth % ％ and Japanese qualifiers)
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(wt%|%|％|質量%|質量％|重量%|重量％|原子%)", t, flags=re.I):
            val = float(m.group(1))
            unit_raw = m.group(2)
            unit = "%" if unit_raw in ("%", "％", "質量%", "質量％", "重量%", "重量％", "原子%") else unit_raw.lower()
            nums.append({"name": "percent", "value": val, "unit": unit})
            units_found.append(unit)
        # temperature °C or ℃ (require degree-like symbol to avoid C22C false positives)
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:°\s*C|℃)(?:\.|\b)", t, flags=re.I):
            nums.append({"name": "temperature", "value": float(m.group(1)), "unit": "°C"})
            units_found.append("°C")
        # cooling rate °C/s or ℃/s
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:°\s*C|℃)\s*/\s*(?:s|sec)\b", t, flags=re.I):
            nums.append({"name": "cooling_rate", "value": float(m.group(1)), "unit": "°C/s"})
            units_found.append("°C/s")
        # core loss W/kg
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*W/kg\b", t, flags=re.I):
            nums.append({"name": "core_loss", "value": float(m.group(1)), "unit": "W/kg"})
            units_found.append("W/kg")
        # micrometers μm/um
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:μm|um)\b", t, flags=re.I):
            nums.append({"name": "length", "value": float(m.group(1)), "unit": "μm"})
            units_found.append("μm")
        return nums, list(sorted(set(units_found)))

    def _extract_parameters(text: str) -> List[str]:
        params: List[str] = []
        for el in ["Si", "Al", "Cr", "Mn", "C", "N", "B", "Ni", "P", "Cu", "Mo", "S"]:
            if re.search(rf"\b{el}\b", text):
                params.append(el)
        # process / microstructure keywords
        keywords = [
            "cooling_rate", "cube_texture", "<100>", "EBSD", "grain", "anneal", "hot rolling",
            # Japanese domain terms
            "焼鈍", "冷間圧延", "熱間圧延", "冷却速度", "結晶", "介在物", "立方体集合",
        ]
        for kw in keywords:
            if kw.lower() in text.lower():
                params.append(kw)
        return sorted(list({p for p in params}))

    def _infer_role(text: str, section_type: str) -> str:
        low = text.lower()
        if section_type == "CLAIMS":
            return "CONFIG"
        if any(w in low for w in ["effect", "property", "loss", "magnetic", "elongation", "効果", "特性", "磁気", "損失"]):
            return "EFFECT"
        if any(w in low for w in ["rolling", "anneal", "heat treatment", "cooling", "process", "圧延", "焼鈍", "熱処理", "冷却"]):
            return "PROCESS"
        if any(w in low for w in ["measured", "ebsd", "sem", "eds", "xrd", "cube texture", "測定", "分析"]):
            return "MEASUREMENT"
        return "CONFIG"

    out: List[Dict[str, Any]] = []
    for i, ch in enumerate(chunks):
        h = hashlib.md5(ch["text"].encode("utf-8")).hexdigest()[:16]
        ch["chunk_id"] = f"c{i:06d}_{h}"
        ch["tokens_est"] = len(ch["text"]) // 4
        ch["lang"] = _detect_lang(ch["text"])  # simple heuristic
        ch["weight"] = _weight_for_section(ch.get("section_type", ""))
        norm_nums, units = _extract_norm_numbers(ch["text"])  # numbers with units
        ch["norm_numbers"] = norm_nums
        ch.setdefault("tags", {})
        ch["tags"]["units"] = units
        ch["tags"]["parameters"] = _extract_parameters(ch["text"])
        ch["tags"]["role"] = _infer_role(ch["text"], ch.get("section_type", ""))
        out.append(ch)
    return out


def _normalize_for_match(s: str) -> List[str]:
    s2 = re.sub(r"\s+", " ", s.lower())
    # include Japanese Hiragana/Katakana/Kanji to keep tokens for page matching
    s2 = re.sub(r"[^0-9a-zA-Z가-힣\u3040-\u30ff\u4e00-\u9fff·\.\-_%\s]", "", s2)
    return [t for t in s2.split() if len(t) >= 2]


def _guess_citation_page(chunk_text: str, per_page_texts: List[str]) -> Optional[int]:
    snippet = chunk_text.strip()
    snippet = snippet[:200] if len(snippet) > 200 else snippet
    norm_snip_tokens = set(_normalize_for_match(snippet))
    if not norm_snip_tokens:
        return None

    best_idx: Optional[int] = None
    best_score = -1
    for idx, page_txt in enumerate(per_page_texts):
        page_tokens = set(_normalize_for_match(page_txt))
        score = len(norm_snip_tokens & page_tokens)
        if score > best_score:
            best_score = score
            best_idx = idx

    if best_idx is None:
        return None
    threshold = max(5, len(norm_snip_tokens) // 10)
    if best_score < threshold:
        return None
    return best_idx + 1


def _annotate_chunks_for_prompt(
    chunks: List[Dict[str, Any]],
    *,
    per_page_texts: List[str],
    document_id: str,
    publication_number: Optional[str],
) -> None:
    doc_id_for_ref = publication_number or document_id
    for ch in chunks:
        if ch.get("page_range") in (None, [], {}):
            page = _guess_citation_page(ch.get("text", ""), per_page_texts)
            if page is not None:
                ch["page_range"] = [page, page]
                ch["citation_page"] = page
        else:
            pr = ch.get("page_range")
            if isinstance(pr, list) and pr:
                ch["citation_page"] = pr[0]

        ch["doc_id"] = document_id
        ch["DocumentId"] = doc_id_for_ref
        ch["CitationPage"] = ch.get("citation_page")
        ch["Context"] = ch.get("text")


META_PATTERNS = {
    "publication_number": r"\b(PUB\s*NO\.?|publication\s*number)[:\s]*([A-Z]{2}\d+[A-Z]?\d*)",
    "application_number": r"\b(application\s*number|app\s*no\.?|appl\.?\s*no\.?)[:\s]*([A-Z]{2}\d+[A-Z]?\d*)",
    "ipc_codes": r"\bIPC\b[:\s]*([A-Z0-9/;\s,]+)",
    "cpc_codes": r"\bCPC\b[:\s]*([A-Z0-9/;\s,]+)",
}


def _normalize_jp_date(raw: str) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip()
    # Gregorian forms: YYYY年MM月DD日 or YYYY/MM/DD or YYYY.M.D
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"(\d{4})[\./-](\d{1,2})[\./-](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    # Reiwa era: 令和N年M月D日 (Reiwa 1 = 2019)
    m = re.search(r"令和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", s)
    if m:
        r, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = 2018 + r  # R1 -> 2019
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _extract_jp_metadata(text: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if "日本国特許庁" in text or re.search(r"\bJP\s*\d", text):
        meta["jurisdiction"] = "JP"

    # Publication/Patent numbers
    # Pattern A: JP 7415134 B2 → JP7415134B2
    m = re.search(r"\bJP\s*(\d{4,9})\s*([AB]\d)\b", text)
    if m:
        meta["publication_number"] = f"JP{m.group(1)}{m.group(2)}".replace(" ", "")
    # Pattern B: 特開2021-80494A / 特表YYYY-xxxxxx
    if "publication_number" not in meta:
        m = re.search(r"特(?:開|表)\s*(\d{4})[-－](\d{5,7})\s*([A-Z]\d)?", text)
        if m:
            suffix = m.group(3) or "A1"
            meta["publication_number"] = f"JP{m.group(1)}-{m.group(2)}{suffix}"
    # Pattern C: (11) 特許番号/公開番号
    if "publication_number" not in meta:
        m = re.search(r"\(11\)\s*(?:特許番号|公開番号)\s*([^)\n]+)", text)
        if m:
            meta["publication_number"] = re.sub(r"\s+", "", m.group(1))

    # Application number
    m = re.search(r"\(21\)\s*出願番号\s*([^\n]+)", text)
    if m:
        meta["application_number"] = m.group(1).strip()
    else:
        m = re.search(r"特願\s*([0-9]{4}[-－]\d{5,7})", text)
        if m:
            meta["application_number"] = m.group(1).replace("－", "-")

    # Dates
    m = re.search(r"\(22\)\s*出願日\s*([^\n]+)", text)
    if m:
        iso = _normalize_jp_date(m.group(1))
        if iso:
            meta["application_date"] = iso
    m = re.search(r"\((?:43|45)\)\s*(?:公開日|発行日)\s*([^\n]+)", text)
    if m:
        iso = _normalize_jp_date(m.group(1))
        if iso:
            meta["publication_date"] = iso

    # Title
    m = re.search(r"\(54\)\s*発明の名称\s*([^\n]+)", text)
    if m:
        meta["title"] = m.group(1).strip()

    # Assignee / Applicant and Inventors
    m = re.search(r"\(71\)\s*出願人\s*([^\n]+)", text)
    if m:
        meta["assignee"] = m.group(1).strip()
    m = re.search(r"\(72\)\s*発明者\s*([^\n]+)", text)
    if m:
        invent_raw = m.group(1).strip()
        parts = re.split(r"[,、，]|\s{2,}", invent_raw)
        meta["inventors"] = [p.strip() for p in parts if p.strip()]

    # IPC codes from (51)
    m = re.search(r"\(51\)[\s\S]{0,600}", text)
    if m:
        blk = m.group(0)
        codes = _CODE_RE.findall(blk)
        if codes:
            meta["ipc_codes"] = sorted(list({c.replace("  ", " ").strip() for c in codes}))

    return meta

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


def _extract_us_metadata(text: str) -> Dict[str, Any]:
    """Extract basic metadata from a USPTO-format specification.

    Targets common labels and masthead patterns seen on US patents and publications.
    The goal is to fill the shared RAG metadata schema without changing the UI.
    """
    meta: Dict[str, Any] = {}

    # Jurisdiction heuristics
    if re.search(r"\bUnited\s+States\s+Patent\b", text, flags=re.I) or re.search(r"\bUS\s*\d", text):
        meta["jurisdiction"] = "US"

    # Publication/Patent numbers
    # Granted: US 11,505,845 B2 / US 7,654,321 B1 (commas optional)
    m = re.search(r"\bUS\s*\d{1,3}(?:,\d{3})+\s*[AB]\d\b", text)
    if not m:
        # Application publication: US 2023/0123456 A1
        m = re.search(r"\bUS\s*\d{4}/\d{7}\s*[A-Z]\d\b", text)
    if m:
        pub_no = m.group(0)
        pub_no = re.sub(r"\s+", "", pub_no).replace(",", "")
        meta["publication_number"] = pub_no

    # Application number: Appl. No.: 16/565,759  (commas/spaces vary)
    m = re.search(
        r"(Appl\.?\s*No\.?|Application\s*No\.?)\s*[:\-]?\s*([A-Z]{0,2}\s*\d{2}/\d{3},?\d{3})",
        text,
        flags=re.I,
    )
    if m:
        meta["application_number"] = re.sub(r"[\s,]", "", m.group(2))

    # Title (sometimes also shown as (54) ...)
    m = re.search(r"\bTitle\b\s*[:\-]?\s*([^\n]+)", text, flags=re.I)
    if not m:
        m = re.search(r"\(54\)\s*([^\n]+)", text)
    if m:
        meta["title"] = m.group(1).strip()

    # Assignee / Applicant
    m = re.search(r"\bAssignee(?:\(s\))?\b\s*[:\-]?\s*([^\n]+)", text, flags=re.I)
    if not m:
        m = re.search(r"\bApplicant\b\s*[:\-]?\s*([^\n]+)", text, flags=re.I)
    if m:
        meta["assignee"] = m.group(1).strip()

    # Inventors (comma/semicolon/and separated)
    m = re.search(r"\bInventors?\b\s*[:\-]?\s*([^\n]+)", text, flags=re.I)
    if m:
        raw = m.group(1)
        parts = re.split(r"[;,]|\band\b", raw, flags=re.I)
        inv = [p.strip() for p in parts if p.strip()]
        if inv:
            meta["inventors"] = inv

    # IPC (Int. Cl.)
    blk = re.search(r"Int\.?\s*Cl\.?[\s\S]{0,600}", text, flags=re.I)
    codes = _CODE_RE.findall(blk.group(0)) if blk else []
    if codes:
        meta["ipc_codes"] = sorted(list({c.replace("  ", " ").strip() for c in codes}))

    # CPC
    blk = re.search(r"\bCPC\b[\s\S]{0,600}", text, flags=re.I)
    codes = _CODE_RE.findall(blk.group(0)) if blk else []
    if codes:
        meta["cpc_codes"] = sorted(list({c.replace("  ", " ").strip() for c in codes}))

    return meta


def extract_basic_metadata(text: str) -> Dict[str, Any]:
    # Jurisdiction-aware extraction: KR, US, JP, then generic fallback
    kr_meta = _extract_kr_metadata(text)
    us_meta = _extract_us_metadata(text)
    jp_meta = _extract_jp_metadata(text)

    meta: Dict[str, Any] = {}
    meta.update(kr_meta)
    for k, v in us_meta.items():
        if k not in meta or meta.get(k) in (None, "", [], {}):
            meta[k] = v
    for k, v in jp_meta.items():
        if k not in meta or meta.get(k) in (None, "", [], {}):
            meta[k] = v

    # Generic fallback for any remaining keys
    for k, pat in META_PATTERNS.items():
        if k in meta and meta[k] not in (None, "", [], {}):
            continue
        m = re.search(pat, text, flags=re.I)
        if m:
            meta[k] = m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1)

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
    def _page_text_robust(page) -> str:
        # 1) basic
        txt = page.extract_text() or ""
        if txt and txt.strip():
            return txt

        # 2) word-based reconstruction (handles some vector-text PDFs that fail extract_text)
        try:
            words = page.extract_words() or []
        except Exception:
            words = []
        if words:
            # group by line using 'top' with small tolerance
            words_sorted = sorted(words, key=lambda w: (round(float(w.get("top", 0)) / 2) * 2, float(w.get("x0", 0))))
            lines: List[List[str]] = []
            cur_top: Optional[float] = None
            cur_line: List[str] = []
            for w in words_sorted:
                top = round(float(w.get("top", 0)) / 2) * 2
                if cur_top is None:
                    cur_top = top
                if abs(top - cur_top) <= 2:
                    cur_line.append(str(w.get("text", "")))
                else:
                    if cur_line:
                        lines.append(cur_line)
                    cur_top = top
                    cur_line = [str(w.get("text", ""))]
            if cur_line:
                lines.append(cur_line)
            return "\n".join(" ".join(line) for line in lines)

        # 3) nothing found
        return ""

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        texts: List[str] = []
        for p in pdf.pages:
            try:
                txt = _page_text_robust(p)
            except Exception:
                txt = ""
            texts.append(txt or "")
        full_text = "\n".join(texts)

    # OCR fallback if text is very sparse
    def _needs_ocr(pages_text: List[str]) -> bool:
        total_chars = sum(len(t) for t in pages_text)
        nonempty_pages = sum(1 for t in pages_text if len(t.strip()) > 10)
        # Heuristics: if average chars per page is tiny or majority pages empty
        if len(pages_text) >= 2 and (total_chars / max(1, len(pages_text)) < 200 or nonempty_pages <= len(pages_text) // 3):
            return True
        return total_chars < 400

    if _needs_ocr(texts) and convert_from_bytes is not None and pytesseract is not None:
        try:
            images = convert_from_bytes(file_bytes, dpi=300, fmt="png")
            ocr_texts: List[str] = []
            for img in images:
                # Use English + Korean if available; pytesseract falls back if not installed
                try:
                    ocr_txt = pytesseract.image_to_string(img, lang="eng+kor+jpn")
                except Exception:
                    ocr_txt = pytesseract.image_to_string(img)
                ocr_texts.append(ocr_txt or "")
            if sum(len(t) for t in ocr_texts) > sum(len(t) for t in texts):
                texts = ocr_texts
                full_text = "\n".join(texts)
        except Exception:
            # keep original texts if OCR fails
            pass

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
    # Build lightweight section index for doc-level navigation
    # Map from section type to range of chunk indices for fast slicing in UI/RAG
    sections_index: List[Dict[str, Any]] = []
    sec_to_range: Dict[str, Tuple[int, int]] = {}
    # compute via chunk order
    start_idx: Dict[str, int] = {}
    end_idx: Dict[str, int] = {}
    for idx, ch in enumerate(chunks):
        st = ch.get("section_type", "UNKNOWN")
        if st not in start_idx:
            start_idx[st] = idx
        end_idx[st] = idx
    for st in sorted(start_idx.keys(), key=lambda s: start_idx[s]):
        rng = {"type": st, "start_chunk": start_idx[st], "end_chunk": end_idx[st], "title": ""}
        sections_index.append(rng)

    document: Dict[str, Any] = {
        "doc_id": doc_id,
        "file_name": file_name,
        "metadata": meta,
        "structure": {
            "sections_index": sections_index,
            "claims_count": len(claims),
        },
        # Keep raw sections and claims for traceability if needed by downstream
        "sections": sections,
        "claims": claims,
    }

    # Annotate chunks for downstream prompt: DocumentId/CitationPage/Context
    _annotate_chunks_for_prompt(
        chunks,
        per_page_texts=texts,
        document_id=doc_id,
        publication_number=meta.get("publication_number") if isinstance(meta, dict) else None,
    )

    return document, chunks


def chunks_to_jsonl(chunks: List[Dict[str, Any]]) -> str:
    return "\n".join(json.dumps(c, ensure_ascii=False) for c in chunks)

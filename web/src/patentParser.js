// Basic text utilities
export function cleanText(text) {
  let s = text.replace(/\u00AD/g, '');
  s = s.replace(/-\n(?=[a-z])/g, '');
  s = s.replace(/\n{2,}/g, '\n\n');
  return s.trim();
}

export const SECTION_HEADERS = [
  ['ABSTRACT', /\babstract\b|요약/i],
  ['CLAIMS', /\bclaims?\b|청구항/i],
  [
    'DESCRIPTION',
    /\b(description|detailed description|specification)\b|설명서|상세한 설명/i,
  ],
  ['BACKGROUND', /\b(background|field of the invention)\b|배경/i],
  ['SUMMARY', /\bsummary\b|요약(서)?/i],
  ['DRAWINGS', /\bbrief description of the drawings\b|도면의 간단한 설명/i],
];

export function splitSections(fullText) {
  const text = cleanText(fullText);
  const lines = text.split(/\r?\n/);
  const sections = [];
  let current = { type: 'UNKNOWN', title: '', text: [] };
  const flush = () => {
    if (current.text.length) {
      sections.push({
        type: current.type,
        title: current.title,
        text: current.text.join('\n').trim(),
      });
    }
  };
  for (const ln of lines) {
    const low = ln.toLowerCase().trim();
    let matched = null;
    for (const [t, pat] of SECTION_HEADERS) {
      if (pat.test(low)) {
        matched = [t, ln.trim()];
        break;
      }
    }
    if (matched) {
      flush();
      current = { type: matched[0], title: matched[1], text: [] };
    } else {
      current.text.push(ln);
    }
  }
  flush();
  return sections;
}

const CLAIM_LINE = /^\s*(?:claim\s*)?(\d+)[\.\)]\s+/i;
const KO_CLAIM_LINE = /^\s*청구항\s*(\d+)\s*[\.）)]?\s*/;

export function extractClaims(claimsText) {
  const claims = [];
  let cur = null;
  for (const ln of claimsText.split(/\r?\n/)) {
    const m = ln.match(CLAIM_LINE) || ln.match(KO_CLAIM_LINE);
    if (m) {
      if (cur) claims.push(cur);
      cur = { num: parseInt(m[1], 10), text: ln.slice(m[0].length).trim() };
    } else if (cur) {
      cur.text += '\n' + ln.trim();
    }
  }
  if (cur) claims.push(cur);

  for (const c of claims) {
    const refs = [...c.text.matchAll(/(?:claim|제)\s*([0-9]+)/gi)].map((m) =>
      parseInt(m[1], 10),
    );
    c.dependencies = Array.from(new Set(refs.filter((x) => x !== c.num))).sort(
      (a, b) => a - b,
    );
  }
  return claims;
}

function paragraphs(text) {
  return text
    .split(/\n\s*\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function slidingSplit(text, targetTokens, overlapTokens) {
  const charTarget = Math.max(1, targetTokens * 4);
  let charOverlap = Math.max(0, overlapTokens * 4);
  if (charOverlap >= charTarget) {
    charOverlap = Math.floor(charTarget / 10); // cap overlap to < target
  }
  const step = Math.max(1, charTarget - charOverlap);
  const out = [];
  let i = 0;
  while (i < text.length) {
    const j = Math.min(text.length, i + charTarget);
    out.push(text.slice(i, j));
    i += step;
  }
  return out;
}

export function chunkForRag(
  sections,
  claims,
  { targetTokens = 600, overlapTokens = 80 } = {},
) {
  const tlen = (s) => Math.max(1, Math.floor(s.length / 4));
  const chunks = [];

  for (const c of claims) {
    const txt = (c.text || '').trim();
    if (tlen(txt) <= Math.floor(targetTokens * 1.5)) {
      chunks.push({
        text: txt,
        section_type: 'CLAIMS',
        claim_nums: [c.num],
        page_range: null,
      });
    } else {
      for (const p of slidingSplit(txt, targetTokens, overlapTokens)) {
        chunks.push({
          text: p,
          section_type: 'CLAIMS',
          claim_nums: [c.num],
          page_range: null,
        });
      }
    }
  }

  for (const s of sections) {
    if (s.type === 'CLAIMS') continue;
    for (const p of paragraphs(s.text || '')) {
      if (tlen(p) <= targetTokens) {
        chunks.push({
          text: p,
          section_type: s.type,
          claim_nums: [],
          page_range: null,
        });
      } else {
        for (const sp of slidingSplit(p, targetTokens, overlapTokens)) {
          chunks.push({
            text: sp,
            section_type: s.type,
            claim_nums: [],
            page_range: null,
          });
        }
      }
    }
  }

  // ids and token estimate
  return chunks.map((ch, i) => {
    const h = cryptoRandomId(16);
    return {
      ...ch,
      chunk_id: `c${String(i).padStart(6, '0')}_${h}`,
      tokens_est: Math.floor((ch.text || '').length / 4),
    };
  });
}

function cryptoRandomId(n) {
  const bytes = new Uint8Array(n);
  (globalThis.crypto || window.crypto).getRandomValues(bytes);
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
    .slice(0, n);
}

export function extractBasicMetadata(text) {
  const meta = {
    jurisdiction: null,
    publication_number: null,
    application_number: null,
    title: null,
    abstract: null,
    assignee: null,
    inventors: null,
    publication_date: null,
    application_date: null,
    priority_numbers: null,
    ipc_codes: null,
    cpc_codes: null,
  };
  const patterns = {
    publication_number:
      /(PUB\s*NO\.?|publication\s*number)[:\s]*([A-Z]{2}\d+[A-Z]?\d*)/i,
    application_number:
      /(application\s*number|app\s*no\.?|appl\.?\s*no\.?)[:\s]*([A-Z]{2}\d+[A-Z]?\d*)/i,
    ipc_codes: /\bIPC\b[:\s]*([A-Z0-9/;\s,]+)/i,
    cpc_codes: /\bCPC\b[:\s]*([A-Z0-9/;\s,]+)/i,
  };
  for (const [k, pat] of Object.entries(patterns)) {
    const m = text.match(pat);
    if (m) meta[k] = m[2] || m[1];
  }
  return meta;
}

export function buildPatentDoc(fileName, fullText, sections, claims) {
  const meta = extractBasicMetadata(fullText);
  const docId = cryptoRandomId(24);
  return {
    doc_id: docId,
    file_name: fileName,
    num_sections: sections.length,
    num_claims: claims.length,
    metadata: meta,
    sections,
    claims,
  };
}

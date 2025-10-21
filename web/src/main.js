import JSZip from 'jszip';
import * as pdfjsLib from 'pdfjs-dist';
import {
  splitSections,
  extractClaims,
  chunkForRag,
  buildPatentDoc,
  cleanText,
} from './patentParser.js';

// configure pdfjs worker
pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.js',
  import.meta.url,
).toString();

const els = {
  fileInput: document.getElementById('fileInput'),
  convertBtn: document.getElementById('convertBtn'),
  downloadZipBtn: document.getElementById('downloadZipBtn'),
  status: document.getElementById('status'),
  docPreview: document.getElementById('docPreview'),
  chunksPreview: document.getElementById('chunksPreview'),
};

let lastZipBlob = null;

els.convertBtn.addEventListener('click', async () => {
  const files = Array.from(els.fileInput.files || []);
  if (!files.length) {
    setStatus('PDF 파일을 선택하세요.');
    return;
  }

  els.convertBtn.disabled = true;
  els.downloadZipBtn.disabled = true;
  setStatus('처리 중…');

  try {
    const zip = new JSZip();
    const allChunks = [];

    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      setStatus(`${i + 1}/${files.length} 처리 중: ${file.name}`);
      const arrayBuf = await file.arrayBuffer();
      const { doc, chunks } = await convertOnePdf(arrayBuf, file.name);

      if (i === files.length - 1) {
        els.docPreview.textContent = JSON.stringify(doc, null, 2);
        els.chunksPreview.textContent = chunks
          .map((c) => JSON.stringify(c))
          .join('\n');
      }

      const base = file.name.replace(/\.pdf$/i, '');
      zip.file(`docs/${base}.patent.json`, JSON.stringify(doc, null, 2));
      allChunks.push(...chunks);
    }

    zip.file(
      `chunks/all.chunks.jsonl`,
      allChunks.map((c) => JSON.stringify(c)).join('\n'),
    );
    const blob = await zip.generateAsync({ type: 'blob' });
    lastZipBlob = blob;

    els.downloadZipBtn.disabled = false;
    setStatus('완료. ZIP 다운로드 버튼을 클릭하세요.');
  } catch (e) {
    console.error(e);
    setStatus('오류: ' + (e?.message || e));
  } finally {
    els.convertBtn.disabled = false;
  }
});

els.downloadZipBtn.addEventListener('click', () => {
  if (!lastZipBlob) return;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(lastZipBlob);
  a.download = 'patent_output.zip';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});

function setStatus(msg) {
  els.status.textContent = msg;
}

async function convertOnePdf(arrayBuf, fileName) {
  const pdf = await pdfjsLib.getDocument({ data: arrayBuf }).promise;
  let fullText = '';

  for (let p = 1; p <= pdf.numPages; p++) {
    setStatus(`${fileName} - ${p}/${pdf.numPages} 페이지 처리 중…`);
    const page = await pdf.getPage(p);
    const textContent = await page.getTextContent();
    const pageText = textItemsToString(textContent);
    fullText += (pageText || '') + '\n\n';
  }

  fullText = cleanText(fullText);
  const sections = splitSections(fullText);
  const claimsSection = sections.find((s) => s.type === 'CLAIMS');
  const claims = claimsSection ? extractClaims(claimsSection.text) : [];
  const chunks = chunkForRag(sections, claims, {
    targetTokens: 600,
    overlapTokens: 80,
  }).map((ch) => ({ ...ch }));
  const doc = buildPatentDoc(fileName, fullText, sections, claims);
  const docId = doc.doc_id;
  for (const ch of chunks) ch.doc_id = docId;
  return { doc, chunks };
}

function textItemsToString(textContent) {
  if (!textContent || !textContent.items) return '';
  return textContent.items
    .map((i) => i.str)
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim();
}

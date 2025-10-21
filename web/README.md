# Web (Static) - PDF → JSON (특허 RAG)

브라우저에서 작동하는 정적 앱입니다. 사용자는 특허 PDF만 업로드하면 변환됩니다.

## 빌드

```bash
cd web
npm ci
npm run build
# dist/ 폴더를 정적 호스팅(CloudFront, S3, Vercel Static, Netlify 등)
```

## 개발 서버

```bash
npm run dev
```

## 결과물

- 각 PDF에 대해 `docs/{파일명}.patent.json` 생성
- 모든 청크를 합친 `chunks/all.chunks.jsonl` 생성
- UI에서 ZIP으로 한번에 다운로드 가능

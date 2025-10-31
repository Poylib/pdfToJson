## PDF Crawling / WIPS 링크 실험 도구

엑셀(`.xlsx`) 파일의 `출원번호` 열에서 링크를 추출하고, WIPS 상세 페이지에서 본문(발명의 설명)과 상단 요약 메타데이터를 수집/미리보기/다운로드할 수 있는 간단한 Streamlit 앱입니다.

### 빠른 시작

```bash
# 1) 가상환경 생성 및 활성화 (macOS/Linux)
python3 -m venv .venv
source .venv/bin/activate

# 2) 의존성 설치
pip install -U pip
pip install -r requirements.txt

# 3) Playwright 브라우저 바이너리 설치(필수)
python -m playwright install chromium

# 4) 앱 실행
streamlit run app.py
```

브라우저가 자동으로 열리며, 열리지 않으면 출력되는 URL(예: `http://localhost:8501`)로 접속하세요.

### 사전 요구 사항

- Python 3.10 이상 권장
- macOS 또는 Linux (Windows도 동작 가능하나 명령은 다를 수 있음)
- 네트워크 접속 가능 환경 (WIPS 페이지 접근 필요)

### 실행 방법(상세)

1. 가상환경 준비 및 의존성 설치

   - `requirements.txt`에는 `streamlit`, `openpyxl`, `requests`, `playwright`가 포함되어 있습니다.
   - 설치 후 반드시 `python -m playwright install chromium`을 실행해 Chromium 바이너리를 내려받으세요.

2. 앱 실행
   - 아래 명령으로 앱을 실행합니다.
   ```bash
   streamlit run app.py
   ```

### 사용 방법

- 엑셀 업로드

  - 페이지 상단의 업로더에 `.xlsx` 파일을 업로드합니다.
  - 상위 50행에서 `출원번호` 열을 자동 탐색합니다. 자동 탐색이 실패하면 화면에서 수동으로 열을 선택할 수 있습니다.
  - 추출된 링크 목록을 표로 확인하고, JSONL로 다운로드할 수 있습니다.

- 발명의 설명(본문) 수집

  - 추출된 링크 중 임의 1건을 선택하거나, 직접 URL을 입력하여 수집을 실행합니다.
  - 언어별 탭으로 본문 HTML을 미리보기 합니다.

- 상단 요약 메타데이터(docSummaryInfo) 수집
  - 엔진을 선택할 수 있습니다.
    - 브라우저 렌더링(권장): Playwright Chromium으로 실제 렌더링 후 추출
    - 요청/정규식(빠름): HTML 요청과 정규식으로 추출
  - 결과를 표/JSON으로 확인하고 JSONL 파일로 다운로드할 수 있습니다.

### 포함 파일

- `app.py`: Streamlit 앱 진입점
- `requirements.txt`: 파이썬 의존성 목록
- `wips.xlsx`: 예시 엑셀(있다면 테스트용으로 업로드)
- `wips_meta_one.jsonl`: 예시/샘플 출력(있다면 구조 참고용)

### 문제 해결

- Playwright 미설치 오류가 발생하는 경우

  - 메시지에 안내된 대로 아래 명령을 실행하세요.

  ```bash
  python -m playwright install chromium
  ```

- macOS에서 권한 또는 보안 경고로 브라우저가 실행되지 않을 때

  - 보안 및 개인정보 보호 설정에서 차단 해제 후 다시 실행하거나, 터미널에서 위 설치 명령을 재실행하세요.

- 네트워크/접속 문제
  - 사내 프록시 환경이라면 `HTTP_PROXY`/`HTTPS_PROXY` 환경변수를 설정해야 할 수 있습니다.

### 라이선스

내부 실험/데모 용도로 제작되었습니다. 외부 배포 전 사내 정책을 확인하세요.

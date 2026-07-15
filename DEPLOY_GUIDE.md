# 위단테 실전 배포 가이드 — 어디서, 무엇을, 어떻게

이 문서 하나만 순서대로 따라 하면 www.wedante.com 이 실제로 열립니다.
필요한 계정: **GitHub, Render.com, 도메인 관리 페이지(도메인 산 곳), 알리고(문자)**. 넷 다 무료 가입 가능합니다 (Render 유료 플랜은 선택사항).

---

## 0단계. 지금 갖고 있어야 하는 것
- [ ] `wedante.zip` 압축을 풀어놓은 폴더
- [ ] www.wedante.com 도메인 (이미 갖고 계신 것으로 알고 있습니다 — 어디서 구매하셨는지에 따라 4단계 화면이 조금 다릅니다: 가비아/후이즈/코리아센터/GoDaddy 등)

---

## 1단계. 코드를 GitHub에 올리기 (Render가 여기서 코드를 가져갑니다)

1. https://github.com 가입 (이미 있으면 생략)
2. 오른쪽 위 `+` → **New repository** → 이름 예: `wedante` → Create
3. 내 컴퓨터에서 압축 푼 `wedante` 폴더 안에서:
   ```bash
   cd wedante
   git init
   git add .
   git commit -m "위단테 첫 배포"
   git branch -M main
   git remote add origin https://github.com/<내계정>/wedante.git
   git push -u origin main
   ```
   (git이 없으면 https://git-scm.com 에서 설치, 또는 GitHub Desktop 앱으로 마우스 클릭만으로도 가능합니다)

---

## 2단계. Render.com 에서 PostgreSQL 만들기 (API/DB 서버)

1. https://render.com 가입 → GitHub 계정으로 로그인하면 편합니다
2. 대시보드에서 **New +** → **PostgreSQL**
3. Name: `wedante-db` 입력 → 나머지 기본값 → **Create Database**
4. 생성되면 상세 페이지에 **Internal Database URL** 이라는 값이 보입니다 (`postgresql://...` 형태) → 이걸 복사해두세요. 이게 3단계에서 쓸 `DATABASE_URL` 입니다.

---

## 3단계. Render.com 에서 웹 서비스(실제 앱) 만들기

1. 대시보드 **New +** → **Web Service**
2. 1단계에서 올린 GitHub 저장소(`wedante`) 선택 → Connect
3. 아래 값 입력:
   - Name: `wedante`
   - Region: Singapore (한국에서 가장 가까움)
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn -k eventlet -w 1 app:app`
4. **Environment Variables** (여기가 "API 키 넣는 곳"입니다) → Add Environment Variable 을 눌러 아래를 하나씩 등록:

   | Key | Value | 어디서 구하나요 |
   |---|---|---|
   | `SECRET_KEY` | 아무 랜덤 문자열 (예: `wedante-2026-secret-x93kd`) | 직접 지어서 입력 |
   | `DATABASE_URL` | 2단계에서 복사한 Internal Database URL | Render PostgreSQL 페이지 |
   | `ENCRYPTION_KEY` | 아래 명령 실행 결과 값 | 터미널에서 생성 (아래 참고) |
   | `ALIGO_API_KEY` | 문자 발송용 키 | 5단계 참고 |
   | `ALIGO_USER_ID` | 문자 발송용 아이디 | 5단계 참고 |
   | `ALIGO_SENDER` | 사전등록한 발신번호 | 5단계 참고 |

   `ENCRYPTION_KEY` 생성 방법 (내 컴퓨터 터미널에서 한 번만 실행):
   ```bash
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   출력된 값을 그대로 복사해서 Render에 붙여넣으세요. **이 값은 절대 나중에 바꾸면 안 됩니다** (바꾸면 회원들의 환불계좌 정보를 다시 읽을 수 없게 됩니다). 어딘가에 백업해두세요.

5. **Create Web Service** 클릭 → 5분 정도 빌드/배포가 진행됩니다
6. 완료되면 화면 상단에 `wedante.onrender.com` 같은 주소가 생깁니다. 클릭해서 사이트가 뜨는지 확인하세요.

### 최초 관리자 계정 만들기
배포가 끝난 뒤, Render 웹서비스 페이지 → 상단 **Shell** 탭 클릭 → 아래 입력:
```bash
python seed_demo.py
```
이러면 관리자 계정(`01000000000` / `admin1234`)과 샘플 대회가 만들어집니다. 실제 운영할 땐 이 비밀번호를 로그인 후 꼭 바꾸세요 (지금 버전엔 비밀번호 변경 화면은 없어서, 필요하면 DB에서 직접 바꾸거나 저에게 "비밀번호 변경 기능도 만들어줘"라고 요청하시면 추가해드릴 수 있습니다).

---

## 4단계. www.wedante.com 도메인 연결하기

1. Render 웹서비스 페이지 → **Settings** → **Custom Domains** → **Add Custom Domain**
2. `www.wedante.com` 입력 → Save
3. Render가 이런 식으로 알려줍니다: *"이 CNAME 값을 DNS에 등록하세요"* → 그 값을 복사
4. 도메인을 구매한 사이트(가비아/후이즈/코리아센터 등)에 로그인 → **DNS 관리** 또는 **네임서버 설정** 메뉴로 이동
5. 레코드 추가:
   - 호스트: `www` / 타입: `CNAME` / 값: Render가 알려준 주소
   - 루트 도메인(`wedante.com`, www 없이)도 쓰고 싶다면: 타입 `A` 또는 `ALIAS` 로 Render가 안내하는 IP 등록
6. 저장 후 보통 10분~수 시간 내 반영됩니다 (DNS 전파 시간)
7. Render Custom Domains 화면에서 상태가 "Verified"로 바뀌면 SSL(자물쇠, https)도 자동으로 발급됩니다

이제 브라우저에서 **www.wedante.com** 치면 위단테 사이트가 뜹니다.

---

## 5단계. 문자(SMS) API 실제 연동하기

지금은 문자 API 키가 없으면 화면에는 "발송성공"이라고 뜨지만 실제로는 서버 로그에만 출력되고 문자가 안 나갑니다. 진짜로 문자를 보내려면:

1. https://smartsms.aligo.in 접속 → 회원가입
2. **발신번호 등록** 메뉴에서 실제 문자 보낼 번호(예: 010-6890-9123) 등록 → 본인인증 서류 제출 필요 (통신사 가입증명원 등, 보통 1일 내 승인)
3. 승인 후 **마이페이지 → API 정보** 에서 `API KEY` 와 `USER ID` 확인
4. 3단계에서 만든 Render 환경변수에 `ALIGO_API_KEY`, `ALIGO_USER_ID`, `ALIGO_SENDER`(승인받은 발신번호) 입력 후 저장 → Render가 자동으로 재배포합니다
5. 알리고는 건당 과금(문자 1건에 약 9~20원, 발송량에 따라 상이)이라 미리 충전해두셔야 합니다 (마이페이지 → 충전)

---

## 정리 — "API" 라고 부를 수 있는 것들
헷갈리실 수 있어 정리하면, 이 프로젝트에서 "API"는 세 가지 의미로 쓰입니다:

1. **위단테 자체 API** (제가 이미 만든 것) — 예: `/api/tournaments/1/draw` 처럼, 앱 안에서 대진표/콜/문자 기능이 서로 통신하는 내부 주소들. 이건 이미 코드에 다 들어있어서 따로 신청할 필요 없습니다.
2. **알리고 문자 API** — 문자를 실제로 발송하려면 필요한 외부 서비스. 위 5단계에서 키를 발급받아 넣으면 됩니다.
3. **PostgreSQL 연결 정보(DATABASE_URL)** — 엄밀히는 API키는 아니지만 "이 값을 넣어야 작동하는 비밀값"이라는 점에서 비슷하게 다뤄야 합니다 (Render가 자동 발급).

---

## 문제가 생기면
- 배포 로그는 Render 웹서비스 페이지 → **Logs** 탭에서 실시간으로 볼 수 있습니다. 에러 메시지가 뜨면 그 내용을 저에게 붙여넣어 주시면 바로 원인을 찾아드릴 수 있습니다.
- 도메인이 반영이 안 되면 https://dnschecker.org 에서 `www.wedante.com` 검색해 전세계 DNS 반영 상태를 확인할 수 있습니다.

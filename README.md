# 위단테 (Wedante) — 실시간 테니스 대진표 웹앱

기존 구글 사이트(스프레드시트+수기 문자보고) 운영 방식을 대체하는 실제 웹 애플리케이션입니다.
DB는 **PostgreSQL**을 사용하고, 환불계좌 등 민감정보는 **암호화 저장**됩니다.

## 구현된 기능
1. **예선/본선 대진표 실시간 운영** — Flask-SocketIO로 스코어 입력 즉시 모든 접속자 화면에 반영 (예선/본선 모두)
2. **예선 → 본선 자동 반영** — 조별 예선 우승자를 확정하면 본선 대진표 슬롯에 자동 배정
3. **회원가입** — 실명·전화번호·지역·소속클럽·환불계좌 저장. 환불계좌(은행/번호)는 **Fernet 암호화**로 DB에 저장되고, 조회 시에만 복호화됩니다.
4. **문자(SMS) 발송** — 관리자가 체크한 참가자에게 예선 안내 문자 일괄 발송 (`sms.py`, 알리고 API 연동)
5. **주최자 콜 기능** — 로그인한 선수가 버튼 한 번으로 라인시비/스코어시비/공교체 등을 실시간으로 주최자에게 전송
6. **멀티테넌트** — 관리자 화면에서 "다른 주최자 카테고리 만들기"로 새 테넌트(`/org/<slug>`)와 그 주최자의 관리자 계정을 함께 생성. 각 주최자는 로그인 후 자기 대회만 관리
7. **대회 생성 UI** — 관리자 화면에서 대회명/장소/날짜/참가비/예선 조 인원을 입력해 대회를 직접 생성 (더 이상 스크립트로 DB에 직접 넣을 필요 없음)
8. **예선 경기별 스코어 입력** — 조별 라운드로빈 개별 경기 결과도 관리자 화면에서 입력 가능

## 폴더 구조
```
wedante/
  app.py                 # 라우트 + SocketIO 이벤트 + 대진표 로직
  models.py               # DB 모델 (Organizer/User/Tournament/Match/CallLog/SmsLog)
  crypto_utils.py          # 환불계좌 등 민감정보 암호화/복호화 (Fernet)
  sms.py                    # 알리고 문자 발송 모듈
  seed_demo.py               # 데모용 샘플 데이터 생성 스크립트
  docker-compose.yml          # 로컬 PostgreSQL 실행용
  templates/                   # Jinja2 HTML 템플릿
  static/css/style.css          # 디자인 (코트그린/클레이 팔레트)
  requirements.txt
  .env.example
```

## 1. 로컬에서 실행하기

### 1) PostgreSQL 준비
Docker가 있다면 가장 간단합니다:
```bash
cd wedante
docker compose up -d          # localhost:5432 에 postgres/wedante DB 기동
```
Docker가 없다면 로컬에 PostgreSQL을 설치하고 `.env.example`과 동일한 이름의 DB/계정을 만들어도 됩니다.
(참고: DB가 전혀 준비되지 않았다면 `.env`의 `DATABASE_URL`을 `sqlite:///wedante.db`로 바꿔 SQLite로 우선 테스트할 수도 있습니다.)

### 2) 앱 실행
```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# ENCRYPTION_KEY 생성 후 .env에 채우기 (아래 명령 실행 후 나온 값을 복사)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

python seed_demo.py                # 테이블 생성 + 샘플 대회/선수/관리자 계정 생성
python app.py                      # http://localhost:5000
```

샘플 관리자 로그인: `01000000000` / `admin1234`
샘플 선수 로그인: 시드 스크립트에 있는 전화번호(`0101000000X`) / `1234`

### 3) 관리자 화면 사용법
1. 관리자로 로그인 → 상단 "주최자 메뉴" (`/admin`)
2. "+ 새 대회 만들기" 로 대회 생성
3. 대회 상세 관리 화면에서:
   - 참가자 체크 → "예선 대진표 생성" (조별 자동 편성 + 본선 빈 대진표 자동 생성)
   - 조별 개별 경기 스코어 입력
   - 조별 우승자 지정 → "확정" 누르면 본선에 자동 반영
   - 본선 스코어 입력 → 다음 라운드로 자동 진출
   - 참가자 체크 → 문자 발송
   - 실시간 콜 현황에서 선수가 보낸 콜을 확인/처리완료 처리
4. "+ 다른 주최자 카테고리 만들기" 로 타 주최자용 `/org/<slug>` 페이지와 그 주최자의 관리자 계정을 함께 생성

## 2. www.wedante.com 에 실제로 배포하기

### 방법 A — PaaS (가장 쉬움, 추천)
Render, Railway, Fly.io 같은 서비스에 GitHub 저장소를 연결하면 서버 관리 없이 배포됩니다.

1. 이 폴더를 GitHub 저장소로 push
2. Render.com 기준:
   - New → PostgreSQL 생성 (무료 플랜 가능) → 자동 발급되는 `Internal Database URL` 확인
   - New → Web Service → 저장소 선택
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn -k eventlet -w 1 app:app`
   - Environment 탭에서 `SECRET_KEY`, `DATABASE_URL`(위에서 발급받은 값), `ENCRYPTION_KEY`, `ALIGO_*` 등록
   - 최초 배포 후 Render Shell에서 `python seed_demo.py` 한 번 실행 (또는 관리자 계정만 별도 생성하는 초기화 스크립트 실행)
3. 배포 후 Render가 주는 `xxx.onrender.com` 주소 확인
4. **커스텀 도메인 연결**: Render 대시보드 → Settings → Custom Domain → `www.wedante.com` 입력
   → 표시되는 CNAME 값을 도메인 구매처(가비아/카페24 등)의 DNS 설정에 등록
   - `www` 레코드: CNAME → Render가 알려주는 주소
   - 루트 도메인(`wedante.com`)은 A 레코드 또는 ALIAS로 Render IP 연결
5. SSL 인증서는 Render/Railway가 자동 발급 (Let's Encrypt)

### 방법 B — 직접 서버(VPS) 운영 (더 유연, 관리 필요)
네이버클라우드/AWS Lightsail/Vultr 등에서 Ubuntu 서버를 하나 띄운 뒤:

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv nginx certbot python3-certbot-nginx postgresql
sudo -u postgres createuser wedante -P     # 비밀번호 입력 (환경변수 값과 동일하게)
sudo -u postgres createdb wedante -O wedante

git clone <저장소> /var/www/wedante && cd /var/www/wedante
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # DATABASE_URL, ENCRYPTION_KEY, SECRET_KEY, ALIGO_* 값 채우기
python seed_demo.py    # 최초 1회, 관리자 계정 생성용

# Gunicorn + eventlet 워커로 실행 (systemd 서비스로 등록해 항상 켜두기)
gunicorn -k eventlet -w 1 -b 127.0.0.1:8000 app:app
```

Nginx 리버스 프록시 설정 (`/etc/nginx/sites-available/wedante`):
```
server {
    listen 80;
    server_name www.wedante.com wedante.com;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;   # SocketIO(websocket) 필수
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
```
```bash
sudo ln -s /etc/nginx/sites-available/wedante /etc/nginx/sites-enabled/
sudo certbot --nginx -d www.wedante.com -d wedante.com   # 무료 SSL 발급
sudo systemctl restart nginx
```

도메인 등록처(가비아 등)에서 A 레코드를 서버의 공인 IP로 지정하면 됩니다.

## 3. 문자(SMS) 실제 발송 연동
현재 `sms.py`는 API 키가 없으면 콘솔에만 출력하는 개발 모드입니다. 실제 발송하려면:
1. https://smartsms.aligo.in 가입 → 발신번호 사전등록(본인인증)
2. 마이페이지에서 API KEY / User ID 발급
3. `.env`에 `ALIGO_API_KEY`, `ALIGO_USER_ID`, `ALIGO_SENDER` 입력
4. (선택) 카카오 알림톡으로 바꾸고 싶다면 `sms.py`의 `send_sms()` 함수만 해당 API 호출로 교체

## 4. 개인정보 암호화 관련 주의사항
- `.env`의 `ENCRYPTION_KEY`는 **한 번 정하면 절대 바꾸면 안 됩니다.** 키를 바꾸면 기존에 암호화 저장된 환불계좌 정보를 더 이상 복호화할 수 없습니다.
- 서버를 여러 대(다중 인스턴스)로 운영한다면 모든 인스턴스가 **동일한 ENCRYPTION_KEY**를 써야 합니다.
- 키 백업을 별도 안전한 곳(비밀번호 관리자, 볼트 서비스 등)에 보관하세요.

## 5. 남은 작업 / 운영 전 꼭 확인할 점
- **동시접속 다수 대회**: PostgreSQL로 전환했지만, 대회 규모가 매우 커지면 커넥션 풀 설정(`SQLALCHEMY_ENGINE_OPTIONS`)을 조정하는 것을 권장합니다.
- **인증 강화**: 현재 비밀번호 로그인만 구현했습니다. 문자 인증(OTP) 기반 로그인으로 바꾸면 더 간편합니다.
- **역할 분리**: 지금은 `is_admin` 한 종류의 권한만 있습니다. "위단테 운영자(모든 카테고리 관리)"와 "개별 주최자(자기 카테고리만 관리)"를 구분하려면 역할(role) 필드를 추가하는 것을 권장합니다. 지금은 로그인한 관리자 누구나 새 카테고리를 만들 수 있습니다.
- **문자 발송 비용**: 알리고 등 SMS API는 건당 과금입니다. 대회 규모에 맞게 요금제를 확인하세요.
- **개인정보처리방침**: 실제 서비스 오픈 전 개인정보처리방침 게시가 법적으로 필요합니다.

## 6. API 요약
| Method | Path | 설명|
|---|---|---|
| GET | `/admin` | 주최자 메뉴 (내 대회 목록 + 카테고리 목록) |
| GET/POST | `/admin/tournaments/new` | 새 대회 생성 |
| GET/POST | `/admin/organizers/new` | 새 주최자 카테고리(테넌트) + 관리자 계정 생성 |
| POST | `/register` | 회원가입 (환불계좌 암호화 저장) |
| POST | `/login` | 로그인 |
| POST | `/api/tournaments/<id>/draw/generate` | 예선 조편성 + 본선 빈 대진표 생성 (관리자) |
| POST | `/api/tournaments/<id>/groups/<gno>/winner` | 조 우승자 확정 → 본선 자동 반영 (관리자) |
| POST | `/api/matches/<id>/score` | 경기 스코어 입력 → 본선이면 다음 라운드 자동 진출 (관리자) |
| GET | `/api/tournaments/<id>/draw` | 현재 대진표 JSON (실시간 폴링/구독용) |
| POST | `/api/tournaments/<id>/call` | 선수 → 주최자 콜 전송 |
| POST | `/api/calls/<id>/resolve` | 콜 처리완료 처리 (관리자) |
| POST | `/api/tournaments/<id>/notify` | 선택 참가자에게 문자 발송 (관리자) |
| POST | `/api/organizers` | 새 주최자 카테고리 생성 (API, `/admin/organizers/new` 화면과 동일 기능) |


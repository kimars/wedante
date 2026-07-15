"""
위단테(Wedante) 데이터 모델
- Organizer : 대회 주최자(테넌트). 각 주최자는 고유 slug(URL 경로)를 가짐
- User      : 선수/회원. 실명, 전화번호, 지역, 소속클럽, 환불계좌 저장
- Tournament: 대회. 특정 Organizer 소속
- Match     : 대진표의 한 경기(예선/본선 공용). next_match_id 로 토너먼트 트리 연결
- CallLog   : 선수가 주최자에게 보내는 콜(라인시비/스코어시비/공교체 등)
- SmsLog    : 발송된 문자 기록
"""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from crypto_utils import encrypt_str, decrypt_str

db = SQLAlchemy()

CALL_TYPES = {
    "line": "라인 시비",
    "score": "스코어 시비",
    "ball": "공 교체 요청",
    "etc": "기타 문의",
}


class Organizer(db.Model):
    """타 주최자가 위단테를 사용할 때 만들어지는 개별 카테고리(테넌트)"""
    __tablename__ = "organizers"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)          # 예: "위단테", "OO테니스아카데미"
    slug = db.Column(db.String(80), unique=True, nullable=False)  # URL 경로 /org/<slug>
    contact_phone = db.Column(db.String(20))
    band_url = db.Column(db.String(300))       # 위단테 밴드 등 커뮤니티 링크
    youtube_url = db.Column(db.String(300))    # 유튜브 채널 링크
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    tournaments = db.relationship("Tournament", backref="organizer", lazy=True)
    users = db.relationship("User", backref="organizer", lazy=True)


class User(db.Model, UserMixin):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    organizer_id = db.Column(db.Integer, db.ForeignKey("organizers.id"), nullable=True)

    name = db.Column(db.String(50), nullable=False)            # 실명
    phone = db.Column(db.String(20), unique=True, nullable=False)
    region = db.Column(db.String(50))                          # 지역
    club = db.Column(db.String(80))                            # 소속클럽
    _refund_bank_enc = db.Column("refund_bank_enc", db.String(255))     # 암호화 저장
    _refund_account_enc = db.Column("refund_account_enc", db.String(255))  # 암호화 저장
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)            # 주최측 관리자 여부
    is_superadmin = db.Column(db.Boolean, default=False)       # 위단테 최상위 관리자(배너 등 전역설정)
    can_create_tournament = db.Column(db.Boolean, default=False)  # 최고 관리자가 부여하는 대회 생성 권한
    phone_verified = db.Column(db.Boolean, default=False)      # 전화번호 SMS 인증 여부
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    @property
    def refund_bank(self):
        return decrypt_str(self._refund_bank_enc)

    @refund_bank.setter
    def refund_bank(self, value):
        self._refund_bank_enc = encrypt_str(value)

    @property
    def refund_account(self):
        return decrypt_str(self._refund_account_enc)

    @refund_account.setter
    def refund_account(self, value):
        self._refund_account_enc = encrypt_str(value)


class Tournament(db.Model):
    __tablename__ = "tournaments"
    id = db.Column(db.Integer, primary_key=True)
    organizer_id = db.Column(db.Integer, db.ForeignKey("organizers.id"), nullable=False)

    title = db.Column(db.String(150), nullable=False)
    location = db.Column(db.String(150))
    event_date = db.Column(db.String(20))            # "2026-07-15"
    entry_fee = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default="접수중")  # 접수중 / 접수마감 / 진행중 / 종료
    group_size = db.Column(db.Integer, default=3)    # 예선 조 인원
    parking_url = db.Column(db.String(400))          # 주차등록 링크 (외부)
    deposit_bank = db.Column(db.String(40))          # 참가비 입금계좌 은행
    deposit_account = db.Column(db.String(40))       # 참가비 입금계좌 번호
    deposit_holder = db.Column(db.String(40))        # 예금주
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)  # 이 대회를 만든 관리자
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    matches = db.relationship("Match", backref="tournament", lazy=True,
                               cascade="all, delete-orphan")
    creator = db.relationship("User", foreign_keys=[created_by_id])


class TournamentParticipant(db.Model):
    """선수가 대회 페이지에서 '참가 신청' 버튼을 눌러 등록. 회원가입 시 입력한 정보를
    그대로 쓰기 때문에 매번 인적사항을 다시 적을 필요가 없다."""
    __tablename__ = "tournament_participants"
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournaments.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    # 대기 -> 입금확인요청(선수가 입금했다고 표시) -> 확정(관리자가 확인)  |  참가비 0원이면 바로 확정
    status = db.Column(db.String(20), default="대기")
    applied_at = db.Column(db.DateTime, default=datetime.utcnow)
    confirmed_at = db.Column(db.DateTime)

    tournament = db.relationship("Tournament")
    user = db.relationship("User")

    __table_args__ = (db.UniqueConstraint("tournament_id", "user_id", name="uq_tournament_user"),)


class RefundRequest(db.Model):
    """선수가 참가 취소/환불을 요청하면 생성. 주최자와 채팅으로 대화하며 처리한다."""
    __tablename__ = "refund_requests"
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournaments.id"), nullable=False)
    participant_id = db.Column(db.Integer, db.ForeignKey("tournament_participants.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(20), default="요청중")   # 요청중 / 처리중 / 완료 / 거절
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    tournament = db.relationship("Tournament")
    participant = db.relationship("TournamentParticipant")
    user = db.relationship("User")


class RefundMessage(db.Model):
    """환불 요청 채팅방의 메시지 한 개"""
    __tablename__ = "refund_messages"
    id = db.Column(db.Integer, primary_key=True)
    refund_request_id = db.Column(db.Integer, db.ForeignKey("refund_requests.id"), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    sender = db.relationship("User")


class Match(db.Model):
    """예선/본선 공용 경기 노드. stage='prelim' 이면 group_no 사용,
    stage='main' 이면 round_name/match_no 로 토너먼트 트리를 구성한다."""
    __tablename__ = "matches"
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournaments.id"), nullable=False)

    stage = db.Column(db.String(10), nullable=False)   # 'prelim' | 'main'
    group_no = db.Column(db.Integer)                    # 예선 조 번호 (prelim 전용)
    round_name = db.Column(db.String(20))                # '32강','16강','8강','4강','결승' (main 전용)
    match_no = db.Column(db.Integer)                     # 같은 라운드 내 경기 번호

    player1_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    player2_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    score = db.Column(db.String(50))                     # "6-3,6-4"
    winner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    court = db.Column(db.String(30))
    scheduled_time = db.Column(db.String(20))
    status = db.Column(db.String(20), default="시작전")   # 시작전 / 진행중 / 종료

    # 본선 1라운드 슬롯이 "몇 조 우승자 자리"인지 명시적으로 기록 (stage='main' 1라운드 전용)
    seed_group_no = db.Column(db.Integer, nullable=True)
    seed_slot = db.Column(db.Integer, nullable=True)   # 1 또는 2

    # 본선 자동 진출용: 이 경기의 승자가 올라갈 다음 경기와 슬롯(1 or 2)
    next_match_id = db.Column(db.Integer, db.ForeignKey("matches.id"), nullable=True)
    next_match_slot = db.Column(db.Integer, nullable=True)

    player1 = db.relationship("User", foreign_keys=[player1_id])
    player2 = db.relationship("User", foreign_keys=[player2_id])
    winner = db.relationship("User", foreign_keys=[winner_id])


class CallLog(db.Model):
    """로그인한 선수가 주최자에게 보내는 실시간 콜"""
    __tablename__ = "call_logs"
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournaments.id"), nullable=False)
    match_id = db.Column(db.Integer, db.ForeignKey("matches.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    call_type = db.Column(db.String(10), nullable=False)   # line/score/ball/etc
    court = db.Column(db.String(30))
    status = db.Column(db.String(20), default="대기")       # 대기 / 확인함 / 처리완료
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")


class SmsLog(db.Model):
    __tablename__ = "sms_logs"
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournaments.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    message = db.Column(db.Text)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    success = db.Column(db.Boolean, default=False)
    provider_response = db.Column(db.Text)


class PhoneVerification(db.Model):
    """회원가입 시 전화번호 SMS 인증용 임시 코드 저장."""
    __tablename__ = "phone_verifications"
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), nullable=False, index=True)
    code = db.Column(db.String(10), nullable=False)                # 6자리 숫자
    expires_at = db.Column(db.DateTime, nullable=False)            # 5분 유효
    verified = db.Column(db.Boolean, default=False)                # 성공적으로 검증됐는지
    attempt_count = db.Column(db.Integer, default=0)               # 오입력 5회 초과 시 무효
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Banner(db.Model):
    """홈 화면 상단 배너(캐러셀). 최상위 관리자(is_superadmin)만 등록/수정 가능."""
    __tablename__ = "banners"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    subtitle = db.Column(db.String(200))
    image_url = db.Column(db.Text)          # 없으면 그라디언트 배경 사용
    link_url = db.Column(db.Text)
    button_text = db.Column(db.String(40), default="자세히 보기")
    bg_from = db.Column(db.String(20), default="#0E9F8C")
    bg_to = db.Column(db.String(20), default="#0B7A6B")
    duration_seconds = db.Column(db.Integer, default=5)   # 이 배너를 몇 초간 보여줄지
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class QuickIcon(db.Model):
    """홈 화면 배너 아래 바로가기 아이콘 9개. 최상위 관리자만 등록/수정 가능.
    link_type='calendar' 이면 내장 대회 캘린더로 이동, 'external'이면 link_url로 이동."""
    __tablename__ = "quick_icons"
    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(40), nullable=False)
    icon_url = db.Column(db.Text)              # 업로드했거나 외부에서 가져온 아이콘 이미지 주소
    link_type = db.Column(db.String(20), default="external")   # external | calendar
    link_url = db.Column(db.Text)              # link_type == external 일 때 이동할 주소
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

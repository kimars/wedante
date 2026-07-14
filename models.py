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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    matches = db.relationship("Match", backref="tournament", lazy=True,
                               cascade="all, delete-orphan")


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

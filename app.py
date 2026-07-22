import os
import math
import random
import uuid
from datetime import datetime, date, timedelta, timezone
from calendar import Calendar

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, abort, session
from flask_login import (LoginManager, login_user, logout_user, login_required,
                          current_user, UserMixin)
from flask_socketio import SocketIO, emit, join_room
from sqlalchemy.orm import joinedload
from sqlalchemy import inspect as sqla_inspect, text, func
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

from models import db, Organizer, User, Tournament, TournamentParticipant, RefundRequest, RefundMessage, Match, CallLog, CallReply, SmsLog, Banner, QuickIcon, PhoneVerification, ScheduledSms, CALL_TYPES
from sms import send_sms

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
# 운영 기본값: PostgreSQL. 로컬에 Postgres가 없으면 .env에서 sqlite:///wedante.db 로 바꿔도 됩니다.
_db_url = os.environ.get(
    "DATABASE_URL", "postgresql://wedante:wedante@localhost:5432/wedante"
)
if _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+psycopg://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

UPLOAD_DIR = os.path.join(app.root_path, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_UPLOAD_EXT = {"png", "jpg", "jpeg", "gif", "webp", "svg"}

db.init_app(app)
socketio = SocketIO(app, cors_allowed_origins="*")

login_manager = LoginManager(app)
login_manager.login_view = "login"


def _apply_lightweight_migrations():
    """가벼운 인라인 마이그레이션.
    새로 추가된 컬럼을 기존 DB에 안전하게 ADD COLUMN 한다.
    (Postgres/SQLite 모두에서 idempotent 하게 동작하도록 컬럼 존재 여부를 먼저 확인)"""
    # 신규 컬럼 목록: (테이블, 컬럼명, 타입 스펠링)
    columns_to_add = [
        ("users", "can_create_tournament", "BOOLEAN DEFAULT FALSE"),
        ("users", "phone_verified", "BOOLEAN DEFAULT FALSE"),
        ("users", "handedness", "VARCHAR(10)"),
        ("users", "strengths", "TEXT"),
        ("users", "weaknesses", "TEXT"),
        ("users", "playstyle_public", "BOOLEAN DEFAULT TRUE"),
        ("tournaments", "created_by_id", "INTEGER"),
        ("tournaments", "winner_id", "INTEGER"),
        ("tournaments", "winner_pair", "VARCHAR(120)"),
        ("tournaments", "format", "VARCHAR(10) DEFAULT 'singles'"),
        ("tournaments", "start_time", "VARCHAR(10) DEFAULT '09:00'"),
        ("tournaments", "match_interval_min", "INTEGER DEFAULT 30"),
        ("tournaments", "groups_per_court", "INTEGER DEFAULT 2"),
        ("tournaments", "main_bracket_size", "INTEGER"),
        ("tournaments", "main_bracket_frame", "TEXT"),
        ("tournament_participants", "partner_id", "INTEGER"),
        ("call_logs", "message", "TEXT"),
        ("quick_icons", "icon_size", "INTEGER DEFAULT 64"),
        ("quick_icons", "icon_width", "INTEGER DEFAULT 64"),
        ("quick_icons", "icon_height", "INTEGER DEFAULT 64"),
        ("matches", "seq_in_group", "INTEGER"),
        ("matches", "court_seq", "INTEGER"),
        ("matches", "started_at", "TIMESTAMP"),
        ("matches", "duration_min", "INTEGER DEFAULT 20"),
        ("matches", "source_match_id", "INTEGER"),
        ("matches", "source_role", "VARCHAR(10)"),
        ("matches", "player1_partner_id", "INTEGER"),
        ("matches", "player2_partner_id", "INTEGER"),
        ("sms_logs", "created_at", "TIMESTAMP"),
    ]
    try:
        insp = sqla_inspect(db.engine)
        with db.engine.begin() as conn:
            for table, col, coltype in columns_to_add:
                if not insp.has_table(table):
                    continue
                existing = {c["name"] for c in insp.get_columns(table)}
                if col in existing:
                    continue
                # 컬럼 이름/타입은 상수만 사용하므로 SQL 인젝션 위험 없음
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {col} {coltype}'))
                app.logger.info(f"[migration] added column {table}.{col}")
    except Exception:
        # 마이그레이션은 실패해도 앱 부팅을 막지 않는다 (기록만 남김)
        app.logger.exception("lightweight migration failed")


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ---------------------------------------------------------------------------
# 권한 헬퍼
# ---------------------------------------------------------------------------

def can_manage_tournament(user, tournament):
    """이 사용자가 이 대회를 관리(수정/대진표/참가자/문자 등)할 수 있는가.
    - 최고 관리자는 모두 가능
    - 대회 생성자는 자신의 대회를 관리 가능
    - 예전 대회(created_by_id 없음)는 하위호환으로 같은 organizer 소속 admin 이면 허용"""
    if not user or not user.is_authenticated:
        return False
    if user.is_superadmin:
        return True
    if tournament.created_by_id and tournament.created_by_id == user.id:
        return True
    # 하위호환: 생성자 정보가 없는 예전 대회는 organizer 소속 admin 이 관리 가능
    if not tournament.created_by_id and user.is_admin and user.organizer_id == tournament.organizer_id:
        return True
    return False


def can_delete_tournament(user, tournament):
    """대회 삭제는 최고 관리자 전용."""
    return bool(user and user.is_authenticated and user.is_superadmin)


def mask_phone(phone: str) -> str:
    """공개 페이지에서 개인정보 보호를 위해 중간 자리 마스킹.
    01012345678 → 010-1***-**78"""
    if not phone:
        return ""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11:
        return f"{digits[0:3]}-{digits[3]}***-**{digits[9:11]}"
    if len(digits) == 10:
        return f"{digits[0:3]}-***-**{digits[8:10]}"
    return "***-****-****"


def mask_name(name: str) -> str:
    """공개 목록의 이름을 부분 마스킹. 홍길동 → 홍*동, 김철 → 김*"""
    if not name:
        return ""
    if len(name) <= 2:
        return name[0] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1]


# Jinja 템플릿에서 바로 사용할 수 있도록 등록
app.jinja_env.filters["mask_phone"] = mask_phone
app.jinja_env.filters["mask_name"] = mask_name


# ---------------------------------------------------------------------------
# 대진표 생성 로직
# ---------------------------------------------------------------------------

ROUND_NAMES_BY_SIZE = {
    2: ["결승"],
    4: ["준결승", "결승"],
    8: ["8강", "준결승", "결승"],
    16: ["16강", "8강", "준결승", "결승"],
    32: ["32강", "16강", "8강", "준결승", "결승"],
    64: ["64강", "32강", "16강", "8강", "준결승", "결승"],
    128: ["128강", "64강", "32강", "16강", "8강", "준결승", "결승"],
    256: ["256강", "128강", "64강", "32강", "16강", "8강", "준결승", "결승"],
}


def next_power_of_two(n):
    return 1 if n <= 1 else 2 ** math.ceil(math.log2(n))


def build_main_bracket(tournament_id, groups_count, qualifiers_per_group=2):
    """예선 각 조에서 qualifiers_per_group 명(팀)이 본선 진출한다고 가정하고 본선 트리 생성.
    - Tournament.main_bracket_size 가 지정되면 그 크기(16/32/64) 우선 사용
    - 없으면 자동 계산 (groups_count * qualifiers_per_group 을 2의 거듭제곱으로 올림)
    """
    t = Tournament.query.get(tournament_id)
    if t and t.main_bracket_size and t.main_bracket_size in (2, 4, 8, 16, 32, 64, 128, 256):
        size = t.main_bracket_size
    else:
        total_qualifiers = max(groups_count * qualifiers_per_group, 2)
        size = next_power_of_two(total_qualifiers)
    round_names = ROUND_NAMES_BY_SIZE.get(size, [f"{size}강"] + ["결승"])

    rounds = []
    num_matches_this_round = size // 2
    for r_idx, rname in enumerate(round_names):
        round_matches = []
        for m in range(num_matches_this_round):
            match = Match(tournament_id=tournament_id, stage="main",
                          round_name=rname, match_no=m + 1, status="시작전")
            db.session.add(match)
            round_matches.append(match)
        rounds.append(round_matches)
        num_matches_this_round //= 2
    db.session.flush()

    # 라운드끼리 연결
    for r in range(len(rounds) - 1):
        for i, match in enumerate(rounds[r]):
            nxt = rounds[r + 1][i // 2]
            match.next_match_id = nxt.id
            match.next_match_slot = (i % 2) + 1
    db.session.commit()
    return rounds[0]


def _first_round_main_matches(tournament_id):
    """본선 첫 라운드(가장 매치가 많은 라운드)의 매치들을 match_no 순으로 반환."""
    main = Match.query.filter_by(tournament_id=tournament_id, stage="main").all()
    if not main:
        return []
    by_round = {}
    for m in main:
        by_round.setdefault(m.round_name, []).append(m)
    first = max(by_round.values(), key=len)
    return sorted(first, key=lambda m: m.match_no)


def _resolve_bracket_slot(tournament, group_no, rank, groups_count, first_round_size):
    """조 g의 rank위(1|2)가 본선 첫 라운드에서 앉을 (match_index, slot) 반환.

    우선순위:
    1) tournament.main_bracket_frame 이 있으면 그걸 사용 (사용자 사전 정의)
       - 형식: {"size": N, "slots": [{"match_no": 1, "slot": 1, "group_no": 5, "rank": 1}, ...]}
       - "match_no"는 1-based, "slot"은 1 또는 2, "rank"는 1(조1위) 또는 2(조2위)
       - 매칭되는 항목이 없으면 자동 계산 fallback
    2) 자동 알고리즘: 조 g 1위 → 매치 g, 조 g 2위 → 매치 (g+1) slot2 (마지막 조는 첫 조와)"""
    # 1) 사전 정의 프레임 우선
    if tournament and tournament.main_bracket_frame:
        try:
            import json
            frame = json.loads(tournament.main_bracket_frame)
            for s in frame.get("slots", []):
                if int(s.get("group_no") or 0) == group_no and int(s.get("rank") or 0) == rank:
                    match_no = int(s["match_no"])
                    slot = int(s["slot"])
                    idx = match_no - 1
                    if 0 <= idx < first_round_size and slot in (1, 2):
                        return idx, slot
        except (ValueError, KeyError, TypeError):
            app.logger.exception("main_bracket_frame parse error - falling back to auto layout")
    # 2) 자동 알고리즘 fallback
    if rank == 1:
        idx = group_no - 1
    else:
        idx = group_no % groups_count if groups_count else 0
    slot = 1 if rank == 1 else 2
    if first_round_size > 0 and idx >= first_round_size:
        idx = idx % first_round_size
    return idx, slot


def _qualifier_slot(group_no, rank, groups_count, first_round_size):
    """[하위호환] 프레임 없는 경우의 자동 계산. tournament 컨텍스트 없이 호출."""
    return _resolve_bracket_slot(None, group_no, rank, groups_count, first_round_size)


def _set_group_qualifier_slot(tournament_id, group_no, rank, user_id, partner_id=None,
                                groups_count=None):
    """조 rank위 선수를 본선 첫 라운드 해당 슬롯에 배치. commit/emit은 호출자 책임."""
    first_round = _first_round_main_matches(tournament_id)
    if not first_round:
        return
    tournament = Tournament.query.get(tournament_id)
    if groups_count is None:
        groups_count = (db.session.query(func.count(func.distinct(Match.group_no)))
                        .filter(Match.tournament_id == tournament_id,
                                Match.stage == "prelim").scalar()) or len(first_round)
    idx, slot = _resolve_bracket_slot(tournament, group_no, rank, groups_count, len(first_round))
    if idx >= len(first_round):
        return
    slot_match = first_round[idx]
    if slot == 1:
        slot_match.player1_id = user_id
        slot_match.player1_partner_id = partner_id
    else:
        slot_match.player2_id = user_id
        slot_match.player2_partner_id = partner_id


# 하위호환: 기존 코드가 _set_group_winner_slot 을 부르면 1위로 처리
def _set_group_winner_slot(tournament_id, group_no, winner_id):
    _set_group_qualifier_slot(tournament_id, group_no, 1, winner_id)


def record_group_winner(tournament: Tournament, group_no: int, winner_id: int):
    """[하위호환] 조 1위 자동 반영. 요즘은 _finalize_prelim_group 이 1위+2위 함께 배치."""
    _set_group_qualifier_slot(tournament.id, group_no, 1, winner_id)
    db.session.commit()
    socketio.emit("bracket_update", {"tournament_id": tournament.id},
                   room=f"tournament_{tournament.id}")


def _assign_courts_and_schedule(tournament: Tournament, groups_count: int):
    """대회 예선 매치들에 코트와 시각을 자동 배정한다.

    tournament.groups_per_court 로 두 가지 프레임 선택:
    - 2 (기본, 참가자 많을 때): 1코트에 2개 조 = 총 6경기 교차 진행
      · 순서: A1, B1, A2, B2, A3, B3  (조1↔조2 번갈아, 한 조가 연속 경기 안 함)
    - 1 (참가자 적을 때): 1코트에 1개 조 = 3경기만 진행
      · 순서: A1, A2, A3

    시각은 tournament.start_time 부터 match_interval_min(기본 30분)씩 증가."""
    from datetime import datetime, timedelta

    interval = tournament.match_interval_min or 30
    gpc = tournament.groups_per_court or 2
    try:
        base = datetime.strptime(tournament.start_time or "09:00", "%H:%M")
    except ValueError:
        base = datetime.strptime("09:00", "%H:%M")

    court_no = 1
    max_seq = 3   # 조당 최대 경기 수 (3경기 방식)

    if gpc == 1:
        # === 1코트 = 1조 프레임 (참가자 적을 때) ===
        for group_no in range(1, groups_count + 1):
            court_seq = 0
            for si in range(1, max_seq + 1):
                m = (Match.query
                     .filter_by(tournament_id=tournament.id, stage="prelim",
                                 group_no=group_no, seq_in_group=si)
                     .first())
                if not m:
                    continue
                court_seq += 1
                m.court = str(court_no)
                m.court_seq = court_seq
                m.scheduled_time = (base + timedelta(minutes=interval * (court_seq - 1))).strftime("%H:%M")
            court_no += 1
        return

    # === 1코트 = 2조 프레임 (기본, 참가자 많을 때) ===
    for pair_start in range(1, groups_count + 1, 2):
        group_a = pair_start
        group_b = pair_start + 1 if pair_start + 1 <= groups_count else None
        court_seq = 0
        # A-B-A-B-A-B 순서 (조간 교차)
        for si in range(1, max_seq + 1):
            for g in (group_a, group_b):
                if not g:
                    continue
                m = (Match.query
                     .filter_by(tournament_id=tournament.id, stage="prelim",
                                 group_no=g, seq_in_group=si)
                     .first())
                if not m:
                    continue
                court_seq += 1
                m.court = str(court_no)
                m.court_seq = court_seq
                m.scheduled_time = (base + timedelta(minutes=interval * (court_seq - 1))).strftime("%H:%M")
        court_no += 1


KST = timezone(timedelta(hours=9))


def _kst_to_utc_naive(event_date: str, hhmm: str):
    """KST '2026-08-01' + '09:00' → UTC naive datetime (2026-07-31 24:00 UTC).
    파싱 실패 시 None."""
    try:
        local = datetime.strptime(f"{event_date} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        return local.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _schedule_pre_match_sms(tournament: Tournament):
    """대회 예선 매치들의 시작 20분 前 시각에 자동 SMS 발송을 DB에 예약.
    - DB에 저장되므로 서버 재시작/재배포에 안전하다.
    - 배경 워커(_sms_dispatcher_loop)가 30초마다 폴링하여 발송한다.
    - 재예약 시(대진표 재생성 등) 이 대회의 미발송 예약을 먼저 정리한다."""
    if not tournament.event_date:
        return
    # 재예약 시 이전 미발송 예약 삭제
    ScheduledSms.query.filter_by(tournament_id=tournament.id, sent=False).delete(synchronize_session=False)
    db.session.commit()

    matches = Match.query.filter_by(tournament_id=tournament.id, stage="prelim").all()
    now_utc = datetime.utcnow()
    scheduled_count = 0
    for m in matches:
        if not m.scheduled_time:
            continue
        match_utc = _kst_to_utc_naive(tournament.event_date, m.scheduled_time)
        if not match_utc:
            continue
        notify_utc = match_utc - timedelta(minutes=20)
        # 이미 지난 시각이면 skip
        if notify_utc <= now_utc:
            continue
        # 매치 참가자 수집 (단식 2명 / 복식 4명)
        ids = [pid for pid in [m.player1_id, m.player1_partner_id,
                                m.player2_id, m.player2_partner_id] if pid]
        if not ids:
            continue
        users = User.query.filter(User.id.in_(ids)).all()
        for u in users:
            msg = (f"[위단테] {tournament.title} {m.group_no}조 {m.seq_in_group}경기가 "
                   f"20분 뒤({m.scheduled_time})에 시작됩니다. "
                   f"{u.name}님 {m.court or '?'}코트로 준비해주세요.")
            db.session.add(ScheduledSms(
                tournament_id=tournament.id, match_id=m.id, user_id=u.id,
                phone=u.phone, message=msg, send_at=notify_utc,
            ))
            scheduled_count += 1
    db.session.commit()
    app.logger.info(f"[schedule] tournament {tournament.id}: {scheduled_count} SMS reservations added")


def _dispatch_due_sms(limit: int = 50) -> int:
    """DB에서 발송 시각이 지난 예약을 꺼내 실제로 SMS 발송.
    반환값: 발송 시도 수. 워커 루프에서 호출됨. 앱 컨텍스트 안에서 실행되어야 함."""
    now = datetime.utcnow()
    due = (ScheduledSms.query
           .filter(ScheduledSms.sent == False, ScheduledSms.send_at <= now)
           .order_by(ScheduledSms.send_at)
           .limit(limit).all())
    if not due:
        return 0
    for s in due:
        try:
            r = send_sms(s.phone, s.message)
            s.sent = True
            s.sent_at = datetime.utcnow()
            s.result = str(r)[:500]
            db.session.add(SmsLog(
                tournament_id=s.tournament_id, user_id=s.user_id,
                message=s.message,
                success=bool(r.get("success")),
                provider_response=str(r.get("raw"))[:500]
            ))
        except Exception as e:
            s.result = f"error: {e}"[:500]
            app.logger.exception(f"SMS 발송 실패 (예약 id={s.id})")
    db.session.commit()
    return len(due)


# SMS 발송 워커의 마지막 하트비트 (진단용)
_sms_worker_last_beat = None
_sms_worker_started = False


def _start_sms_dispatcher():
    """eventlet 워커 안에서 백그라운드 SMS 발송 루프를 시작한다.
    30초마다 만기된 예약을 확인하고 발송. 워커 재시작해도 DB 예약이 살아있어 재개된다."""
    global _sms_worker_started
    if _sms_worker_started:
        return
    try:
        import eventlet
    except ImportError:
        app.logger.warning("eventlet 미설치 - SMS 스케줄러 미가동 (예약된 SMS 발송 안 됨)")
        return

    def loop():
        global _sms_worker_last_beat
        while True:
            try:
                _sms_worker_last_beat = datetime.utcnow()
                with app.app_context():
                    _dispatch_due_sms()
            except Exception:
                app.logger.exception("SMS dispatcher loop error")
            eventlet.sleep(30)

    eventlet.spawn(loop)
    _sms_worker_started = True
    app.logger.info("SMS dispatcher started (30s polling)")


@app.route("/api/admin/sms/diagnose", methods=["GET"])
@login_required
def api_sms_diagnose():
    """관리자 SMS 발송 진단.
    - 환경변수 세팅 여부 (값은 마스킹)
    - 백그라운드 워커 상태
    - 최근 발송 로그
    - 대기 중인 예약 개수"""
    if not current_user.is_admin:
        abort(403)

    api_key = os.environ.get("SOLAPI_API_KEY", "")
    api_secret = os.environ.get("SOLAPI_API_SECRET", "")
    sender = os.environ.get("SOLAPI_SENDER", "")

    def _mask(v):
        if not v:
            return None
        if len(v) <= 6:
            return v[0] + "***"
        return v[:4] + "***" + v[-2:]

    # 워커 하트비트 (마지막으로 살아있었던 시각)
    worker_status = "not_started"
    worker_last_beat_sec_ago = None
    if _sms_worker_started:
        if _sms_worker_last_beat:
            elapsed = (datetime.utcnow() - _sms_worker_last_beat).total_seconds()
            worker_last_beat_sec_ago = int(elapsed)
            worker_status = "alive" if elapsed < 90 else "stalled"
        else:
            worker_status = "starting"

    # 최근 SMS 로그 10건
    logs = SmsLog.query.order_by(SmsLog.created_at.desc()).limit(10).all()
    recent_logs = [{
        "id": s.id,
        "created_at": s.created_at.strftime("%Y-%m-%d %H:%M:%S") if s.created_at else None,
        "message": (s.message or "")[:80],
        "success": s.success,
        "response": (s.provider_response or "")[:200],
    } for s in logs]

    # 예약 통계
    pending = ScheduledSms.query.filter_by(sent=False).count()
    sent = ScheduledSms.query.filter_by(sent=True).count()
    # 다음 발송 예정 시각
    next_scheduled = (ScheduledSms.query
                      .filter_by(sent=False)
                      .order_by(ScheduledSms.send_at).first())

    return jsonify({
        "solapi": {
            "api_key_set": bool(api_key),
            "api_key_masked": _mask(api_key),
            "api_secret_set": bool(api_secret),
            "api_secret_masked": _mask(api_secret),
            "sender": sender or None,
            "sender_valid_format": bool(sender and sender.replace("-", "").isdigit() and len(sender.replace("-", "")) in (10, 11)),
            "all_set": bool(api_key and api_secret and sender),
        },
        "worker": {
            "started": _sms_worker_started,
            "status": worker_status,
            "last_heartbeat_sec_ago": worker_last_beat_sec_ago,
        },
        "scheduled": {
            "pending": pending,
            "sent": sent,
            "next_send_at_utc": next_scheduled.send_at.isoformat() if next_scheduled else None,
            "next_send_at_kst": (
                next_scheduled.send_at.replace(tzinfo=timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M")
                if next_scheduled else None
            ),
        },
        "recent_logs": recent_logs,
        "server_now_utc": datetime.utcnow().isoformat(),
        "server_now_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/api/admin/sms/test", methods=["POST"])
@login_required
def api_sms_test():
    """관리자가 즉시 테스트 문자를 특정 번호로 발송해본다.
    실제 SOLAPI API 응답을 그대로 반환하므로 원인 파악에 사용."""
    if not current_user.is_admin:
        abort(403)
    phone = ((request.json or {}).get("phone") or "").strip()
    if not phone:
        return jsonify({"error": "전화번호를 입력해주세요."}), 400
    message = (request.json or {}).get("message") or f"[위단테] 테스트 문자입니다. ({datetime.now(KST).strftime('%H:%M:%S')})"
    r = send_sms(phone, message)
    # 로그도 남김 (실패해도 발송 결과는 정상 반환)
    try:
        db.session.add(SmsLog(
            tournament_id=None, user_id=current_user.id, message=message,
            success=bool(r.get("success")), provider_response=str(r.get("raw"))[:500]
        ))
        db.session.commit()
    except Exception as log_err:
        app.logger.warning(f"SMS 로그 저장 실패 (발송은 정상): {log_err}")
        db.session.rollback()
    return jsonify({
        "success": r.get("success"),
        "raw": r.get("raw"),
        "hint": _sms_error_hint(r),
    })


def _sms_error_hint(r):
    """SOLAPI 응답으로부터 원인 진단 힌트를 만든다. dev-mode 는 success=True 지만
    실제 발송 안 됐으므로 먼저 감지해서 별도 안내."""
    raw = r.get("raw")
    s = str(raw) if raw else ""
    if "dev-mode" in s:
        return "❗ Railway Variables에 SOLAPI_API_KEY / SOLAPI_API_SECRET / SOLAPI_SENDER 세 개를 등록하세요. 지금은 콘솔 로그만 되고 실제 발송이 안 됩니다."
    if r.get("success"):
        return "✅ SOLAPI 접수 성공 (실제 도달 안 되면 수신 번호가 잘못됐거나 통신사 스팸 필터에 걸린 것)"
    if "4013" in s:
        return "❗ statusCode 4013 = 발신번호 미등록. solapi 콘솔에서 발신번호 사전등록을 완료해야 합니다."
    if "1004" in s or "1005" in s:
        return "❗ API Key/Secret이 잘못됐거나 만료됐습니다. solapi 콘솔에서 재발급하세요."
    if "1015" in s or "잔액" in s or "balance" in s.lower():
        return "❗ 잔액 부족. solapi 콘솔에서 문자 충전이 필요합니다."
    if "network down" in s or "timeout" in s.lower():
        return "❗ 네트워크 오류. Railway → solapi.com 접근이 차단됐는지 확인하세요."
    return f"❗ 원인 미확인. solapi 문서(https://docs.solapi.com)에서 응답 코드 확인 필요: {s[:200]}"


@app.route("/game/tennis")
def game_tennis():
    """대회 대기 중 즐길 수 있는 테니스 미니 게임 (Pong 스타일)"""
    return render_template("game_tennis.html")


@app.route("/admin/sms")
@login_required
def admin_sms_diagnose_page():
    """SMS 발송 진단 페이지. 관리자만 접근."""
    if not current_user.is_admin:
        abort(403)
    return render_template("admin_sms.html")


# 관리자용 즉시 발송 트리거 (테스트/수동 배포 확인용)
@app.route("/api/tournaments/<int:tid>/sms/dispatch-now", methods=["POST"])
@login_required
def api_sms_dispatch_now(tid):
    """만기된 예약 SMS를 즉시 발송한다 (관리자 수동 트리거).
    보통은 백그라운드 워커가 알아서 발송하지만 강제로 시도할 때 사용."""
    t = Tournament.query.get_or_404(tid)
    if not can_manage_tournament(current_user, t):
        abort(403)
    count = _dispatch_due_sms(limit=200)
    return jsonify({"ok": True, "dispatched": count})


@app.route("/api/tournaments/<int:tid>/bracket-frame", methods=["GET"])
@login_required
def api_get_bracket_frame(tid):
    """대회의 본선 사전 배치 프레임 조회. 없으면 null."""
    t = Tournament.query.get_or_404(tid)
    if not can_manage_tournament(current_user, t):
        abort(403)
    if not t.main_bracket_frame:
        return jsonify({"frame": None})
    try:
        import json
        return jsonify({"frame": json.loads(t.main_bracket_frame)})
    except ValueError:
        return jsonify({"frame": None, "error": "저장된 프레임이 손상됨"}), 200


@app.route("/api/tournaments/<int:tid>/bracket-frame", methods=["POST"])
@login_required
def api_set_bracket_frame(tid):
    """대회 본선 프레임 저장. body 예시:
    {"size": 64, "slots": [
        {"match_no": 1, "slot": 1, "group_no": 5, "rank": 1},
        {"match_no": 1, "slot": 2, "group_no": null, "rank": null, "bye": true},
        ...
    ]}
    match_no: 1-based, slot: 1|2, rank: 1(조1위)|2(조2위)"""
    t = Tournament.query.get_or_404(tid)
    if not can_manage_tournament(current_user, t):
        abort(403)
    import json
    payload = request.json or {}
    slots = payload.get("slots", [])
    if not isinstance(slots, list):
        return jsonify({"error": "slots는 배열이어야 합니다."}), 400
    # 유효성 검사: 중복 슬롯, 중복 조/rank 쌍 확인
    seen_slots = set()
    seen_qualifiers = set()
    for s in slots:
        try:
            match_no = int(s["match_no"]); slot = int(s["slot"])
        except (KeyError, ValueError, TypeError):
            return jsonify({"error": f"잘못된 slot 항목: {s}"}), 400
        key = (match_no, slot)
        if key in seen_slots:
            return jsonify({"error": f"매치{match_no} slot{slot}이 중복됩니다."}), 400
        seen_slots.add(key)
        if not s.get("bye"):
            try:
                gno = int(s.get("group_no") or 0)
                rk = int(s.get("rank") or 0)
            except (ValueError, TypeError):
                gno, rk = 0, 0
            if gno and rk in (1, 2):
                q = (gno, rk)
                if q in seen_qualifiers:
                    return jsonify({"error": f"{gno}조 {rk}위가 중복 배치됐습니다."}), 400
                seen_qualifiers.add(q)
    t.main_bracket_frame = json.dumps(payload, ensure_ascii=False)
    db.session.commit()
    return jsonify({"ok": True, "slot_count": len(slots)})


@app.route("/api/tournaments/<int:tid>/bracket-frame/reapply", methods=["POST"])
@login_required
def api_reapply_bracket_frame(tid):
    """이미 결정된 조 순위를 프레임 기준으로 재배치. 프레임 변경 후 호출."""
    t = Tournament.query.get_or_404(tid)
    if not can_manage_tournament(current_user, t):
        abort(403)
    # 각 조에 대해 finalize 재실행
    group_nos = [g[0] for g in (db.session.query(Match.group_no)
                                 .filter_by(tournament_id=tid, stage="prelim")
                                 .distinct().all()) if g[0]]
    reapplied = 0
    for gno in group_nos:
        if _finalize_prelim_group(tid, gno):
            reapplied += 1
    db.session.commit()
    socketio.emit("bracket_update", {"tournament_id": tid},
                   room=f"tournament_{tid}")
    return jsonify({"ok": True, "reapplied_groups": reapplied})


@app.route("/api/tournaments/<int:tid>/bracket-frame/default", methods=["GET"])
@login_required
def api_get_default_bracket_frame(tid):
    """현재 알고리즘에 기반한 자동 프레임을 반환. 관리자가 참조/수정 시작점으로 사용."""
    t = Tournament.query.get_or_404(tid)
    if not can_manage_tournament(current_user, t):
        abort(403)
    groups_count = (db.session.query(func.count(func.distinct(Match.group_no)))
                    .filter(Match.tournament_id == tid, Match.stage == "prelim").scalar()) or 0
    first_round = _first_round_main_matches(tid)
    if not first_round:
        return jsonify({"error": "본선 대진표가 아직 없습니다."}), 400
    size = next_power_of_two(max(groups_count * 2, 2))
    slots = []
    used = {(0, 0)}
    for gno in range(1, groups_count + 1):
        for rank in (1, 2):
            idx, slot = _resolve_bracket_slot(None, gno, rank, groups_count, len(first_round))
            slots.append({"match_no": idx + 1, "slot": slot, "group_no": gno, "rank": rank})
            used.add((idx + 1, slot))
    # 나머지는 BYE
    for mn in range(1, len(first_round) + 1):
        for sl in (1, 2):
            if (mn, sl) not in used:
                slots.append({"match_no": mn, "slot": sl, "group_no": None, "rank": None, "bye": True})
    return jsonify({
        "size": size,
        "slots": slots,
        "note": "이것은 자동 생성된 기본 프레임입니다. 사용자 정의 프레임으로 덮어쓰려면 이 형식대로 POST 하세요."
    })


@app.route("/api/tournaments/<int:tid>/sms/schedule", methods=["GET"])
@login_required
def api_sms_schedule(tid):
    """이 대회에 예약된 SMS 목록 (관리자 확인용)."""
    t = Tournament.query.get_or_404(tid)
    if not can_manage_tournament(current_user, t):
        abort(403)
    items = (ScheduledSms.query
             .filter_by(tournament_id=tid)
             .order_by(ScheduledSms.send_at)
             .all())
    return jsonify({
        "items": [{
            "id": s.id, "match_id": s.match_id, "phone": s.phone,
            "send_at_utc": s.send_at.isoformat() if s.send_at else None,
            "send_at_kst": (s.send_at.replace(tzinfo=timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M") if s.send_at else None),
            "sent": s.sent, "sent_at": s.sent_at.isoformat() if s.sent_at else None,
            "result": s.result,
        } for s in items]
    })


def generate_prelim_groups(tournament: Tournament, player_ids: list[int]):
    """참가자를 조로 나누고 예선 매치 생성 + 코트/시각 자동 배정 + 20분 전 SMS 예약.
    단식(singles): player_ids 는 유저 id 목록
    복식(doubles): player_ids 는 TournamentParticipant 의 user_id 목록. 각 participant 의
                    partner_id 를 조회해 팀 단위로 처리한다."""
    players = player_ids[:]
    random.shuffle(players)
    gsize = tournament.group_size or 3
    groups = [players[i:i + gsize] for i in range(0, len(players), gsize)]

    build_main_bracket(tournament.id, len(groups))

    for gi, group in enumerate(groups, start=1):
        _build_group_matches(tournament.id, gi, group, tournament.format)

    db.session.commit()
    _assign_courts_and_schedule(tournament, len(groups))
    db.session.commit()
    _schedule_pre_match_sms(tournament)
    return groups


def _build_group_matches(tournament_id: int, group_no: int, group: list[int], fmt: str = "singles"):
    """조 하나의 예선 매치들 생성. 복식이면 각 참가자의 파트너까지 매치에 저장한다."""
    # 복식이면 partner_id 미리 조회
    partner_map = {}
    if fmt == "doubles":
        parts = TournamentParticipant.query.filter(
            TournamentParticipant.tournament_id == tournament_id,
            TournamentParticipant.user_id.in_(group)
        ).all()
        partner_map = {p.user_id: p.partner_id for p in parts}

    def team(uid):
        return uid, partner_map.get(uid) if fmt == "doubles" else None

    if len(group) == 1:
        _set_group_winner_slot(tournament_id, group_no, group[0])
        return

    if len(group) == 3:
        u1, u2, u3 = group
        p1a, p1b = team(u1)
        p2a, p2b = team(u2)
        p3a, p3b = team(u3)
        m1 = Match(tournament_id=tournament_id, stage="prelim", group_no=group_no,
                    seq_in_group=1, player1_id=p1a, player1_partner_id=p1b,
                    player2_id=p2a, player2_partner_id=p2b,
                    status="시작전", duration_min=20)
        db.session.add(m1); db.session.flush()
        m2 = Match(tournament_id=tournament_id, stage="prelim", group_no=group_no,
                    seq_in_group=2, player1_id=None, player1_partner_id=None,
                    player2_id=p3a, player2_partner_id=p3b,
                    source_match_id=m1.id, source_role="winner",
                    status="시작전", duration_min=20)
        m3 = Match(tournament_id=tournament_id, stage="prelim", group_no=group_no,
                    seq_in_group=3, player1_id=None, player1_partner_id=None,
                    player2_id=p3a, player2_partner_id=p3b,
                    source_match_id=m1.id, source_role="loser",
                    status="시작전", duration_min=20)
        db.session.add_all([m2, m3])
        return

    if len(group) == 2:
        u1, u2 = group
        p1a, p1b = team(u1)
        p2a, p2b = team(u2)
        m = Match(tournament_id=tournament_id, stage="prelim", group_no=group_no,
                   seq_in_group=1, player1_id=p1a, player1_partner_id=p1b,
                   player2_id=p2a, player2_partner_id=p2b,
                   status="시작전", duration_min=20)
        db.session.add(m)
        return

    # 4명 이상 - 라운드로빈 fallback
    seq = 0
    for a in range(len(group)):
        for b in range(a + 1, len(group)):
            seq += 1
            pa_a, pa_b = team(group[a])
            pb_a, pb_b = team(group[b])
            m = Match(tournament_id=tournament_id, stage="prelim", group_no=group_no,
                       seq_in_group=seq, player1_id=pa_a, player1_partner_id=pa_b,
                       player2_id=pb_a, player2_partner_id=pb_b,
                       status="시작전", duration_min=20)
            db.session.add(m)


def generate_manual_groups(tournament: Tournament, manual_groups: list[list[int]]):
    """관리자가 조 구성을 직접 지정한 경우."""
    if not manual_groups:
        return []
    build_main_bracket(tournament.id, len(manual_groups))
    for gi, group in enumerate(manual_groups, start=1):
        _build_group_matches(tournament.id, gi, group, tournament.format)
    db.session.commit()
    _assign_courts_and_schedule(tournament, len(manual_groups))
    db.session.commit()
    _schedule_pre_match_sms(tournament)
    return manual_groups


def advance_winner(match: Match):
    """본선 경기 승자를 다음 라운드 매치에 자동으로 채워 넣는다"""
    if match.next_match_id and match.winner_id:
        nxt = Match.query.get(match.next_match_id)
        if match.next_match_slot == 1:
            nxt.player1_id = match.winner_id
        else:
            nxt.player2_id = match.winner_id
        db.session.commit()


# ---------------------------------------------------------------------------
# 페이지 라우트
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """홈 - 진행중 대회 (오늘 event_date) + 접수중 대회 를 각각 표시"""
    today_kst = datetime.now(KST).strftime("%Y-%m-%d")
    organizers = Organizer.query.all()

    # 진행중: 오늘 열리는 대회 (status = 진행중 이거나 event_date == 오늘)
    live_tournaments = (Tournament.query
                        .filter((Tournament.event_date == today_kst) | (Tournament.status == "진행중"))
                        .order_by(Tournament.event_date.desc()).limit(20).all())
    # 접수중: 미래 대회 중 접수중 (오늘 아직 안 열린 것)
    open_tournaments = (Tournament.query
                        .filter(Tournament.status == "접수중")
                        .filter((Tournament.event_date == None) | (Tournament.event_date >= today_kst))
                        .order_by(Tournament.event_date.asc().nullslast()).limit(20).all())

    banners = Banner.query.filter_by(is_active=True).order_by(Banner.sort_order, Banner.id).all()
    icons = QuickIcon.query.filter_by(is_active=True).order_by(QuickIcon.sort_order, QuickIcon.id).limit(9).all()
    return render_template("index.html", organizers=organizers,
                            live_tournaments=live_tournaments,
                            open_tournaments=open_tournaments,
                            banners=banners, icons=icons)


@app.route("/tournaments")
def tournament_list_page():
    """대회 전체 페이지 - 상태(진행중/접수중/종료/전체) + 단식/복식 필터. 날짜순 정렬."""
    fmt = request.args.get("format", "all")
    if fmt not in ("all", "singles", "doubles"):
        fmt = "all"
    status = request.args.get("status", "live")
    if status not in ("live", "open", "closed", "all"):
        status = "live"

    today_kst = datetime.now(KST).strftime("%Y-%m-%d")
    query = Tournament.query

    # 종목 필터
    if fmt in ("singles", "doubles"):
        query = query.filter(Tournament.format == fmt)

    # 상태 필터
    if status == "live":
        # 진행중: 오늘 열리는 대회
        query = query.filter(
            (Tournament.event_date == today_kst) | (Tournament.status == "진행중")
        )
    elif status == "open":
        # 접수중: status가 접수중이고 오늘 이후 or 날짜 미정
        query = query.filter(Tournament.status == "접수중").filter(
            (Tournament.event_date == None) | (Tournament.event_date >= today_kst)
        )
    elif status == "closed":
        # 종료: 지난 대회 또는 status가 종료
        query = query.filter(
            (Tournament.status == "종료") | (Tournament.event_date < today_kst)
        )
    # status == "all"은 필터 안 함

    tournaments = query.order_by(
        Tournament.event_date.desc().nullslast(), Tournament.id.desc()
    ).all()

    return render_template("tournament_list.html", tournaments=tournaments,
                            current_format=fmt, current_status=status)


@app.route("/org/<slug>")
def organizer_page(slug):
    org = Organizer.query.filter_by(slug=slug).first_or_404()
    tournaments = Tournament.query.filter_by(organizer_id=org.id).order_by(
        Tournament.event_date.desc()).all()
    # 주최자별 페이지는 기존 tournaments 리스트를 그대로 사용 (변경 없음)
    return render_template("index.html", organizers=[org],
                            live_tournaments=tournaments,
                            open_tournaments=[],
                            active_org=org, banners=[], icons=[])


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        phone = (f.get("phone") or "").strip()
        if User.query.filter_by(phone=phone).first():
            flash("이미 가입된 전화번호입니다.")
            return redirect(url_for("register"))
        # 전화번호 SMS 인증 확인
        # 클라이언트가 /api/verify-phone 을 성공적으로 호출했다면 세션에 verified_phone 이 세팅되어 있음
        verified_phone = session.get("verified_phone")
        if verified_phone != phone:
            flash("전화번호 인증을 완료해주세요.")
            return redirect(url_for("register"))
        u = User(name=f["name"], phone=phone, region=f.get("region"),
                  club=f.get("club"), refund_bank=f.get("refund_bank"),
                  refund_account=f.get("refund_account"),
                  handedness=f.get("handedness") or None,
                  strengths=(f.get("strengths") or "").strip() or None,
                  weaknesses=(f.get("weaknesses") or "").strip() or None,
                  playstyle_public=bool(f.get("playstyle_public")),
                  phone_verified=True)
        u.set_password(f["password"])
        db.session.add(u)
        db.session.commit()
        session.pop("verified_phone", None)
        login_user(u)
        flash("회원가입이 완료되었습니다.")
        return redirect(url_for("index"))
    return render_template("register.html")


# ---------------------------------------------------------------------------
# 전화번호 SMS 인증 (회원가입용)
# ---------------------------------------------------------------------------

def _generate_otp_code() -> str:
    return f"{random.randint(0, 999999):06d}"


@app.route("/api/phone/send-code", methods=["POST"])
def api_phone_send_code():
    phone = (request.json or {}).get("phone", "").strip()
    if not phone or len(phone) < 10:
        return jsonify({"error": "전화번호를 정확히 입력해주세요."}), 400
    if User.query.filter_by(phone=phone).first():
        return jsonify({"error": "이미 가입된 전화번호입니다."}), 400
    # 최근 1분 이내에 발송한 코드가 있으면 도스 방지 위해 재발송 제한
    recent = (PhoneVerification.query
              .filter_by(phone=phone)
              .order_by(PhoneVerification.created_at.desc()).first())
    if recent and (datetime.utcnow() - recent.created_at) < timedelta(seconds=45):
        return jsonify({"error": "잠시 후 다시 시도해주세요 (45초 이내 재발송 제한)."}), 429
    code = _generate_otp_code()
    pv = PhoneVerification(phone=phone, code=code,
                            expires_at=datetime.utcnow() + timedelta(minutes=5))
    db.session.add(pv)
    db.session.commit()
    msg = f"[위단테] 인증번호 {code} (5분 유효)"
    r = send_sms(phone, msg)
    return jsonify({"ok": True, "delivered": r["success"]})


@app.route("/api/phone/verify", methods=["POST"])
def api_phone_verify():
    data = request.json or {}
    phone = (data.get("phone") or "").strip()
    code = (data.get("code") or "").strip()
    if not phone or not code:
        return jsonify({"error": "전화번호와 인증번호를 입력해주세요."}), 400
    pv = (PhoneVerification.query
          .filter_by(phone=phone)
          .order_by(PhoneVerification.created_at.desc()).first())
    if not pv:
        return jsonify({"error": "인증번호를 먼저 요청해주세요."}), 400
    if pv.expires_at < datetime.utcnow():
        return jsonify({"error": "인증번호가 만료되었습니다. 다시 요청해주세요."}), 400
    if pv.attempt_count >= 5:
        return jsonify({"error": "인증 시도 횟수를 초과했습니다. 다시 요청해주세요."}), 400
    if pv.code != code:
        pv.attempt_count += 1
        db.session.commit()
        return jsonify({"error": "인증번호가 일치하지 않습니다."}), 400
    pv.verified = True
    db.session.commit()
    session["verified_phone"] = phone
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 마이페이지 (회원 정보 + 참가 대회 이력)
# ---------------------------------------------------------------------------

@app.route("/profile")
@login_required
def profile():
    # 참가한 대회들
    my_parts = (TournamentParticipant.query
                .filter_by(user_id=current_user.id)
                .options(joinedload(TournamentParticipant.tournament))
                .order_by(TournamentParticipant.applied_at.desc()).all())
    # 최고 관리자면 권한 부여 가능한 회원 목록
    granting_users = []
    if current_user.is_superadmin:
        granting_users = User.query.order_by(User.name).all()
    return render_template("profile.html", my_parts=my_parts, granting_users=granting_users)


@app.route("/profile", methods=["POST"])
@login_required
def profile_update():
    f = request.form
    current_user.name = f.get("name") or current_user.name
    current_user.region = f.get("region") or current_user.region
    current_user.club = f.get("club") or current_user.club
    # 선수 프로필 (분석용)
    if "handedness" in f:
        current_user.handedness = f.get("handedness") or None
    if "strengths" in f:
        current_user.strengths = (f.get("strengths") or "").strip() or None
    if "weaknesses" in f:
        current_user.weaknesses = (f.get("weaknesses") or "").strip() or None
    # 체크박스는 명시적으로 처리 (체크 해제도 반영)
    if "playstyle_form_submitted" in f:
        current_user.playstyle_public = bool(f.get("playstyle_public"))
    if f.get("refund_bank"):
        current_user.refund_bank = f.get("refund_bank")
    if f.get("refund_account"):
        current_user.refund_account = f.get("refund_account")
    if f.get("new_password"):
        current_user.set_password(f["new_password"])
    db.session.commit()
    flash("회원정보가 저장되었습니다.")
    return redirect(url_for("profile"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = User.query.filter_by(phone=request.form["phone"]).first()
        if u and u.check_password(request.form["password"]):
            login_user(u)
            return redirect(url_for("index"))
        flash("전화번호 또는 비밀번호가 올바르지 않습니다.")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))


@app.route("/tournament/<int:tid>")
def tournament_page(tid):
    t = Tournament.query.get_or_404(tid)
    # N+1 방지: player1/player2 를 한 번에 로드
    prelim = (Match.query
              .filter_by(tournament_id=tid, stage="prelim")
              .options(joinedload(Match.player1), joinedload(Match.player2))
              .order_by(Match.group_no).all())
    main = (Match.query
            .filter_by(tournament_id=tid, stage="main")
            .options(joinedload(Match.player1), joinedload(Match.player2))
            .all())
    groups = {}
    for m in prelim:
        groups.setdefault(m.group_no, []).append(m)
    my_participation = None
    if current_user.is_authenticated:
        my_participation = TournamentParticipant.query.filter_by(
            tournament_id=tid, user_id=current_user.id).first()
    participant_count = TournamentParticipant.query.filter_by(tournament_id=tid).count()
    is_manager = current_user.is_authenticated and can_manage_tournament(current_user, t)
    return render_template("tournament.html", t=t, groups=groups, main=main,
                            call_types=CALL_TYPES, my_participation=my_participation,
                            participant_count=participant_count,
                            is_manager=is_manager)


@app.route("/admin/banners")
@login_required
def admin_banners():
    if not current_user.is_superadmin:
        abort(403)
    banners = Banner.query.order_by(Banner.sort_order, Banner.id).all()
    return render_template("admin_banners.html", banners=banners, edit_banner=None)


@app.route("/admin/banners/new", methods=["GET", "POST"])
@login_required
def new_banner():
    if not current_user.is_superadmin:
        abort(403)
    if request.method == "POST":
        f = request.form
        b = Banner(
            title=f["title"], subtitle=f.get("subtitle"),
            image_url=f.get("image_url") or None, link_url=f.get("link_url") or None,
            button_text=f.get("button_text") or "자세히 보기",
            bg_from=f.get("bg_from") or "#0E9F8C", bg_to=f.get("bg_to") or "#0B7A6B",
            duration_seconds=int(f.get("duration_seconds") or 5),
            sort_order=int(f.get("sort_order") or 0),
            is_active=bool(f.get("is_active")),
            created_by=current_user.id,
        )
        db.session.add(b)
        db.session.commit()
        flash("배너가 등록되었습니다.")
        return redirect(url_for("admin_banners"))
    return render_template("admin_banners.html", banners=Banner.query.all(), edit_banner=None, is_new=True)


@app.route("/admin/banners/<int:bid>/edit", methods=["GET", "POST"])
@login_required
def edit_banner(bid):
    if not current_user.is_superadmin:
        abort(403)
    b = Banner.query.get_or_404(bid)
    if request.method == "POST":
        f = request.form
        b.title = f["title"]; b.subtitle = f.get("subtitle")
        b.image_url = f.get("image_url") or None
        b.link_url = f.get("link_url") or None
        b.button_text = f.get("button_text") or "자세히 보기"
        b.bg_from = f.get("bg_from") or "#0E9F8C"
        b.bg_to = f.get("bg_to") or "#0B7A6B"
        b.duration_seconds = int(f.get("duration_seconds") or 5)
        b.sort_order = int(f.get("sort_order") or 0)
        b.is_active = bool(f.get("is_active"))
        db.session.commit()
        flash("배너가 수정되었습니다.")
        return redirect(url_for("admin_banners"))
    return render_template("admin_banners.html", banners=Banner.query.all(), edit_banner=b)


@app.route("/admin/banners/<int:bid>/toggle", methods=["POST"])
@login_required
def toggle_banner(bid):
    if not current_user.is_superadmin:
        abort(403)
    b = Banner.query.get_or_404(bid)
    b.is_active = not b.is_active
    db.session.commit()
    return jsonify({"ok": True, "is_active": b.is_active})


@app.route("/admin/banners/<int:bid>/delete", methods=["POST"])
@login_required
def delete_banner(bid):
    if not current_user.is_superadmin:
        abort(403)
    b = Banner.query.get_or_404(bid)
    db.session.delete(b)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/upload", methods=["POST"])
@login_required
def admin_upload():
    """아이콘/배너용 이미지 파일 업로드. 관리자만 가능.
    주의: Railway 등 컨테이너 배포 환경은 파일시스템이 재배포 시 초기화될 수 있어
    영구 보관이 필요하면 Volume 연결 또는 외부 이미지 URL 사용을 권장."""
    if not current_user.is_admin:
        abort(403)
    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"error": "파일이 없습니다"}), 400
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_UPLOAD_EXT:
        return jsonify({"error": "지원하지 않는 파일 형식입니다 (png/jpg/gif/webp/svg만 가능)"}), 400
    filename = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(UPLOAD_DIR, secure_filename(filename)))
    return jsonify({"ok": True, "url": url_for("static", filename=f"uploads/{filename}")})


@app.route("/admin/icons")
@login_required
def admin_icons():
    if not current_user.is_superadmin:
        abort(403)
    icons = QuickIcon.query.order_by(QuickIcon.sort_order, QuickIcon.id).all()
    return render_template("admin_icons.html", icons=icons, edit_icon=None)


@app.route("/admin/icons/new", methods=["GET", "POST"])
@login_required
def new_icon():
    if not current_user.is_superadmin:
        abort(403)
    if request.method == "POST":
        f = request.form
        w = max(24, min(240, int(f.get("icon_width") or f.get("icon_size") or 64)))
        h = max(24, min(240, int(f.get("icon_height") or f.get("icon_size") or 64)))
        icon = QuickIcon(
            label=f["label"], icon_url=f.get("icon_url") or None,
            icon_size=w,   # 하위호환 (구 컬럼에도 저장)
            icon_width=w, icon_height=h,
            link_type=f.get("link_type") or "external",
            link_url=f.get("link_url") or None,
            sort_order=int(f.get("sort_order") or 0),
            is_active=bool(f.get("is_active")),
            created_by=current_user.id,
        )
        db.session.add(icon)
        db.session.commit()
        flash("아이콘이 등록되었습니다.")
        return redirect(url_for("admin_icons"))
    return render_template("admin_icons.html", icons=QuickIcon.query.all(), edit_icon=None, is_new=True)


@app.route("/admin/icons/<int:iid>/edit", methods=["GET", "POST"])
@login_required
def edit_icon(iid):
    if not current_user.is_superadmin:
        abort(403)
    icon = QuickIcon.query.get_or_404(iid)
    if request.method == "POST":
        f = request.form
        w = max(24, min(240, int(f.get("icon_width") or f.get("icon_size") or 64)))
        h = max(24, min(240, int(f.get("icon_height") or f.get("icon_size") or 64)))
        icon.label = f["label"]
        icon.icon_url = f.get("icon_url") or None
        icon.icon_size = w
        icon.icon_width = w
        icon.icon_height = h
        icon.link_type = f.get("link_type") or "external"
        icon.link_url = f.get("link_url") or None
        icon.sort_order = int(f.get("sort_order") or 0)
        icon.is_active = bool(f.get("is_active"))
        db.session.commit()
        flash("아이콘이 수정되었습니다.")
        return redirect(url_for("admin_icons"))
    return render_template("admin_icons.html", icons=QuickIcon.query.all(), edit_icon=icon)


@app.route("/admin/icons/<int:iid>/toggle", methods=["POST"])
@login_required
def toggle_icon(iid):
    if not current_user.is_superadmin:
        abort(403)
    icon = QuickIcon.query.get_or_404(iid)
    icon.is_active = not icon.is_active
    db.session.commit()
    return jsonify({"ok": True, "is_active": icon.is_active})


@app.route("/admin/icons/<int:iid>/delete", methods=["POST"])
@login_required
def delete_icon(iid):
    if not current_user.is_superadmin:
        abort(403)
    icon = QuickIcon.query.get_or_404(iid)
    db.session.delete(icon)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/calendar")
def tournament_calendar():
    """대회 아이콘을 누르면 뜨는 월별 캘린더. 날짜별로 대회가 표시되고,
    관리자는 특정 날짜를 눌러 그 날짜로 대회를 바로 만들 수 있다."""
    today = date.today()
    year = int(request.args.get("year") or today.year)
    month = int(request.args.get("month") or today.month)

    cal = Calendar(firstweekday=6)  # 일요일 시작
    weeks = cal.monthdatescalendar(year, month)

    prefix = f"{year:04d}-{month:02d}"
    tournaments = Tournament.query.filter(Tournament.event_date.like(f"{prefix}%")).all()
    by_date = {}
    for t in tournaments:
        by_date.setdefault(t.event_date, []).append(t)

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)

    return render_template("calendar.html", weeks=weeks, year=year, month=month,
                            by_date=by_date, today=today,
                            prev_year=prev_year, prev_month=prev_month,
                            next_year=next_year, next_month=next_month)


@app.route("/api/tournaments/<int:tid>/status", methods=["POST"])
@login_required
def api_update_status(tid):
    t = Tournament.query.get_or_404(tid)
    if not can_manage_tournament(current_user, t):
        abort(403)
    status = request.json.get("status")
    if status not in ("접수중", "접수마감", "진행중", "종료"):
        return jsonify({"error": "잘못된 상태값입니다"}), 400
    t.status = status
    db.session.commit()
    # 실시간 반영: 대회 페이지에 접속한 모두 + 관리자 대시보드
    socketio.emit("tournament_status", {"tournament_id": tid, "status": status},
                   room=f"tournament_{tid}")
    return jsonify({"ok": True, "status": t.status})


@app.route("/admin")
@login_required
def admin_home():
    if not current_user.is_admin:
        abort(403)
    # 최고 관리자는 전체 대회, 그 외 관리자는 자기가 만든 대회 + 소유자 없는 예전 대회(같은 organizer) 만
    if current_user.is_superadmin:
        tournaments = Tournament.query.order_by(Tournament.created_at.desc()).all()
    else:
        tournaments = (Tournament.query
                       .filter(db.or_(
                            Tournament.created_by_id == current_user.id,
                            db.and_(Tournament.created_by_id.is_(None),
                                    Tournament.organizer_id == current_user.organizer_id),
                        ))
                       .order_by(Tournament.created_at.desc()).all())
    organizers = Organizer.query.all() if current_user.is_superadmin else []
    return render_template("admin_home.html", tournaments=tournaments, organizers=organizers)


@app.route("/admin/tournaments/new", methods=["GET", "POST"])
@login_required
def new_tournament():
    # 대회 생성 권한: 최고 관리자 또는 대회 생성 권한을 부여받은 사용자
    if not (current_user.is_superadmin or current_user.can_create_tournament):
        abort(403)
    if request.method == "POST":
        f = request.form
        t = Tournament(
            organizer_id=current_user.organizer_id,
            title=f["title"], location=f.get("location"), event_date=f.get("event_date"),
            entry_fee=int(f.get("entry_fee") or 0),
            group_size=int(f.get("group_size") or 3),
            format=f.get("format") or "singles",
            start_time=f.get("start_time") or "09:00",
            match_interval_min=int(f.get("match_interval_min") or 30),
            groups_per_court=int(f.get("groups_per_court") or 2),
            main_bracket_size=(int(f.get("main_bracket_size")) if f.get("main_bracket_size") else None),
            parking_url=f.get("parking_url") or None,
            deposit_bank=f.get("deposit_bank") or None,
            deposit_account=f.get("deposit_account") or None,
            deposit_holder=f.get("deposit_holder") or None,
            status="접수중",
            created_by_id=current_user.id,
        )
        # 대회를 만드는 사용자는 is_admin=True 로 승격(자기 대회 관리 위해)
        if not current_user.is_admin:
            current_user.is_admin = True
        db.session.add(t)
        db.session.commit()
        flash("대회가 생성되었습니다.")
        return redirect(url_for("admin_dashboard", tid=t.id))
    return render_template("new_tournament.html", prefill_date=request.args.get("date", ""))


@app.route("/admin/organizers/new", methods=["GET", "POST"])
@login_required
def new_organizer():
    """다른 주최자가 위단테를 사용할 때: 메인화면에 노출될 개별 카테고리(테넌트)를 만들고
    그 카테고리의 첫 관리자 계정을 함께 생성한다."""
    if not current_user.is_admin:
        abort(403)
    if request.method == "POST":
        f = request.form
        if Organizer.query.filter_by(slug=f["slug"]).first():
            flash("이미 사용 중인 카테고리 주소입니다.")
            return redirect(url_for("new_organizer"))
        if User.query.filter_by(phone=f["admin_phone"]).first():
            flash("이미 가입된 전화번호입니다.")
            return redirect(url_for("new_organizer"))

        org = Organizer(name=f["name"], slug=f["slug"], contact_phone=f.get("contact_phone"),
                       band_url=f.get("band_url") or None, youtube_url=f.get("youtube_url") or None)
        db.session.add(org)
        db.session.commit()

        admin = User(name=f["admin_name"], phone=f["admin_phone"], organizer_id=org.id,
                     is_admin=True)
        admin.set_password(f["admin_password"])
        db.session.add(admin)
        db.session.commit()

        flash(f"'{org.name}' 카테고리가 생성되었습니다. /org/{org.slug} 에서 독립적으로 운영할 수 있습니다.")
        return redirect(url_for("admin_home"))
    return render_template("new_organizer.html")



@app.route("/admin/tournament/<int:tid>")
@login_required
def admin_dashboard(tid):
    t = Tournament.query.get_or_404(tid)
    if not can_manage_tournament(current_user, t):
        abort(403)
    # 참가자와 user 관계를 한 번에 (N+1 방지)
    participants = (TournamentParticipant.query
                    .filter_by(tournament_id=tid)
                    .options(joinedload(TournamentParticipant.user))
                    .order_by(TournamentParticipant.applied_at).all())
    confirmed_ids = {p.user_id for p in participants if p.status == "확정"}
    applicant_ids = {p.user_id for p in participants}
    # 조 편성 대상: (1) 이 대회에 신청한 회원 우선, (2) 최고 관리자면 organizer 소속 회원까지 옵션 확대
    #  * 예전 코드는 organizer 소속 회원 전체를 항상 로드해서 회원이 많으면 렉의 원인이 됨
    if applicant_ids:
        applicant_users = User.query.filter(User.id.in_(applicant_ids)).all()
    else:
        applicant_users = []
    players = sorted(applicant_users, key=lambda u: u.name)
    refund_requests = RefundRequest.query.filter_by(tournament_id=tid).order_by(
        RefundRequest.created_at.desc()).all()
    calls = (CallLog.query.filter_by(tournament_id=tid)
             .options(joinedload(CallLog.user))
             .order_by(CallLog.created_at.desc()).limit(50).all())
    prelim = Match.query.filter_by(tournament_id=tid, stage="prelim").all()
    groups = sorted(set(m.group_no for m in prelim)) if prelim else []
    return render_template("admin_dashboard.html", t=t, players=players, calls=calls,
                            groups=groups, call_types=CALL_TYPES,
                            participants=participants, confirmed_ids=confirmed_ids,
                            refund_requests=refund_requests,
                            can_delete=can_delete_tournament(current_user, t))


# ---------------------------------------------------------------------------
# API - 대진표
# ---------------------------------------------------------------------------

@app.route("/api/tournaments/<int:tid>/draw/generate", methods=["POST"])
@login_required
def api_generate_draw(tid):
    t = Tournament.query.get_or_404(tid)
    if not can_manage_tournament(current_user, t):
        abort(403)
    data = request.json or {}
    player_ids = data.get("player_ids", [])
    # 중복 제거 (같은 선수를 두 번 체크하는 실수 방지)
    player_ids = list(dict.fromkeys(int(pid) for pid in player_ids))
    if len(player_ids) < 2:
        return jsonify({"error": "참가자가 2명 이상 필요합니다"}), 400
    # 수동 조 배정 데이터가 있으면 사용, 없으면 랜덤
    #   manual_groups: [[id, id, id], [id, id, id], ...]  각 조의 선수 id 배열
    manual_groups = data.get("manual_groups")
    try:
        # 재생성 시 이전 매치를 완전 삭제 (조 우승자 반영 상태 등 초기화)
        old_match_ids = [row[0] for row in db.session.query(Match.id).filter_by(tournament_id=t.id).all()]
        if old_match_ids:
            # CallLog가 삭제될 매치를 참조 중이면 FK 위반이 나므로 참조를 먼저 끊는다
            CallLog.query.filter(CallLog.match_id.in_(old_match_ids)).update(
                {"match_id": None}, synchronize_session=False)
            Match.query.filter_by(tournament_id=t.id).delete(synchronize_session=False)
            db.session.commit()
        if manual_groups:
            # 각 조를 관리자가 직접 지정
            cleaned = []
            for g in manual_groups:
                ids = list(dict.fromkeys(int(x) for x in g if x))
                if ids:
                    cleaned.append(ids)
            if not cleaned:
                return jsonify({"error": "수동 조 편성에 유효한 선수가 없습니다"}), 400
            generate_manual_groups(t, cleaned)
        else:
            generate_prelim_groups(t, player_ids)
    except Exception as e:
        db.session.rollback()
        app.logger.exception("draw generation failed")
        return jsonify({"error": f"대진표 생성 중 오류가 발생했습니다: {e}"}), 500
    socketio.emit("bracket_update", {"tournament_id": tid}, room=f"tournament_{tid}")
    return jsonify({"ok": True})


def _advance_prelim_winner_loser(m: Match):
    """예선 경기 종료 시, 이 경기의 승자/패자를 다음 매치(seq 2, 3)에 자동 배치.
    복식이면 파트너 정보도 함께 이동한다."""
    if m.stage != "prelim" or not m.winner_id:
        return
    # 승자 팀 (와 파트너)
    if m.winner_id == m.player1_id:
        w_uid, w_partner = m.player1_id, m.player1_partner_id
        l_uid, l_partner = m.player2_id, m.player2_partner_id
    else:
        w_uid, w_partner = m.player2_id, m.player2_partner_id
        l_uid, l_partner = m.player1_id, m.player1_partner_id

    dependents = Match.query.filter_by(
        tournament_id=m.tournament_id, stage="prelim", source_match_id=m.id
    ).all()
    for d in dependents:
        if d.source_role == "winner":
            uid, partner = w_uid, w_partner
        else:
            uid, partner = l_uid, l_partner
        if d.player1_id is None:
            d.player1_id = uid
            d.player1_partner_id = partner
        elif d.player2_id is None:
            d.player2_id = uid
            d.player2_partner_id = partner
    db.session.flush()


def _finalize_prelim_group(tournament_id: int, group_no: int):
    """조 내 모든 경기가 끝나면 조 1위, 2위를 자동 결정하여 본선 첫 라운드에 배치한다.
    반환값: (winner_user_id, runner_up_user_id) 또는 None
    - 순위 규칙: 승수 많은 순, 동률이면 그 둘의 직접 대결 승자가 위
    - 조 1위, 2위 두 명(팀)이 본선 진출. 3위는 탈락.
    """
    matches = (Match.query
               .filter_by(tournament_id=tournament_id, stage="prelim", group_no=group_no)
               .order_by(Match.seq_in_group.nulls_last()).all())
    if not matches or any(x.status != "종료" for x in matches):
        return None

    # 조에 참가한 선수 목록 수집 (player1/player2 슬롯에서 나온 모든 user_id)
    all_players = set()
    for m in matches:
        if m.player1_id: all_players.add(m.player1_id)
        if m.player2_id: all_players.add(m.player2_id)
    if not all_players:
        return None

    # 승수 계산
    wins = {pid: 0 for pid in all_players}
    for m in matches:
        if m.winner_id and m.winner_id in wins:
            wins[m.winner_id] += 1

    # 순위 결정: 승수 내림차순. 동률 시 그 둘의 상대전적으로 결정
    def head_to_head(a, b):
        """a가 b를 이긴 경기가 있으면 +1, 진 경기가 있으면 -1, 없으면 0"""
        for m in matches:
            if ((m.player1_id == a and m.player2_id == b) or
                (m.player1_id == b and m.player2_id == a)):
                if m.winner_id == a:
                    return 1
                if m.winner_id == b:
                    return -1
        return 0

    # 파이썬 정렬은 비교 기반이 아니어서, 승수 그룹핑 후 그룹 내 상대전적으로 재정렬
    ranked = sorted(all_players, key=lambda p: -wins[p])
    # 동률 그룹별로 상대전적 정렬
    from itertools import groupby
    final_order = []
    for _, grp in groupby(ranked, key=lambda p: wins[p]):
        grp = list(grp)
        if len(grp) > 1:
            # 상대전적 tournament (간단히: 각 선수 대비 승수)
            grp.sort(key=lambda p: -sum(1 for q in grp if q != p and head_to_head(p, q) > 0))
        final_order.extend(grp)

    # 파트너 정보 조회 (복식이면 함께 이동)
    def partner_of(user_id):
        for m in matches:
            if m.player1_id == user_id:
                return m.player1_partner_id
            if m.player2_id == user_id:
                return m.player2_partner_id
        return None

    # 조 개수 조회 (본선 슬롯 계산에 필요)
    groups_count = (db.session.query(func.count(func.distinct(Match.group_no)))
                    .filter(Match.tournament_id == tournament_id,
                            Match.stage == "prelim").scalar()) or 1

    winner_id = final_order[0] if len(final_order) >= 1 else None
    runner_up_id = final_order[1] if len(final_order) >= 2 else None

    if winner_id:
        _set_group_qualifier_slot(tournament_id, group_no, 1, winner_id,
                                   partner_id=partner_of(winner_id),
                                   groups_count=groups_count)
    if runner_up_id:
        _set_group_qualifier_slot(tournament_id, group_no, 2, runner_up_id,
                                   partner_id=partner_of(runner_up_id),
                                   groups_count=groups_count)

    return winner_id  # 하위호환: 기존 호출부는 조 1위 반환값을 기대


def _schedule_next_match_notification(match: Match):
    """[Deprecated] 예전에는 경기 '시작 후' 20분 뒤 다음 조원에게 문자를 보냈으나,
    이제는 시간표 자동 배정 시 각 매치 '시작 20분 前' 발송으로 대체됨.
    (호환용으로 남겨두었으며 별도로 호출되지 않음)"""
    return


@app.route("/api/tournaments/<int:tid>/participants-search")
@login_required
def api_tournament_participants_search(tid):
    """본선 매치 슬롯 배정을 위한 참가자 검색.
    이 대회에 참가한 사람(TournamentParticipant) 중 이름/전화번호 부분일치.
    관리자만 호출 가능."""
    t = Tournament.query.get_or_404(tid)
    if not can_manage_tournament(current_user, t):
        abort(403)
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []})
    from sqlalchemy import or_
    users = (User.query
             .join(TournamentParticipant, TournamentParticipant.user_id == User.id)
             .filter(TournamentParticipant.tournament_id == tid)
             .filter(or_(User.name.ilike(f"%{q}%"), User.phone.ilike(f"%{q}%")))
             .limit(15).all())
    return jsonify({
        "results": [{"id": u.id, "name": u.name, "phone": u.phone or ""} for u in users]
    })


@app.route("/api/matches/<int:mid>/assign", methods=["POST"])
@login_required
def api_assign_match(mid):
    """본선 대진표 관리자 수동 편집: 특정 slot에 특정 유저 할당하거나 비움.
    body: {slot: 1 or 2, user_id: int or null, partner_id: int or null}
    - 관리자만 가능
    - 예선 매치는 자동 진행이므로 수동 편집 불가 (혼란 방지)
    - BYE 처리는 user_id=null 로 slot 비우기"""
    m = Match.query.get_or_404(mid)
    if not can_manage_tournament(current_user, m.tournament):
        abort(403)
    if m.stage != "main":
        return jsonify({"error": "예선 매치는 자동으로 배치되므로 수동 편집이 불가합니다. 필요하면 예선을 재생성해주세요."}), 400
    if m.status == "종료":
        return jsonify({"error": "이미 종료된 경기는 편집할 수 없습니다. 먼저 '경기전으로 되돌리기'를 사용해주세요."}), 400

    slot = request.json.get("slot")
    if slot not in (1, 2):
        return jsonify({"error": "slot 은 1 또는 2 여야 합니다."}), 400
    user_id = request.json.get("user_id")
    partner_id = request.json.get("partner_id")

    if user_id is not None:
        user = User.query.get(user_id)
        if not user:
            return jsonify({"error": "존재하지 않는 사용자입니다."}), 404

    if slot == 1:
        m.player1_id = user_id
        m.player1_partner_id = partner_id
    else:
        m.player2_id = user_id
        m.player2_partner_id = partner_id
    db.session.commit()
    socketio.emit("bracket_update", {"tournament_id": m.tournament_id},
                   room=f"tournament_{m.tournament_id}")
    return jsonify({"ok": True})


@app.route("/api/matches/<int:mid>/schedule", methods=["POST"])
@login_required
def api_schedule_match(mid):
    """본선 매치의 코트 / 예정 시각 수동 설정 (관리자 전용)."""
    m = Match.query.get_or_404(mid)
    if not can_manage_tournament(current_user, m.tournament):
        abort(403)
    court = request.json.get("court")
    scheduled_time = request.json.get("scheduled_time")
    if court is not None:
        m.court = str(court).strip() or None
    if scheduled_time is not None:
        m.scheduled_time = str(scheduled_time).strip() or None
    db.session.commit()
    socketio.emit("bracket_update", {"tournament_id": m.tournament_id},
                   room=f"tournament_{m.tournament_id}")
    return jsonify({"ok": True, "court": m.court, "scheduled_time": m.scheduled_time})


@app.route("/api/matches/<int:mid>/reset", methods=["POST"])
@login_required
def api_reset_match(mid):
    """실수로 진행중/종료로 표시한 경기를 '경기전' 상태로 되돌린다.
    - status = '시작전', started_at = None
    - 종료된 경기라면 score/winner도 지워짐 → 다음 매치에 배치했던 승자/패자도 되돌려짐
    - 관리자만 가능"""
    m = Match.query.get_or_404(mid)
    if not can_manage_tournament(current_user, m.tournament):
        abort(403)

    was_ended = (m.status == "종료")
    prev_winner_id = m.winner_id

    m.status = "시작전"
    m.started_at = None
    m.score = None
    m.winner_id = None

    # 종료였다면 dependent 매치들(다음 경기의 승자/패자 슬롯)도 되돌림
    if was_ended:
        dependents = Match.query.filter_by(
            tournament_id=m.tournament_id, stage="prelim", source_match_id=m.id
        ).all()
        for d in dependents:
            # 이 매치의 승자/패자가 dependent 매치의 slot1에 배치돼있었으면 되돌림
            if d.player1_id == prev_winner_id and d.source_role == "winner":
                d.player1_id = None
                d.player1_partner_id = None
            # source_role == "loser" 인 경우도 마찬가지
            loser_id = m.player2_id if prev_winner_id == m.player1_id else m.player1_id
            if d.player1_id == loser_id and d.source_role == "loser":
                d.player1_id = None
                d.player1_partner_id = None

    db.session.commit()
    socketio.emit("bracket_update", {"tournament_id": m.tournament_id},
                   room=f"tournament_{m.tournament_id}")
    return jsonify({"ok": True})


@app.route("/api/matches/<int:mid>/start", methods=["POST"])
@login_required
def api_start_match(mid):
    """경기 시작 - 관리자가 명시적으로 표시. 다음 경기 참가자에게는 이미 대진표 생성 시
    '시작 20분 前 문자'가 자동 예약되어 있으므로 여기서는 상태 변경만 처리."""
    m = Match.query.get_or_404(mid)
    if not can_manage_tournament(current_user, m.tournament):
        abort(403)
    if m.status != "종료":
        m.status = "진행중"
        m.started_at = datetime.utcnow()
        db.session.commit()
    socketio.emit("bracket_update", {"tournament_id": m.tournament_id},
                   room=f"tournament_{m.tournament_id}")
    return jsonify({"ok": True, "started_at": m.started_at.replace(tzinfo=timezone.utc).isoformat() if m.started_at else None})


@app.route("/api/matches/<int:mid>/score", methods=["POST"])
@login_required
def api_submit_score(mid):
    m = Match.query.get_or_404(mid)
    if not can_manage_tournament(current_user, m.tournament):
        abort(403)
    data = request.json
    m.score = data.get("score")
    m.winner_id = data.get("winner_id")
    m.status = "종료"
    db.session.commit()

    if m.stage == "main":
        advance_winner(m)
        # 결승(next_match_id가 없는 최종 매치)이 종료되면 대회 우승자 확정
        if m.next_match_id is None and m.winner_id:
            t = m.tournament
            t.winner_id = m.winner_id
            t.status = "종료"
            db.session.commit()
            socketio.emit("tournament_finished", {
                "tournament_id": t.id,
                "winner_id": t.winner_id,
                "winner_name": t.winner.name if t.winner else None,
            }, room=f"tournament_{t.id}")
    elif m.stage == "prelim":
        # 다음 예선 경기(승자/패자 대기 매치)에 자동 배치
        _advance_prelim_winner_loser(m)
        # 조 전체가 끝났으면 조 1위 + 2위를 본선 슬롯에 자동 반영 (finalize 함수 안에서 배치)
        _finalize_prelim_group(m.tournament_id, m.group_no)
        db.session.commit()

    socketio.emit("bracket_update", {"tournament_id": m.tournament_id},
                   room=f"tournament_{m.tournament_id}")
    return jsonify({"ok": True})


@app.route("/api/tournaments/<int:tid>/groups/<int:gno>/winner", methods=["POST"])
@login_required
def api_group_winner(tid, gno):
    t = Tournament.query.get_or_404(tid)
    if not can_manage_tournament(current_user, t):
        abort(403)
    winner_id = request.json.get("winner_id")
    record_group_winner(t, gno, winner_id)
    return jsonify({"ok": True})


@app.route("/api/tournaments/<int:tid>/draw")
def api_get_draw(tid):
    # N+1 방지: player1/player2 관계를 joinedload 로 한 번에 로드
    prelim = (Match.query
              .filter_by(tournament_id=tid, stage="prelim")
              .options(joinedload(Match.player1), joinedload(Match.player2))
              .order_by(Match.group_no, Match.seq_in_group.nulls_last())
              .all())
    main = (Match.query
            .filter_by(tournament_id=tid, stage="main")
            .options(joinedload(Match.player1), joinedload(Match.player2))
            .all())

    # 본선 첫 라운드 매치의 소스 조/순위 매핑 계산
    # (예: 매치1 slot1 → 1조 1위, 매치1 slot2 → 2조 2위 등)
    first_round_source = {}   # (match_id, slot) -> (group_no, rank)
    if main:
        first_round = _first_round_main_matches(tid)
        groups_count = (db.session.query(func.count(func.distinct(Match.group_no)))
                        .filter(Match.tournament_id == tid, Match.stage == "prelim").scalar()) or 0
        t = Tournament.query.get(tid)
        if first_round and groups_count > 0:
            for gno in range(1, groups_count + 1):
                for rank in (1, 2):
                    idx, slot = _resolve_bracket_slot(t, gno, rank, groups_count, len(first_round))
                    if 0 <= idx < len(first_round):
                        first_round_source[(first_round[idx].id, slot)] = (gno, rank)

    def m2d(m):
        p1_src = first_round_source.get((m.id, 1))
        p2_src = first_round_source.get((m.id, 2))
        return {
            "id": m.id, "stage": m.stage, "group_no": m.group_no,
            "seq_in_group": m.seq_in_group,
            "round_name": m.round_name, "match_no": m.match_no,
            "player1": m.player1.name if m.player1 else None,
            "player1_id": m.player1_id,
            "player1_partner": m.player1_partner.name if m.player1_partner else None,
            "player1_partner_id": m.player1_partner_id,
            "player1_source_group": p1_src[0] if p1_src else None,
            "player1_source_rank": p1_src[1] if p1_src else None,
            "player2": m.player2.name if m.player2 else None,
            "player2_id": m.player2_id,
            "player2_partner": m.player2_partner.name if m.player2_partner else None,
            "player2_partner_id": m.player2_partner_id,
            "player2_source_group": p2_src[0] if p2_src else None,
            "player2_source_rank": p2_src[1] if p2_src else None,
            "score": m.score, "status": m.status,
            "winner_id": m.winner_id,
            "court": m.court, "court_seq": m.court_seq,
            "scheduled_time": m.scheduled_time,
            "started_at": m.started_at.replace(tzinfo=timezone.utc).isoformat() if m.started_at else None,
            "duration_min": m.duration_min or 20,
            "source_match_id": m.source_match_id,
            "source_role": m.source_role,
        }
    return jsonify({"prelim": [m2d(m) for m in prelim], "main": [m2d(m) for m in main]})


@app.route("/api/tournaments/<int:tid>/search")
def api_search_players(tid):
    """선수이름 검색 → 코트/시간/상대 확인 (원본 사이트의 '선수 검색' 기능)"""
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"results": []})
    matches = Match.query.filter(Match.tournament_id == tid).join(
        User, db.or_(Match.player1_id == User.id, Match.player2_id == User.id)
    ).filter(User.name.ilike(f"%{name}%")).all()

    results = []
    seen = set()
    for m in matches:
        if m.id in seen:
            continue
        seen.add(m.id)
        results.append({
            "stage": "예선" if m.stage == "prelim" else (m.round_name or "본선"),
            "group_no": m.group_no,
            "player1": m.player1.name if m.player1 else None,
            "player2": m.player2.name if m.player2 else None,
            "court": m.court, "scheduled_time": m.scheduled_time,
            "status": m.status, "score": m.score,
        })
    return jsonify({"results": results})


@app.route("/api/tournaments/<int:tid>/apply", methods=["POST"])
@login_required
def api_apply_tournament(tid):
    """선수가 '참가 신청' 버튼을 누르면 호출. 회원가입 시 정보를 그대로 쓰므로
    이름/전화번호 등을 다시 입력할 필요가 없다."""
    t = Tournament.query.get_or_404(tid)
    if current_user.is_admin:
        return jsonify({"error": "관리자 계정은 대회에 참가 신청할 수 없습니다."}), 400
    if t.status != "접수중":
        return jsonify({"error": f"이 대회는 '{t.status}' 상태여서 지금은 신청할 수 없습니다."}), 400
    existing = TournamentParticipant.query.filter_by(tournament_id=tid, user_id=current_user.id).first()
    if existing:
        return jsonify({"error": "이미 신청하셨습니다."}), 400
    status = "확정" if not t.entry_fee else "대기"
    p = TournamentParticipant(tournament_id=tid, user_id=current_user.id, status=status,
                               confirmed_at=datetime.utcnow() if status == "확정" else None)
    db.session.add(p)
    db.session.commit()
    # [4] 관리자 대시보드에 실시간 반영
    socketio.emit("participant_update", {
        "tournament_id": tid, "participant_id": p.id, "user_name": current_user.name,
        "user_phone": current_user.phone, "status": p.status,
        "applied_at": p.applied_at.strftime("%m/%d %H:%M"),
        "action": "new"
    }, room=f"tournament_{tid}_admin")
    return jsonify({"ok": True, "status": p.status})


@app.route("/api/participants/<int:pid>/mark-paid", methods=["POST"])
@login_required
def api_mark_paid(pid):
    """선수 본인이 '입금 완료' 버튼을 눌러 자진 신고 (관리자 확인 전 단계)"""
    p = TournamentParticipant.query.get_or_404(pid)
    if p.user_id != current_user.id:
        abort(403)
    p.status = "입금확인요청"
    db.session.commit()
    socketio.emit("participant_update", {
        "tournament_id": p.tournament_id, "participant_id": p.id,
        "status": p.status, "action": "update"
    }, room=f"tournament_{p.tournament_id}_admin")
    return jsonify({"ok": True, "status": p.status})


@app.route("/api/participants/<int:pid>/partner", methods=["POST"])
@login_required
def api_set_participant_partner(pid):
    """복식 대회에서 관리자가 참가자 파트너를 지정. 상호 파트너 관계 자동 동기화."""
    p = TournamentParticipant.query.get_or_404(pid)
    if not can_manage_tournament(current_user, p.tournament):
        abort(403)
    partner_id = (request.json or {}).get("partner_id")
    partner_id = int(partner_id) if partner_id else None

    # 이전 파트너 관계 해제
    if p.partner_id and p.partner_id != partner_id:
        prev = TournamentParticipant.query.filter_by(
            tournament_id=p.tournament_id, user_id=p.partner_id
        ).first()
        if prev and prev.partner_id == p.user_id:
            prev.partner_id = None

    p.partner_id = partner_id

    # 상대방도 나를 파트너로 자동 지정
    if partner_id:
        other = TournamentParticipant.query.filter_by(
            tournament_id=p.tournament_id, user_id=partner_id
        ).first()
        if other:
            # 상대의 이전 파트너 해제
            if other.partner_id and other.partner_id != p.user_id:
                prev2 = TournamentParticipant.query.filter_by(
                    tournament_id=p.tournament_id, user_id=other.partner_id
                ).first()
                if prev2 and prev2.partner_id == partner_id:
                    prev2.partner_id = None
            other.partner_id = p.user_id
    db.session.commit()
    socketio.emit("participant_update", {
        "tournament_id": p.tournament_id, "participant_id": p.id,
        "status": p.status, "action": "update"
    }, room=f"tournament_{p.tournament_id}_admin")
    return jsonify({"ok": True, "partner_id": p.partner_id})


@app.route("/api/participants/<int:pid>/confirm", methods=["POST"])
@login_required
def api_confirm_participant(pid):
    """관리자가 입금 확인 후 참가를 확정 처리"""
    p = TournamentParticipant.query.get_or_404(pid)
    if not can_manage_tournament(current_user, p.tournament):
        abort(403)
    p.status = "확정"
    p.confirmed_at = datetime.utcnow()
    db.session.commit()
    socketio.emit("participant_update", {
        "tournament_id": p.tournament_id, "participant_id": p.id,
        "status": p.status, "action": "update"
    }, room=f"tournament_{p.tournament_id}_admin")
    return jsonify({"ok": True, "status": p.status})


@app.route("/api/participants/<int:pid>/cancel", methods=["POST"])
@login_required
def api_cancel_participant(pid):
    """본인 신청 취소 또는 관리자의 강제 취소"""
    p = TournamentParticipant.query.get_or_404(pid)
    if p.user_id != current_user.id and not can_manage_tournament(current_user, p.tournament):
        abort(403)
    tid = p.tournament_id; pid_val = p.id
    db.session.delete(p)
    db.session.commit()
    socketio.emit("participant_update", {
        "tournament_id": tid, "participant_id": pid_val, "action": "delete"
    }, room=f"tournament_{tid}_admin")
    return jsonify({"ok": True})


def _can_access_refund(rr):
    return current_user.is_authenticated and (rr.user_id == current_user.id or current_user.is_admin)


@app.route("/api/tournaments/<int:tid>/refund-request", methods=["POST"])
@login_required
def api_create_refund_request(tid):
    """선수가 '환불 요청' 버튼을 누르면 채팅방(RefundRequest)이 생성됨 (없으면 새로 만들고, 있으면 기존 것을 반환)"""
    participation = TournamentParticipant.query.filter_by(tournament_id=tid, user_id=current_user.id).first()
    if not participation:
        return jsonify({"error": "이 대회에 신청한 내역이 없습니다."}), 400
    rr = RefundRequest.query.filter_by(participant_id=participation.id).first()
    if not rr:
        rr = RefundRequest(tournament_id=tid, participant_id=participation.id, user_id=current_user.id)
        db.session.add(rr)
        db.session.commit()
    msg_text = (request.json.get("message") if request.is_json else None) or None
    if msg_text:
        m = RefundMessage(refund_request_id=rr.id, sender_id=current_user.id, message=msg_text)
        db.session.add(m)
        db.session.commit()
        socketio.emit("refund_message", {
            "sender": current_user.name, "is_admin": current_user.is_admin,
            "message": msg_text, "created_at": m.created_at.strftime("%m/%d %H:%M"),
        }, room=f"refund_{rr.id}")
    return jsonify({"ok": True, "id": rr.id, "status": rr.status})


@app.route("/api/refunds/<int:rid>/messages", methods=["GET"])
@login_required
def api_get_refund_messages(rid):
    rr = RefundRequest.query.get_or_404(rid)
    if not _can_access_refund(rr):
        abort(403)
    msgs = RefundMessage.query.filter_by(refund_request_id=rid).order_by(RefundMessage.created_at).all()
    return jsonify({
        "status": rr.status,
        "player_name": rr.user.name,
        "messages": [{"sender": m.sender.name, "is_admin": m.sender.is_admin, "message": m.message,
                       "created_at": m.created_at.strftime("%m/%d %H:%M")} for m in msgs],
    })


@app.route("/api/refunds/<int:rid>/messages", methods=["POST"])
@login_required
def api_post_refund_message(rid):
    rr = RefundRequest.query.get_or_404(rid)
    if not _can_access_refund(rr):
        abort(403)
    text = (request.json.get("message") or "").strip()
    if not text:
        return jsonify({"error": "메시지를 입력하세요"}), 400
    m = RefundMessage(refund_request_id=rid, sender_id=current_user.id, message=text)
    db.session.add(m)
    db.session.commit()
    payload = {"sender": current_user.name, "is_admin": current_user.is_admin,
               "message": text, "created_at": m.created_at.strftime("%m/%d %H:%M")}
    socketio.emit("refund_message", payload, room=f"refund_{rid}")
    return jsonify({"ok": True})


@app.route("/api/refunds/<int:rid>/status", methods=["POST"])
@login_required
def api_update_refund_status(rid):
    rr = RefundRequest.query.get_or_404(rid)
    if not current_user.is_admin:
        abort(403)
    status = request.json.get("status")
    if status not in ("요청중", "처리중", "완료", "거절"):
        return jsonify({"error": "잘못된 상태값입니다"}), 400
    rr.status = status
    db.session.commit()
    socketio.emit("refund_status", {"status": status}, room=f"refund_{rid}")
    return jsonify({"ok": True, "status": status})


@app.route("/api/refunds/<int:rid>/end", methods=["POST"])
@login_required
def api_end_refund(rid):
    """관리자가 '채팅 종료'를 누르면 채팅방과 메시지를 완전히 삭제한다."""
    rr = RefundRequest.query.get_or_404(rid)
    if not current_user.is_admin:
        abort(403)
    RefundMessage.query.filter_by(refund_request_id=rid).delete()
    db.session.delete(rr)
    db.session.commit()
    socketio.emit("refund_ended", {}, room=f"refund_{rid}")
    return jsonify({"ok": True})


@app.route("/admin/refunds")
@login_required
def admin_refunds():
    if not current_user.is_admin:
        abort(403)
    # 최고 관리자는 전체 환불 요청, 아니면 자기가 관리 가능한 대회만
    q = RefundRequest.query.join(Tournament)
    if not current_user.is_superadmin:
        q = q.filter(db.or_(
            Tournament.created_by_id == current_user.id,
            db.and_(Tournament.created_by_id.is_(None),
                    Tournament.organizer_id == current_user.organizer_id),
        ))
    requests = q.order_by(RefundRequest.created_at.desc()).all()
    return render_template("admin_refunds.html", requests=requests)


# ---------------------------------------------------------------------------
# API - 콜 기능 (선수 -> 주최자)
# ---------------------------------------------------------------------------

@app.route("/api/tournaments/<int:tid>/call", methods=["POST"])
@login_required
def api_call(tid):
    t = Tournament.query.get_or_404(tid)
    data = request.json or {}
    call = CallLog(tournament_id=tid, match_id=data.get("match_id"),
                    user_id=current_user.id, call_type=data.get("call_type", "etc"),
                    court=data.get("court"),
                    message=(data.get("message") or "").strip() or None)
    db.session.add(call)
    db.session.commit()

    payload = {
        "id": call.id, "player": current_user.name, "court": call.court,
        "type": CALL_TYPES.get(call.call_type, call.call_type),
        "message": call.message or "",
        "tournament_id": tid,
        "tournament_title": t.title,
        "created_at": call.created_at.strftime("%H:%M:%S"),
    }
    # 이 대회의 관리자들이 있는 room 양쪽으로 emit
    socketio.emit("new_call", payload, room=f"tournament_{tid}_admin")
    socketio.emit("call_alert", payload, room=f"tournament_{tid}_alerts")
    return jsonify({"ok": True, "call_id": call.id})


@app.route("/api/calls/<int:cid>", methods=["GET"])
@login_required
def api_get_call(cid):
    """콜 상세 + 대화 목록 조회 (콜 발송자 본인 또는 대회 관리자만)"""
    call = CallLog.query.get_or_404(cid)
    t = Tournament.query.get(call.tournament_id)
    if call.user_id != current_user.id and not (t and can_manage_tournament(current_user, t)):
        abort(403)
    return jsonify({
        "id": call.id, "status": call.status,
        "player": call.user.name if call.user else None,
        "player_id": call.user_id,
        "court": call.court, "message": call.message,
        "type": CALL_TYPES.get(call.call_type, call.call_type),
        "created_at": call.created_at.strftime("%m/%d %H:%M"),
        "replies": [{
            "id": r.id, "sender": r.sender.name if r.sender else "?",
            "is_admin_reply": r.is_admin_reply, "message": r.message,
            "created_at": r.created_at.strftime("%H:%M:%S"),
        } for r in call.replies],
    })


@app.route("/api/calls/<int:cid>/reply", methods=["POST"])
@login_required
def api_call_reply(cid):
    """콜에 메시지로 답장 (관리자↔선수 양방향). 선수는 본인 콜에만, 관리자는 자기 대회 콜에만."""
    call = CallLog.query.get_or_404(cid)
    t = Tournament.query.get(call.tournament_id)
    is_manager = bool(t and can_manage_tournament(current_user, t))
    is_owner = call.user_id == current_user.id
    if not (is_manager or is_owner):
        abort(403)
    msg = (request.json.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "메시지를 입력해주세요."}), 400
    reply = CallReply(call_id=cid, sender_id=current_user.id,
                       is_admin_reply=is_manager, message=msg)
    db.session.add(reply)
    # 관리자가 답장하면 콜 상태를 '확인함'으로
    if is_manager and call.status == "대기":
        call.status = "확인함"
    db.session.commit()
    payload = {
        "call_id": cid, "reply_id": reply.id,
        "sender": current_user.name, "is_admin_reply": is_manager,
        "message": msg, "created_at": reply.created_at.strftime("%H:%M:%S"),
    }
    # 관리자용 room + 콜 당사자용 room 모두에 emit (선수는 자기 콜 방에 join 되어 있음)
    socketio.emit("call_reply", payload, room=f"tournament_{call.tournament_id}_admin")
    socketio.emit("call_reply", payload, room=f"call_{cid}")
    return jsonify({"ok": True, "reply_id": reply.id})


@app.route("/api/calls/<int:cid>/resolve", methods=["POST"])
@login_required
def api_resolve_call(cid):
    call = CallLog.query.get_or_404(cid)
    t = Tournament.query.get(call.tournament_id)
    if not (t and can_manage_tournament(current_user, t)):
        abort(403)
    call.status = request.json.get("status", "처리완료")
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API - 문자 발송
# ---------------------------------------------------------------------------

@app.route("/api/tournaments/<int:tid>/notify", methods=["POST"])
@login_required
def api_notify(tid):
    t = Tournament.query.get_or_404(tid)
    if not can_manage_tournament(current_user, t):
        abort(403)
    message = request.json.get("message") or (
        f"[위단테] {t.title} 예선 안내: {t.event_date} {t.location}. "
        f"자세한 대진은 www.wedante.com 에서 확인하세요."
    )
    player_ids = request.json.get("player_ids", [])
    users = User.query.filter(User.id.in_(player_ids)).all()
    results = []
    for u in users:
        r = send_sms(u.phone, message)
        db.session.add(SmsLog(tournament_id=tid, user_id=u.id, message=message,
                               success=r["success"], provider_response=str(r["raw"])))
        results.append({"user": u.name, "success": r["success"]})
    db.session.commit()
    return jsonify({"results": results})


# ---------------------------------------------------------------------------
# API - 대회 삭제 (최고 관리자 전용)
# ---------------------------------------------------------------------------

@app.route("/api/tournaments/<int:tid>", methods=["DELETE"])
@login_required
def api_delete_tournament(tid):
    t = Tournament.query.get_or_404(tid)
    if not can_delete_tournament(current_user, t):
        abort(403)
    # 관련 리소스 명시적 삭제 (FK 순서: 자식부터)
    try:
        # 환불 채팅 메시지 → 요청 → 참가자 → 콜/문자 로그 → 매치는 cascade
        rr_ids = [r.id for r in RefundRequest.query.filter_by(tournament_id=tid).all()]
        if rr_ids:
            RefundMessage.query.filter(RefundMessage.refund_request_id.in_(rr_ids)).delete(synchronize_session=False)
            RefundRequest.query.filter_by(tournament_id=tid).delete(synchronize_session=False)
        TournamentParticipant.query.filter_by(tournament_id=tid).delete(synchronize_session=False)
        CallLog.query.filter_by(tournament_id=tid).delete(synchronize_session=False)
        SmsLog.query.filter_by(tournament_id=tid).delete(synchronize_session=False)
        # Match 는 Tournament 의 cascade="all, delete-orphan" 로 함께 삭제됨
        db.session.delete(t)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("tournament delete failed")
        return jsonify({"error": "대회 삭제 중 오류가 발생했습니다"}), 500
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API - 대회 생성 권한 부여 (최고 관리자만)
# ---------------------------------------------------------------------------

@app.route("/api/users/<int:uid>/tournament-permission", methods=["POST"])
@login_required
def api_grant_tournament_permission(uid):
    if not current_user.is_superadmin:
        abort(403)
    u = User.query.get_or_404(uid)
    grant = bool(request.json.get("can_create_tournament"))
    u.can_create_tournament = grant
    if grant:
        u.is_admin = True   # 대회 생성 = 관리자 승격
    db.session.commit()
    return jsonify({"ok": True, "can_create_tournament": u.can_create_tournament})


# ---------------------------------------------------------------------------
# API - 멀티테넌트 (타 주최자 카테고리 생성)
# ---------------------------------------------------------------------------

@app.route("/api/organizers", methods=["POST"])
@login_required
def api_create_organizer():
    if not current_user.is_superadmin:
        abort(403)
    data = request.json
    org = Organizer(name=data["name"], slug=data["slug"], contact_phone=data.get("contact_phone"))
    db.session.add(org)
    db.session.commit()
    return jsonify({"ok": True, "id": org.id, "url": f"/org/{org.slug}"})


# ---------------------------------------------------------------------------
# SocketIO - 실시간 룸 구독
# ---------------------------------------------------------------------------

@socketio.on("join_tournament")
def on_join(data):
    tid = data.get("tournament_id")
    if not tid:
        return
    join_room(f"tournament_{tid}")
    # 관리 대시보드에서 명시적으로 as_admin=True 로 붙는 케이스
    if data.get("as_admin"):
        join_room(f"tournament_{tid}_admin")
    # 대회 공개 페이지라도 현재 사용자가 이 대회의 관리자면 알림 room에 참여
    if current_user.is_authenticated:
        t = Tournament.query.get(tid)
        if t and can_manage_tournament(current_user, t):
            join_room(f"tournament_{tid}_alerts")


@socketio.on("join_refund")
def on_join_refund(data):
    rid = data.get("refund_id")
    if rid:
        join_room(f"refund_{rid}")


@socketio.on("join_call")
def on_join_call(data):
    """콜 채팅방 join. 발송자 본인 또는 대회 관리자만."""
    cid = data.get("call_id")
    if not cid or not current_user.is_authenticated:
        return
    call = CallLog.query.get(cid)
    if not call:
        return
    t = Tournament.query.get(call.tournament_id)
    if call.user_id == current_user.id or (t and can_manage_tournament(current_user, t)):
        join_room(f"call_{cid}")


@app.cli.command("init-db")
def init_db():
    """flask --app app init-db  로 실행: 테이블 생성 + 샘플 관리자 계정 생성"""
    db.create_all()
    _apply_lightweight_migrations()
    if not Organizer.query.filter_by(slug="wedante").first():
        org = Organizer(name="위단테", slug="wedante", contact_phone="010-6890-9123")
        db.session.add(org)
        db.session.commit()
        admin = User(name="관리자", phone="01000000000", organizer_id=org.id,
                     is_admin=True, is_superadmin=True, can_create_tournament=True,
                     phone_verified=True)
        admin.set_password("admin1234")
        db.session.add(admin)
        db.session.commit()
        print("DB 초기화 완료. 관리자 로그인: 01000000000 / admin1234")


import click

@app.cli.command("seed-test")
@click.option("--count", default=40, show_default=True, help="생성할 테스트 계정 수")
@click.option("--format", "fmt", default="singles",
              type=click.Choice(["singles", "doubles"]), show_default=True)
@click.option("--tid", type=int, default=None,
              help="기존 대회 ID. 지정하면 새 대회를 만들지 않고 이 대회에 테스트 계정들을 참가시킵니다.")
@click.option("--skip-draw", is_flag=True, default=False,
              help="참가 신청까지만 하고 예선 대진표는 생성하지 않음")
@click.option("--start-time", default=None,
              help="새 대회 시작 시각 HH:MM. 미지정 시 오늘로부터 30분 뒤로 자동 설정")
@click.option("--event-date", default=None, help="새 대회 날짜 YYYY-MM-DD. 미지정 시 오늘")
def seed_test(count, fmt, tid, skip_draw, start_time, event_date):
    """테스트용 계정과 대회를 한 번에 만들거나, 기존 대회에 테스트 계정을 참가시킨다.

    실행 예시:

    - 계정 40명 만들고 새 대회에 자동 참가 + 예선 대진표까지 (기본):
      flask --app app seed-test

    - 이미 만들어진 대회(예: ID=5)에 테스트 계정 40명 참가시키기:
      flask --app app seed-test --tid 5

    - 복식 대회로:
      flask --app app seed-test --format doubles

    - 참가만 시키고 예선 대진표는 안 만들기:
      flask --app app seed-test --skip-draw

    생성물:
    - 최고 관리자: 01099990001 / test1234
    - 대회 관리자: 01099990002 / test1234
    - 선수 계정:   01099990003 ~ 01099990{2+count} / test1234
    """
    from datetime import datetime as _dt, timedelta as _td

    KOREAN_NAMES = [
        "김민수", "이지훈", "박서준", "정예린", "최우진", "장서연", "윤도현", "임지우",
        "한소율", "송지호", "고은서", "노주원", "홍지민", "안유진", "문시우", "양하은",
        "손민재", "배지훈", "조서윤", "이도윤", "권지안", "황은재", "차준서", "오하윤",
        "구태현", "허유진", "남주완", "심다인", "표승우", "우재원", "설아린", "육민준",
        "인채원", "석윤호", "천서희", "국진혁", "여수아", "탁현우", "명나윤", "복지원",
        "선혜린", "종민찬", "환예은", "형지훈", "옥나연",
    ]

    # 1. 위단테 organizer 확보
    org = Organizer.query.filter_by(slug="wedante").first()
    if not org:
        org = Organizer(name="위단테", slug="wedante", contact_phone="010-6890-9123")
        db.session.add(org); db.session.commit()

    # 2. 테스트 최고관리자
    superadmin = User.query.filter_by(phone="01099990001").first()
    if not superadmin:
        superadmin = User(name="테스트최고관리자", phone="01099990001", organizer_id=org.id,
                           is_admin=True, is_superadmin=True, can_create_tournament=True,
                           phone_verified=True)
        superadmin.set_password("test1234")
        db.session.add(superadmin); db.session.commit()

    # 3. 테스트 대회 관리자
    creator = User.query.filter_by(phone="01099990002").first()
    if not creator:
        creator = User(name="테스트관리자", phone="01099990002", organizer_id=org.id,
                        is_admin=True, is_superadmin=False, can_create_tournament=True,
                        phone_verified=True)
        creator.set_password("test1234")
        db.session.add(creator); db.session.commit()

    # 4. 테스트 선수 계정 count 명 (idempotent) - 강점/약점 자동 배정
    # 다양한 플레이 스타일의 프로필 풀 (순환 배정)
    PLAYSTYLES = [
        ("right", "강력한 포핸드, 빠른 서브", "백핸드가 짧고 슬라이스에 취약"),
        ("right", "정확한 서브, 안정적인 랠리", "긴 랠리에서 체력 저하, 발리가 약함"),
        ("left", "회전이 많은 좌완 서브, 예측 어려운 각도", "높은 공(로브)에 대응이 느림"),
        ("right", "탁월한 발리 감각, 빠른 네트 대시", "베이스라인에서 파워 부족"),
        ("right", "안정적인 백핸드 슬라이스, 뛰어난 컨트롤", "체력이 부족해 3세트에 약함"),
        ("left", "강력한 포핸드 크로스, 빠른 풋워크", "백핸드가 불안정하고 오버스매시 실수 잦음"),
        ("right", "포핸드 톱스핀, 리턴 감각 좋음", "서브가 약하고 첫 서브 성공률 낮음"),
        ("right", "인내심 있는 랠리, 상대 실수 유도", "결정타 부족, 공격적 상황에서 소극적"),
        ("left", "다양한 스핀, 슬라이스 서브 위력적", "체격이 작아 파워 매치에 약함"),
        ("right", "빠른 스텝, 코트 커버리지 우수", "포핸드 위닝샷 부족"),
        ("right", "강한 서브와 발리 위주 공격", "긴 랠리에서 실수 증가, 백핸드 리턴 약함"),
        ("right", "안정적인 그라운드스트로크, 침착한 경기 운영", "네트 앞에서 소극적, 서브 파워 부족"),
        ("left", "회전 서브와 각 위주 플레이", "높은 볼과 스매시 대응 취약"),
        ("right", "리턴이 정확하고 빠른 반응 속도", "긴 경기에서 집중력 저하"),
        ("right", "포핸드 인사이드아웃과 로브가 강점", "네트에서 발리 마무리 약함"),
    ]

    created, updated = 0, 0
    players = []
    for i in range(count):
        phone = f"010999900{i+3:02d}" if i + 3 < 100 else f"01099990{i+3:03d}"
        style = PLAYSTYLES[i % len(PLAYSTYLES)]
        hand, strength, weakness = style

        u = User.query.filter_by(phone=phone).first()
        if not u:
            name = KOREAN_NAMES[i % len(KOREAN_NAMES)] + (str(i//len(KOREAN_NAMES)+1) if i >= len(KOREAN_NAMES) else "")
            u = User(name=name, phone=phone, organizer_id=org.id,
                     region="서울" if i % 3 == 0 else ("대구" if i % 3 == 1 else "부산"),
                     club=f"테스트클럽{(i%5)+1}",
                     handedness=hand, strengths=strength, weaknesses=weakness,
                     playstyle_public=True, phone_verified=True)
            u.set_password("test1234")
            db.session.add(u)
            created += 1
        else:
            # 이미 있는 계정: 비어있는 프로필 필드만 채움 (기존 값 유지)
            changed = False
            if not u.handedness:
                u.handedness = hand; changed = True
            if not u.strengths:
                u.strengths = strength; changed = True
            if not u.weaknesses:
                u.weaknesses = weakness; changed = True
            if u.playstyle_public is None:
                u.playstyle_public = True; changed = True
            if changed:
                updated += 1
        players.append(phone)
    db.session.commit()
    click.echo(f"신규 사용자: {created}명 · 기존 계정 프로필 보강: {updated}명 · 총 {count}명")

    # 5. 대회 확보: --tid 지정 시 기존 대회, 아니면 새 대회 생성
    if tid:
        t = Tournament.query.get(tid)
        if not t:
            click.echo(f"❌ 대회 ID={tid} 를 찾을 수 없습니다.")
            return
        click.echo(f"✓ 기존 대회에 참가시킵니다: [{t.id}] {t.title}")
        # 대회 종목이 CLI 옵션과 다르면 안내
        if t.format != fmt:
            click.echo(f"  ⚠ 대회 종목({t.format})이 CLI 옵션({fmt})과 다릅니다. 대회 종목을 따릅니다.")
            fmt = t.format
    else:
        now = _dt.now(KST)
        if not event_date:
            event_date = now.strftime("%Y-%m-%d")
        if not start_time:
            target = now + _td(minutes=30)
            minute = 30 if target.minute >= 30 else 0
            target = target.replace(minute=minute, second=0, microsecond=0)
            if minute == 0 and target < now + _td(minutes=25):
                target += _td(hours=1)
            start_time = target.strftime("%H:%M")

        title = f"테스트 대회 ({now.strftime('%m/%d %H:%M')} 생성)"
        t = Tournament(
            organizer_id=org.id, title=title,
            location="테스트 코트", event_date=event_date,
            entry_fee=0, format=fmt, group_size=3,
            start_time=start_time, match_interval_min=30,
            status="접수중", created_by_id=creator.id,
        )
        db.session.add(t); db.session.commit()
        click.echo(f"✓ 새 대회 생성: [{t.id}] {title}")
        click.echo(f"  · 날짜: {event_date}  · 첫 경기: {start_time}  · 종목: {'단식' if fmt == 'singles' else '복식'}")

    # 6. 모든 테스트 계정을 이 대회에 참가시킨다 (idempotent - 이미 참가한 계정은 스킵)
    #    api_apply_tournament 를 우회하고 직접 삽입: 대회 상태(접수마감/진행중/종료)와 무관하게 강제 참가
    click.echo(f"\n[참가 신청] 테스트 계정 {count}명을 대회에 참가시킵니다...")
    already, applied = 0, 0
    applied_users = []
    for phone in players:
        u = User.query.filter_by(phone=phone).first()
        if not u:
            continue
        existing = TournamentParticipant.query.filter_by(
            tournament_id=t.id, user_id=u.id
        ).first()
        if existing:
            # 이미 참가 - 상태를 확정으로 승격
            if existing.status != "확정":
                existing.status = "확정"
                existing.confirmed_at = datetime.utcnow()
            already += 1
            applied_users.append(u)
            continue
        p = TournamentParticipant(
            tournament_id=t.id, user_id=u.id, status="확정",
            confirmed_at=datetime.utcnow()
        )
        db.session.add(p)
        applied += 1
        applied_users.append(u)
    db.session.commit()
    click.echo(f"  ✓ 신규 참가 {applied}명 + 기존 참가 {already}명 = 총 {applied + already}명 확정 상태")

    # 7. 복식이면 자동 페어링 (이미 파트너 있는 계정은 유지)
    if fmt == "doubles":
        parts = (TournamentParticipant.query
                 .filter_by(tournament_id=t.id)
                 .order_by(TournamentParticipant.id).all())
        # 아직 파트너 없는 참가자만 2명씩 짝짓기
        unpaired = [p for p in parts if not p.partner_id]
        pair_count = 0
        for i in range(0, len(unpaired) - 1, 2):
            unpaired[i].partner_id = unpaired[i+1].user_id
            unpaired[i+1].partner_id = unpaired[i].user_id
            pair_count += 1
        db.session.commit()
        if pair_count > 0:
            click.echo(f"  ✓ 복식 팀 자동 페어링: {pair_count}팀 신규 편성")

    # 8. 예선 대진표 생성 (--skip-draw 아닐 때)
    if not skip_draw:
        # 기존 예선 매치가 있으면 재생성 여부 확인
        existing_prelim = Match.query.filter_by(tournament_id=t.id, stage="prelim").count()
        if existing_prelim:
            click.echo(f"\n⚠ 이 대회에 이미 예선 매치가 {existing_prelim}개 있습니다.")
            if not click.confirm("삭제하고 다시 생성할까요? (No 선택 시 참가 신청까지만 유지)", default=False):
                click.echo("예선 대진표는 유지합니다.")
                _print_summary(t, applied_users, count)
                return
            # 기존 매치 + 예약 SMS 삭제
            match_ids = [m.id for m in Match.query.filter_by(tournament_id=t.id).all()]
            if match_ids:
                CallLog.query.filter(CallLog.match_id.in_(match_ids)).update({"match_id": None}, synchronize_session=False)
            ScheduledSms.query.filter_by(tournament_id=t.id).delete(synchronize_session=False)
            Match.query.filter_by(tournament_id=t.id).delete(synchronize_session=False)
            db.session.commit()
            click.echo(f"  ✓ 기존 예선 매치 {existing_prelim}개 삭제")

        # 예선용 선수(팀 대표) 목록
        if fmt == "doubles":
            parts = TournamentParticipant.query.filter_by(tournament_id=t.id).order_by(TournamentParticipant.id).all()
            # 대표는 pair 별 첫 번째만 (partner 관계에서 id가 더 작은 쪽만 대표로)
            reps = []
            seen = set()
            for p in parts:
                if p.user_id in seen:
                    continue
                if p.partner_id:
                    reps.append(p.user_id)
                    seen.add(p.user_id)
                    seen.add(p.partner_id)
            player_ids_for_draw = reps
        else:
            player_ids_for_draw = [u.id for u in applied_users]

        if len(player_ids_for_draw) < 2:
            click.echo(f"❌ 대진표를 만들려면 최소 2명(팀) 필요합니다. 현재: {len(player_ids_for_draw)}")
            return

        click.echo(f"\n[대진표] {len(player_ids_for_draw)}{'팀' if fmt=='doubles' else '명'} 예선 대진표 생성 중...")
        generate_prelim_groups(t, player_ids_for_draw)

        prelim_count = Match.query.filter_by(tournament_id=t.id, stage="prelim").count()
        court_ids = set(m.court for m in Match.query.filter_by(tournament_id=t.id, stage="prelim").all() if m.court)
        sms_count = ScheduledSms.query.filter_by(tournament_id=t.id, sent=False).count()
        click.echo(f"  ✓ 예선 매치 {prelim_count}건, 코트 {len(court_ids)}개, SMS 예약 {sms_count}건")

        # 대회 상태를 진행중으로
        if t.status == "접수중":
            t.status = "진행중"
            db.session.commit()
            click.echo(f"  ✓ 대회 상태를 '진행중'으로 변경")

    _print_summary(t, applied_users, count)


def _print_summary(t, applied_users, count):
    """seed-test 결과 요약 출력"""
    click.echo(f"\n{'='*60}")
    click.echo("✅ 완료")
    click.echo(f"{'='*60}")
    click.echo(f"🔑 로그인 정보 (비밀번호 모두 test1234)")
    click.echo(f"  - 최고 관리자: 01099990001")
    click.echo(f"  - 대회 관리자: 01099990002")
    click.echo(f"  - 선수 1번:   01099990003")
    click.echo(f"  - 선수 N번:   010999900{count+2:02d}  ← 마지막 계정")
    click.echo(f"")
    click.echo(f"🏆 대회: {t.title}")
    click.echo(f"  - ID:       {t.id}")
    click.echo(f"  - 상태:     {t.status}")
    click.echo(f"  - 참가자:   {len(applied_users)}명 (모두 확정)")
    click.echo(f"  - URL:      /tournament/{t.id}")
    click.echo(f"  - 관리:     /admin/tournament/{t.id}")
    click.echo(f"{'='*60}\n")


@app.cli.command("clear-test")
@click.option("--yes", is_flag=True, default=False, help="확인 프롬프트 건너뛰기")
def clear_test(yes):
    """seed-test 로 만든 테스트 계정만 삭제한다.
    전화번호 010-9999-00** 로 시작하는 계정과 그들이 만든 대회를 정리."""
    test_users = User.query.filter(User.phone.like("01099990%")).all()
    test_ids = [u.id for u in test_users]
    if not test_ids:
        click.echo("삭제할 테스트 계정이 없습니다.")
        return
    click.echo(f"삭제 대상: 사용자 {len(test_ids)}명 + 관련 대회/참가/매치/문자 로그")
    if not yes:
        click.confirm("계속할까요?", abort=True)
    # 이 사용자들이 만든 대회
    t_ids = [t.id for t in Tournament.query.filter(Tournament.created_by_id.in_(test_ids)).all()]
    if t_ids:
        m_ids = [m.id for m in Match.query.filter(Match.tournament_id.in_(t_ids)).all()]
        if m_ids:
            CallLog.query.filter(CallLog.match_id.in_(m_ids)).update({"match_id": None}, synchronize_session=False)
        ScheduledSms.query.filter(ScheduledSms.tournament_id.in_(t_ids)).delete(synchronize_session=False)
        RefundMessage.query.filter(RefundMessage.refund_request_id.in_([r.id for r in RefundRequest.query.filter(RefundRequest.tournament_id.in_(t_ids)).all()])).delete(synchronize_session=False)
        RefundRequest.query.filter(RefundRequest.tournament_id.in_(t_ids)).delete(synchronize_session=False)
        TournamentParticipant.query.filter(TournamentParticipant.tournament_id.in_(t_ids)).delete(synchronize_session=False)
        CallLog.query.filter(CallLog.tournament_id.in_(t_ids)).delete(synchronize_session=False)
        SmsLog.query.filter(SmsLog.tournament_id.in_(t_ids)).delete(synchronize_session=False)
        Match.query.filter(Match.tournament_id.in_(t_ids)).delete(synchronize_session=False)
        Tournament.query.filter(Tournament.id.in_(t_ids)).delete(synchronize_session=False)
    # 참가/파트너 관계도 정리
    TournamentParticipant.query.filter(TournamentParticipant.user_id.in_(test_ids)).delete(synchronize_session=False)
    User.query.filter(User.id.in_(test_ids)).delete(synchronize_session=False)
    db.session.commit()
    click.echo(f"✓ 테스트 데이터 정리 완료")


@app.cli.command("delete-user")
@click.argument("phones", nargs=-1, required=True)
@click.option("--yes", is_flag=True, default=False, help="확인 프롬프트 건너뛰기")
def delete_user(phones, yes):
    """전화번호로 특정 사용자 계정과 관련 데이터를 삭제한다.

    실행 예시:
      flask --app app delete-user 01012345678
      flask --app app delete-user 01011111111 01022222222   (여러 명)
      flask --app app delete-user 01012345678 --yes         (확인 없이)

    함께 삭제되는 데이터:
      - 대회 참가 이력, 콜 + 답장, 환불 요청/메시지, 문자 로그, 예약된 문자, 전화 인증 토큰
      - 이 사용자가 만든 대회는 유지 (created_by_id 만 NULL 처리)
      - 최고 관리자(is_superadmin) 계정은 안전을 위해 삭제 거부"""
    if not phones:
        click.echo("전화번호를 하나 이상 지정하세요.")
        return

    # 하이픈/공백 제거해서 정규화
    def normalize(p):
        return "".join(ch for ch in (p or "") if ch.isdigit())

    targets = []
    not_found = []
    protected = []
    for raw in phones:
        norm = normalize(raw)
        u = User.query.filter_by(phone=norm).first()
        if not u:
            not_found.append(raw)
            continue
        if u.is_superadmin:
            protected.append(u)
            continue
        targets.append(u)

    if not_found:
        click.echo(f"⚠ 찾을 수 없는 전화번호: {', '.join(not_found)}")
    if protected:
        click.echo(f"⚠ 최고 관리자(is_superadmin) 계정이라 삭제 불가:")
        for u in protected:
            click.echo(f"    - {u.name} ({u.phone})")

    if not targets:
        click.echo("삭제할 대상이 없습니다.")
        return

    click.echo(f"\n[삭제 대상] {len(targets)}명")
    for u in targets:
        parts_count = TournamentParticipant.query.filter_by(user_id=u.id).count()
        calls_count = CallLog.query.filter_by(user_id=u.id).count()
        refunds_count = RefundRequest.query.filter_by(user_id=u.id).count()
        created_tournaments = Tournament.query.filter_by(created_by_id=u.id).count()
        click.echo(f"  - {u.name} ({u.phone})"
                    f" · 참가:{parts_count}건 · 콜:{calls_count}건 · 환불:{refunds_count}건"
                    f"{' · 생성대회:' + str(created_tournaments) + '건(유지됨)' if created_tournaments else ''}")

    if not yes:
        click.confirm("\n정말로 위 계정을 삭제할까요? (되돌릴 수 없음)", abort=True)

    for u in targets:
        uid = u.id
        # 참가자 이력 (파트너 관계 정리 포함)
        # 이 사용자가 파트너로 지정된 다른 참가자 정보를 NULL 처리
        TournamentParticipant.query.filter_by(partner_id=uid).update(
            {"partner_id": None}, synchronize_session=False)
        # 이 사용자의 참가 이력 삭제
        TournamentParticipant.query.filter_by(user_id=uid).delete(synchronize_session=False)

        # 콜 답장 → 콜 → 그 순서로
        my_calls = CallLog.query.filter_by(user_id=uid).all()
        for c in my_calls:
            CallReply.query.filter_by(call_id=c.id).delete(synchronize_session=False)
        # 이 사용자가 남긴 다른 콜의 답장
        CallReply.query.filter_by(sender_id=uid).delete(synchronize_session=False)
        CallLog.query.filter_by(user_id=uid).delete(synchronize_session=False)

        # 환불 요청/메시지
        my_refunds = RefundRequest.query.filter_by(user_id=uid).all()
        for r in my_refunds:
            RefundMessage.query.filter_by(refund_request_id=r.id).delete(synchronize_session=False)
        # 이 사용자가 남긴 다른 환불의 메시지
        RefundMessage.query.filter_by(sender_id=uid).delete(synchronize_session=False)
        RefundRequest.query.filter_by(user_id=uid).delete(synchronize_session=False)

        # 문자 로그, 예약 문자, 전화 인증 토큰
        SmsLog.query.filter_by(user_id=uid).delete(synchronize_session=False)
        ScheduledSms.query.filter_by(user_id=uid).delete(synchronize_session=False)
        PhoneVerification.query.filter_by(phone=u.phone).delete(synchronize_session=False)

        # 매치 슬롯에서 이 사용자 흔적 제거 (경기 결과 보존 위해 이름만 NULL 처리)
        Match.query.filter_by(player1_id=uid).update({"player1_id": None}, synchronize_session=False)
        Match.query.filter_by(player2_id=uid).update({"player2_id": None}, synchronize_session=False)
        Match.query.filter_by(winner_id=uid).update({"winner_id": None}, synchronize_session=False)
        Match.query.filter_by(player1_partner_id=uid).update({"player1_partner_id": None}, synchronize_session=False)
        Match.query.filter_by(player2_partner_id=uid).update({"player2_partner_id": None}, synchronize_session=False)

        # 이 사용자가 만든 대회는 유지하되 created_by_id 만 NULL
        Tournament.query.filter_by(created_by_id=uid).update(
            {"created_by_id": None}, synchronize_session=False)
        # 이 사용자가 우승자로 기록된 대회도 유지하되 winner_id NULL
        Tournament.query.filter_by(winner_id=uid).update(
            {"winner_id": None}, synchronize_session=False)

        # QuickIcon.created_by 등록자도 유지 (icon 자체는 유지)
        QuickIcon.query.filter_by(created_by=uid).update(
            {"created_by": None}, synchronize_session=False)

        # 마지막으로 사용자 자체 삭제
        db.session.delete(u)

    db.session.commit()
    click.echo(f"\n✓ {len(targets)}명 삭제 완료")


@app.cli.command("find-user")
@click.argument("query", required=True)
def find_user(query):
    """사용자를 이름/전화번호 부분 일치로 검색해서 표시. 삭제 전 확인용.

    실행: flask --app app find-user 010     (전화번호 부분)
          flask --app app find-user 홍길동   (이름)"""
    from sqlalchemy import or_
    q = query.strip()
    users = (User.query
             .filter(or_(User.name.ilike(f"%{q}%"), User.phone.ilike(f"%{q}%")))
             .order_by(User.id).all())
    if not users:
        click.echo("일치하는 사용자가 없습니다.")
        return
    click.echo(f"\n{len(users)}명 발견:")
    for u in users:
        role = ""
        if u.is_superadmin: role = " [최고관리자]"
        elif u.is_admin: role = " [관리자]"
        elif u.can_create_tournament: role = " [대회생성권한]"
        click.echo(f"  {u.id:>4}  {u.phone:<15}  {u.name}{role}"
                    f"  ({u.region or '-'}, {u.club or '-'})")


@app.cli.command("reset-data")
@click.option("--keep-banners", is_flag=True, default=False,
              help="배너/아이콘/주최자 카테고리는 남기고 대회/회원만 삭제")
@click.option("--yes", is_flag=True, default=False,
              help="확인 프롬프트 건너뛰기 (스크립트/CI 용)")
def reset_data(keep_banners, yes):
    """최고 관리자(is_superadmin=True) 계정만 남기고 모든 데이터를 삭제합니다.
    실행: flask --app app reset-data
    옵션: --keep-banners 로 배너/아이콘/주최자 유지 가능"""

    # 어떤 것들이 지워질지 미리 카운트
    keep_users = User.query.filter_by(is_superadmin=True).all()
    keep_ids = {u.id for u in keep_users}
    to_delete_users = User.query.filter(~User.id.in_(keep_ids)).count() if keep_ids else User.query.count()

    click.echo("\n[reset-data] 삭제될 데이터:")
    click.echo(f"  - 대회 (Tournament):        {Tournament.query.count()}")
    click.echo(f"  - 매치 (Match):             {Match.query.count()}")
    click.echo(f"  - 참가자 신청:              {TournamentParticipant.query.count()}")
    click.echo(f"  - 환불 요청/메시지:         {RefundRequest.query.count()} / {RefundMessage.query.count()}")
    click.echo(f"  - 콜 로그 / 문자 로그:      {CallLog.query.count()} / {SmsLog.query.count()}")
    click.echo(f"  - 전화 인증 토큰:           {PhoneVerification.query.count()}")
    click.echo(f"  - 회원 (관리자 제외):       {to_delete_users}")
    if not keep_banners:
        click.echo(f"  - 배너 / 아이콘:            {Banner.query.count()} / {QuickIcon.query.count()}")
        # 최고관리자가 다른 organizer 에 속하지 않는 경우 위단테만 유지
        keep_org_ids = {u.organizer_id for u in keep_users if u.organizer_id}
        others = Organizer.query.count() - len(keep_org_ids)
        click.echo(f"  - 주최자 카테고리 (관리자 소속 외): {others}")
    click.echo(f"\n[유지될 최고 관리자]: {len(keep_users)}명")
    for u in keep_users:
        click.echo(f"  · {u.name} ({u.phone})")

    if not keep_users:
        click.echo("\n⚠️  is_superadmin=True 인 계정이 없습니다. 이대로 진행하면 로그인할 계정이 사라집니다.")
        if not yes:
            click.confirm("그래도 진행할까요?", abort=True)

    if not yes:
        click.confirm("\n정말로 위 데이터를 삭제할까요? (되돌릴 수 없음)", abort=True)

    # 자식 → 부모 순서로 삭제
    click.echo("\n삭제 진행 중...")
    RefundMessage.query.delete(synchronize_session=False)
    RefundRequest.query.delete(synchronize_session=False)
    TournamentParticipant.query.delete(synchronize_session=False)
    CallLog.query.delete(synchronize_session=False)
    SmsLog.query.delete(synchronize_session=False)
    PhoneVerification.query.delete(synchronize_session=False)
    # Match 는 Tournament cascade 로 함께 삭제됨. 안전 위해 명시적으로 먼저.
    Match.query.delete(synchronize_session=False)
    Tournament.query.delete(synchronize_session=False)
    db.session.commit()

    # 회원 삭제 (최고관리자 제외)
    if keep_ids:
        User.query.filter(~User.id.in_(keep_ids)).delete(synchronize_session=False)
    else:
        User.query.delete(synchronize_session=False)
    db.session.commit()

    if not keep_banners:
        Banner.query.delete(synchronize_session=False)
        QuickIcon.query.delete(synchronize_session=False)
        # 최고관리자가 소속된 organizer 만 유지
        keep_org_ids = {u.organizer_id for u in keep_users if u.organizer_id}
        if keep_org_ids:
            Organizer.query.filter(~Organizer.id.in_(keep_org_ids)).delete(synchronize_session=False)
        else:
            Organizer.query.delete(synchronize_session=False)
        db.session.commit()

    click.echo("✓ 삭제 완료. 최고 관리자 계정만 남아있습니다.")


@app.route("/admin/users")
@login_required
def admin_users_page():
    """관리자용 전체 회원 리스트 페이지."""
    if not current_user.is_admin:
        abort(403)
    return render_template("admin_users.html")


@app.route("/api/admin/users")
@login_required
def api_admin_users_list():
    """관리자용 회원 리스트 API - 검색/필터/페이지네이션 지원."""
    if not current_user.is_admin:
        abort(403)
    from sqlalchemy import or_
    q = (request.args.get("q") or "").strip()
    role = request.args.get("role") or "all"    # all | admin | player
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50

    query = User.query
    if q:
        query = query.filter(or_(
            User.name.ilike(f"%{q}%"),
            User.phone.ilike(f"%{q}%"),
            User.club.ilike(f"%{q}%"),
            User.region.ilike(f"%{q}%"),
        ))
    if role == "admin":
        query = query.filter(User.is_admin.is_(True))
    elif role == "player":
        query = query.filter(User.is_admin.is_(False))

    total = query.count()
    users = (query.order_by(User.id.desc())
             .offset((page - 1) * per_page)
             .limit(per_page).all())

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page,
        "users": [{
            "id": u.id,
            "name": u.name,
            "phone": u.phone,
            "region": u.region or "",
            "club": u.club or "",
            "is_admin": u.is_admin,
            "is_superadmin": u.is_superadmin,
            "can_create_tournament": u.can_create_tournament,
            "phone_verified": u.phone_verified,
            "created_at": u.created_at.strftime("%Y-%m-%d") if u.created_at else "",
        } for u in users]
    })


@app.route("/analyze", methods=["GET"])
@login_required
def analyze_opponent_page():
    """상대선수 분석 페이지 (검색 폼)."""
    return render_template("analyze.html", result=None, query=None, opponent=None)


@app.route("/api/analyze/search", methods=["GET"])
@login_required
def api_analyze_search():
    """이름/전화번호 부분 일치로 분석 대상 선수 목록 조회 (자동완성용).
    playstyle_public=True 인 사용자만 노출."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []})
    from sqlalchemy import or_
    users = (User.query
             .filter(User.playstyle_public.is_(True))
             .filter(User.id != current_user.id)  # 본인 제외
             .filter(User.is_admin.is_(False))    # 관리자 계정 제외
             .filter(or_(User.name.ilike(f"%{q}%"),
                          User.phone.ilike(f"%{q}%")))
             .limit(20).all())
    return jsonify({
        "results": [{
            "id": u.id, "name": u.name,
            "region": u.region or "", "club": u.club or "",
        } for u in users]
    })


@app.route("/api/analyze/<int:opponent_id>", methods=["POST"])
@login_required
def api_analyze_opponent(opponent_id):
    """상대선수 분석 실행. Gemini API 호출."""
    from gemini import analyze_opponent
    op = User.query.get_or_404(opponent_id)
    if op.id == current_user.id:
        return jsonify({"error": "본인을 분석할 수는 없습니다."}), 400
    if not op.playstyle_public:
        return jsonify({"error": "이 선수는 분석 정보 공개를 허용하지 않았습니다."}), 403

    me = {
        "name": current_user.name,
        "handedness": current_user.handedness,
        "strengths": current_user.strengths,
        "weaknesses": current_user.weaknesses,
        "region": current_user.region, "club": current_user.club,
    }
    opp = {
        "name": op.name,
        "handedness": op.handedness,
        "strengths": op.strengths,
        "weaknesses": op.weaknesses,
        "region": op.region, "club": op.club,
    }
    r = analyze_opponent(me, opp)
    return jsonify({
        "success": r["success"],
        "text": r.get("text"),
        "opponent": {
            "id": op.id, "name": op.name,
            "region": op.region or "", "club": op.club or "",
            "handedness": op.handedness or "",
            "strengths": op.strengths or "", "weaknesses": op.weaknesses or "",
        },
        "hint": None if r["success"] else "AI 분석 서비스가 미설정 상태거나 응답하지 못했습니다. 기본 분석을 대신 표시합니다.",
    })


# gunicorn 부트 시(WSGI 임포트만으로 앱 시작) 스키마를 최신화한다
with app.app_context():
    try:
        db.create_all()
        _apply_lightweight_migrations()
    except Exception:
        app.logger.exception("boot-time schema sync failed")

# 백그라운드 SMS 발송 워커 시작 (eventlet 있으면)
_start_sms_dispatcher()


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    socketio.run(app, debug=debug_mode, host="0.0.0.0", port=5000)

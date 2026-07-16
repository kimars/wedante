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
from sqlalchemy import inspect as sqla_inspect, text
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
        ("tournaments", "created_by_id", "INTEGER"),
        ("tournaments", "winner_id", "INTEGER"),
        ("tournaments", "winner_pair", "VARCHAR(120)"),
        ("tournaments", "format", "VARCHAR(10) DEFAULT 'singles'"),
        ("tournaments", "start_time", "VARCHAR(10) DEFAULT '09:00'"),
        ("tournaments", "match_interval_min", "INTEGER DEFAULT 30"),
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
}


def next_power_of_two(n):
    return 1 if n <= 1 else 2 ** math.ceil(math.log2(n))


def build_main_bracket(tournament_id, slot_count):
    """빈 본선 토너먼트 트리를 생성하고 각 라운드를 next_match_id로 연결한다.
    반환값: 1라운드 매치 리스트 (조 우승자가 채워질 슬롯)
    조가 1개뿐이어도(=참가자가 적어 그룹이 하나만 만들어져도) 최소 2슬롯(결승 1경기)은
    항상 만들어서 첫 조 슬롯 배정 시 인덱스 에러가 나지 않게 한다."""
    size = next_power_of_two(max(slot_count, 2))
    round_names = ROUND_NAMES_BY_SIZE.get(size, [f"{size}강"] + ["결승"])

    rounds = []  # rounds[i] = list[Match] for round i
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
    db.session.flush()  # ids 확정

    # 라운드끼리 연결: rounds[r][i] 의 승자 -> rounds[r+1][i//2] 의 slot (i%2)+1
    for r in range(len(rounds) - 1):
        for i, match in enumerate(rounds[r]):
            nxt = rounds[r + 1][i // 2]
            match.next_match_id = nxt.id
            match.next_match_slot = (i % 2) + 1
    db.session.commit()
    return rounds[0]  # 1라운드 = 조 우승자가 배정될 자리


def _first_round_main_matches(tournament_id):
    """본선 첫 라운드(가장 매치가 많은 라운드)의 매치들을 match_no 순으로 반환.
    조 우승자를 넣을 슬롯을 찾기 위한 결정론적 계산에 사용."""
    main = Match.query.filter_by(tournament_id=tournament_id, stage="main").all()
    if not main:
        return []
    by_round = {}
    for m in main:
        by_round.setdefault(m.round_name, []).append(m)
    # 가장 매치가 많은 라운드 = 1라운드
    first = max(by_round.values(), key=len)
    return sorted(first, key=lambda m: m.match_no)


def _set_group_winner_slot(tournament_id, group_no, winner_id):
    """조 우승자를 본선 첫 라운드 매치의 해당 슬롯에 배치. commit/emit은 호출자 책임."""
    first_round = _first_round_main_matches(tournament_id)
    if not first_round:
        return
    idx = (group_no - 1) // 2
    slot = ((group_no - 1) % 2) + 1
    if idx >= len(first_round):
        return
    slot_match = first_round[idx]
    if slot == 1:
        slot_match.player1_id = winner_id
    else:
        slot_match.player2_id = winner_id


def record_group_winner(tournament: Tournament, group_no: int, winner_id: int):
    """조별 예선이 끝나고 주최자가 우승자를 확정하면, 본선 대진표 해당 슬롯에 자동 반영."""
    _set_group_winner_slot(tournament.id, group_no, winner_id)
    db.session.commit()
    socketio.emit("bracket_update", {"tournament_id": tournament.id},
                   room=f"tournament_{tournament.id}")


def _assign_courts_and_schedule(tournament: Tournament, groups_count: int):
    """대회 예선 매치들에 코트와 시각을 자동 배정한다.
    규칙:
    - 코트 하나에 2개 조가 함께 배정 (총 6경기)
    - 조1의 seq1(경기1) → 조2의 seq1(경기2) → 조1의 seq2(경기3) → 조2의 seq2(경기4) → 조1의 seq3(경기5) → 조2의 seq3(경기6) 순
    - 시각은 tournament.start_time 부터 match_interval_min(기본 30분)씩 증가
    호출자는 이후 db.session.commit() 해야 한다."""
    from datetime import datetime, timedelta

    interval = tournament.match_interval_min or 30
    try:
        base = datetime.strptime(tournament.start_time or "09:00", "%H:%M")
    except ValueError:
        base = datetime.strptime("09:00", "%H:%M")

    # 두 조씩 짝지어 하나의 코트에 배정
    court_no = 1
    for pair_start in range(1, groups_count + 1, 2):
        group_a = pair_start                      # 항상 존재
        group_b = pair_start + 1 if pair_start + 1 <= groups_count else None
        # 이 코트에서 진행될 총 매치 리스트를 seq_in_group 기준으로 만듦
        # 코트 순서: A-1, B-1, A-2, B-2, A-3, B-3
        court_seq = 0
        max_seq = 3   # 조당 최대 경기 수 (3경기 방식)
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
    # 로그도 남김
    db.session.add(SmsLog(
        tournament_id=None, user_id=current_user.id, message=message,
        success=bool(r.get("success")), provider_response=str(r.get("raw"))[:500]
    ))
    db.session.commit()
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
    organizers = Organizer.query.all()
    tournaments = Tournament.query.order_by(Tournament.event_date.desc()).limit(20).all()
    banners = Banner.query.filter_by(is_active=True).order_by(Banner.sort_order, Banner.id).all()
    icons = QuickIcon.query.filter_by(is_active=True).order_by(QuickIcon.sort_order, QuickIcon.id).limit(9).all()
    return render_template("index.html", organizers=organizers, tournaments=tournaments,
                            banners=banners, icons=icons)


@app.route("/org/<slug>")
def organizer_page(slug):
    org = Organizer.query.filter_by(slug=slug).first_or_404()
    tournaments = Tournament.query.filter_by(organizer_id=org.id).order_by(
        Tournament.event_date.desc()).all()
    return render_template("index.html", organizers=[org], tournaments=tournaments,
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
    """조 내 모든 경기 종료 시 조 우승자를 자동 결정하고 본선에 반영.
    - 3경기 방식이면 seq=2 매치(승자결정전)의 승자가 조 우승자
    - 라운드로빈이면 승수 최다자
    - 2명이면 유일 매치의 승자"""
    matches = (Match.query
               .filter_by(tournament_id=tournament_id, stage="prelim", group_no=group_no)
               .order_by(Match.seq_in_group.nulls_last()).all())
    if not matches or any(x.status != "종료" for x in matches):
        return None
    # 3경기 방식: seq=2 매치(승자결정전) 승자가 조 우승자
    if len(matches) == 3 and any(x.seq_in_group == 2 for x in matches):
        m2 = next(x for x in matches if x.seq_in_group == 2)
        return m2.winner_id
    # 2명: 유일 매치의 승자
    if len(matches) == 1:
        return matches[0].winner_id
    # 라운드로빈: 승수 최다자
    wins = {}
    for m in matches:
        if m.winner_id:
            wins[m.winner_id] = wins.get(m.winner_id, 0) + 1
    if not wins:
        return None
    return max(wins, key=wins.get)


def _schedule_next_match_notification(match: Match):
    """[Deprecated] 예전에는 경기 '시작 후' 20분 뒤 다음 조원에게 문자를 보냈으나,
    이제는 시간표 자동 배정 시 각 매치 '시작 20분 前' 발송으로 대체됨.
    (호환용으로 남겨두었으며 별도로 호출되지 않음)"""
    return


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
    return jsonify({"ok": True, "started_at": m.started_at.isoformat() if m.started_at else None})


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
        # 조 전체가 끝났으면 조 우승자를 본선 슬롯에 자동 반영
        winner_id = _finalize_prelim_group(m.tournament_id, m.group_no)
        if winner_id:
            _set_group_winner_slot(m.tournament_id, m.group_no, winner_id)
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

    def m2d(m):
        return {
            "id": m.id, "stage": m.stage, "group_no": m.group_no,
            "seq_in_group": m.seq_in_group,
            "round_name": m.round_name, "match_no": m.match_no,
            "player1": m.player1.name if m.player1 else None,
            "player1_id": m.player1_id,
            "player1_partner": m.player1_partner.name if m.player1_partner else None,
            "player1_partner_id": m.player1_partner_id,
            "player2": m.player2.name if m.player2 else None,
            "player2_id": m.player2_id,
            "player2_partner": m.player2_partner.name if m.player2_partner else None,
            "player2_partner_id": m.player2_partner_id,
            "score": m.score, "status": m.status,
            "winner_id": m.winner_id,
            "court": m.court, "court_seq": m.court_seq,
            "scheduled_time": m.scheduled_time,
            "started_at": m.started_at.isoformat() if m.started_at else None,
            "duration_min": m.duration_min or 20,
            "source_match_id": m.source_match_id,
            "source_role": m.source_role,
        }
    return jsonify({"prelim": [m2d(m) for m in prelim], "main": [m2d(m) for m in main]})


@app.route("/api/matches/<int:mid>/schedule", methods=["POST"])
@login_required
def api_schedule_match(mid):
    """관리자가 경기에 코트 번호와 시간을 배정 (본선 코트진행 기능)"""
    m = Match.query.get_or_404(mid)
    if not can_manage_tournament(current_user, m.tournament):
        abort(403)
    data = request.json
    m.court = data.get("court") or None
    m.scheduled_time = data.get("scheduled_time") or None
    if data.get("status"):
        m.status = data["status"]
    db.session.commit()
    socketio.emit("bracket_update", {"tournament_id": m.tournament_id},
                   room=f"tournament_{m.tournament_id}")
    return jsonify({"ok": True})


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
@click.option("--auto-draw", is_flag=True, default=False,
              help="자동으로 참가 신청 → 확정 → 예선 대진표까지 생성")
@click.option("--start-time", default=None,
              help="대회 시작 시각 HH:MM. 미지정 시 오늘로부터 30분 뒤로 자동 설정(SMS 예약 실동작 확인용)")
@click.option("--event-date", default=None, help="대회 날짜 YYYY-MM-DD. 미지정 시 오늘")
def seed_test(count, fmt, auto_draw, start_time, event_date):
    """테스트용 계정과 대회를 한 번에 만든다.

    실행: flask --app app seed-test --count 40 --format singles --auto-draw

    생성물:
    - 테스트 최고관리자: 01099990001 / test1234
    - 테스트 관리자(대회 소유자): 01099990002 / test1234
    - 테스트 선수 계정: 01099990003 ~ 01099990{2+count} / 비밀번호 모두 test1234
    - 대회 하나 (기본: 오늘 날짜, 30분 후 시작)
    - --auto-draw 시 참가 신청 → 확정 → 예선 대진표까지 자동 생성
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

    # 4. 테스트 선수 계정 count 명
    created = 0
    players = []
    for i in range(count):
        phone = f"010999900{i+3:02d}" if i + 3 < 100 else f"01099990{i+3:03d}"
        u = User.query.filter_by(phone=phone).first()
        if not u:
            name = KOREAN_NAMES[i % len(KOREAN_NAMES)] + (str(i//len(KOREAN_NAMES)+1) if i >= len(KOREAN_NAMES) else "")
            u = User(name=name, phone=phone, organizer_id=org.id,
                     region="서울" if i % 3 == 0 else ("대구" if i % 3 == 1 else "부산"),
                     club=f"테스트클럽{(i%5)+1}",
                     phone_verified=True)
            u.set_password("test1234")
            db.session.add(u)
            created += 1
        players.append(phone)
    db.session.commit()

    # 5. 대회 하나 만들기
    now = _dt.now(KST)
    if not event_date:
        event_date = now.strftime("%Y-%m-%d")
    if not start_time:
        # 지금으로부터 30분 뒤 시각 (SMS는 10분 뒤에 발송 시작)
        target = now + _td(minutes=30)
        # 30분 단위로 반올림 (:00 또는 :30)
        minute = 30 if target.minute >= 30 else 0
        target = target.replace(minute=minute, second=0, microsecond=0)
        if minute == 0 and target.minute == 0 and target < now + _td(minutes=25):
            target += _td(hours=1)
        start_time = target.strftime("%H:%M")

    title = f"테스트 대회 ({now.strftime('%m/%d %H:%M')} 생성)"
    t = Tournament(
        organizer_id=org.id, title=title,
        location="테스트 코트", event_date=event_date,
        entry_fee=0,                   # 무료 (즉시 확정 처리 위해)
        format=fmt, group_size=3,
        start_time=start_time, match_interval_min=30,
        status="접수중",
        created_by_id=creator.id,
    )
    db.session.add(t); db.session.commit()

    click.echo(f"\n{'='*60}")
    click.echo("✅ 테스트 데이터 생성 완료")
    click.echo(f"{'='*60}")
    click.echo(f"신규 사용자: {created}명 (기존 계정은 유지)")
    click.echo(f"전체 테스트 계정: {count}명")
    click.echo(f"")
    click.echo(f"🔑 로그인 정보 (비밀번호는 모두 test1234)")
    click.echo(f"  - 최고 관리자: 01099990001 / test1234")
    click.echo(f"  - 대회 관리자: 01099990002 / test1234")
    click.echo(f"  - 선수 1번:   01099990003 / test1234")
    click.echo(f"  - 선수 2번:   01099990004 / test1234")
    click.echo(f"  - ...")
    click.echo(f"  - 선수 {count}번: 010999900{count+2:02d} / test1234")
    click.echo(f"")
    click.echo(f"🏆 생성된 대회: {title}")
    click.echo(f"  - 대회 ID:    {t.id}")
    click.echo(f"  - 종목:       {'단식' if fmt == 'singles' else '복식'}")
    click.echo(f"  - 날짜:       {event_date}")
    click.echo(f"  - 첫 경기:    {start_time}")
    click.echo(f"  - 상태:       {t.status}")
    click.echo(f"  - URL:        /tournament/{t.id}")

    # 6. --auto-draw 옵션: 자동으로 신청 + 확정 + 예선 생성
    if auto_draw:
        click.echo(f"\n[auto-draw] 참가 신청 자동 처리...")
        # 참가비 0원이므로 즉시 확정 상태로 등록
        applied = 0
        applied_users = []
        for phone in players:
            u = User.query.filter_by(phone=phone).first()
            if not u:
                continue
            if TournamentParticipant.query.filter_by(
                tournament_id=t.id, user_id=u.id
            ).first():
                continue
            p = TournamentParticipant(
                tournament_id=t.id, user_id=u.id, status="확정",
                confirmed_at=datetime.utcnow()
            )
            db.session.add(p)
            applied += 1
            applied_users.append(u)
        db.session.commit()
        click.echo(f"  ✓ {applied}명 신청+확정 처리")

        # 복식이면 자동 페어링
        if fmt == "doubles":
            parts = TournamentParticipant.query.filter_by(tournament_id=t.id).order_by(TournamentParticipant.id).all()
            # 2명씩 짝지음
            for i in range(0, len(parts) - 1, 2):
                parts[i].partner_id = parts[i+1].user_id
                parts[i+1].partner_id = parts[i].user_id
            db.session.commit()
            click.echo(f"  ✓ 복식 팀 자동 페어링 ({len(parts)//2}팀)")
            # 예선용 팀 대표 목록
            team_leads = [parts[i].user_id for i in range(0, len(parts) - 1, 2)]
            player_ids_for_draw = team_leads
        else:
            player_ids_for_draw = [u.id for u in applied_users]

        # 예선 대진표 생성
        click.echo(f"  ⏳ 예선 대진표 생성 중...")
        generate_prelim_groups(t, player_ids_for_draw)

        prelim_count = Match.query.filter_by(tournament_id=t.id, stage="prelim").count()
        court_count = len(set(m.court for m in Match.query.filter_by(tournament_id=t.id, stage="prelim").all() if m.court))
        sms_count = ScheduledSms.query.filter_by(tournament_id=t.id, sent=False).count()

        click.echo(f"  ✓ 예선 매치 {prelim_count}건, 코트 {court_count}개, SMS 예약 {sms_count}건")
        click.echo(f"\n대회 상태를 '진행중'으로 변경합니다...")
        t.status = "진행중"
        db.session.commit()

    click.echo(f"\n{'='*60}")
    click.echo(f"👉 홈에서 확인: /tournament/{t.id}")
    click.echo(f"👉 관리 대시보드: /admin/tournament/{t.id}")
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

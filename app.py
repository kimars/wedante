import os
import math
import random
import uuid
from datetime import datetime, date, timedelta
from calendar import Calendar

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, abort, session
from flask_login import (LoginManager, login_user, logout_user, login_required,
                          current_user, UserMixin)
from flask_socketio import SocketIO, emit, join_room
from sqlalchemy.orm import joinedload
from sqlalchemy import inspect as sqla_inspect, text
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

from models import db, Organizer, User, Tournament, TournamentParticipant, RefundRequest, RefundMessage, Match, CallLog, CallReply, SmsLog, Banner, QuickIcon, PhoneVerification, CALL_TYPES
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
        ("call_logs", "message", "TEXT"),
        ("quick_icons", "icon_size", "INTEGER DEFAULT 64"),
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


def generate_prelim_groups(tournament: Tournament, player_ids: list[int]):
    """참가자를 조로 나누고(조당 group_size 명), 각 조 내 라운드로빈 경기를 만든다.
    조 우승자의 본선 슬롯은 group_no로부터 결정론적으로 계산되므로 별도 저장 불필요.
    (과거에 seed_group_no/seed_slot을 저장하던 로직은 두 조가 같은 매치에 배정될 때
     서로의 값을 덮어써서 1조 우승자가 본선에 반영되지 않는 버그가 있었음)
    조원이 1명뿐이면 부전승으로 처리해 본선에 즉시 반영한다."""
    players = player_ids[:]
    random.shuffle(players)
    gsize = tournament.group_size or 3
    groups = [players[i:i + gsize] for i in range(0, len(players), gsize)]

    # 본선 빈 대진표를 조 개수만큼 미리 생성 (부전승 슬롯 포함)
    build_main_bracket(tournament.id, len(groups))

    for gi, group in enumerate(groups, start=1):
        if len(group) == 1:
            # 조원 1명 = 자동 부전승. 본선 슬롯에 즉시 배치
            _set_group_winner_slot(tournament.id, gi, group[0])
            continue
        # 조 내 라운드로빈 매치 생성
        for a in range(len(group)):
            for b in range(a + 1, len(group)):
                m = Match(tournament_id=tournament.id, stage="prelim", group_no=gi,
                          player1_id=group[a], player2_id=group[b], status="시작전")
                db.session.add(m)
    db.session.commit()
    return groups


def record_group_winner(tournament: Tournament, group_no: int, winner_id: int):
    """조별 예선이 끝나고 주최자가 우승자를 확정하면, 본선 대진표 해당 슬롯에 자동 반영.
    group_no로부터 첫 라운드 매치 인덱스와 슬롯을 결정론적으로 계산한다."""
    _set_group_winner_slot(tournament.id, group_no, winner_id)
    db.session.commit()
    socketio.emit("bracket_update", {"tournament_id": tournament.id},
                   room=f"tournament_{tournament.id}")


def generate_manual_groups(tournament: Tournament, manual_groups: list[list[int]]):
    """관리자가 조 구성을 직접 지정한 경우의 대진표 생성.
    manual_groups: [[선수id, 선수id, ...], [...], ...]  각 원소가 하나의 조.
    generate_prelim_groups 와 동일한 규칙으로 조 내 라운드로빈 + 본선 슬롯 자동 배치."""
    if not manual_groups:
        return []
    build_main_bracket(tournament.id, len(manual_groups))
    for gi, group in enumerate(manual_groups, start=1):
        if len(group) == 1:
            _set_group_winner_slot(tournament.id, gi, group[0])
            continue
        for a in range(len(group)):
            for b in range(a + 1, len(group)):
                m = Match(tournament_id=tournament.id, stage="prelim", group_no=gi,
                          player1_id=group[a], player2_id=group[b], status="시작전")
                db.session.add(m)
    db.session.commit()
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
        icon = QuickIcon(
            label=f["label"], icon_url=f.get("icon_url") or None,
            icon_size=max(24, min(160, int(f.get("icon_size") or 64))),
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
        icon.label = f["label"]
        icon.icon_url = f.get("icon_url") or None
        icon.icon_size = max(24, min(160, int(f.get("icon_size") or 64)))
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
              .all())
    main = (Match.query
            .filter_by(tournament_id=tid, stage="main")
            .options(joinedload(Match.player1), joinedload(Match.player2))
            .all())

    def m2d(m):
        return {
            "id": m.id, "stage": m.stage, "group_no": m.group_no,
            "round_name": m.round_name, "match_no": m.match_no,
            "player1": m.player1.name if m.player1 else None,
            "player1_id": m.player1_id,
            "player2": m.player2.name if m.player2 else None,
            "player2_id": m.player2_id,
            "score": m.score, "status": m.status,
            "winner_id": m.winner_id,
            "court": m.court, "scheduled_time": m.scheduled_time,
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


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    socketio.run(app, debug=debug_mode, host="0.0.0.0", port=5000)

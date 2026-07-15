import os
import math
import random
import uuid
from datetime import datetime, date
from calendar import Calendar

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, abort
from flask_login import (LoginManager, login_user, logout_user, login_required,
                          current_user, UserMixin)
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

from models import db, Organizer, User, Tournament, TournamentParticipant, RefundRequest, RefundMessage, Match, CallLog, SmsLog, Banner, QuickIcon, CALL_TYPES
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


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


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
    반환값: 1라운드 매치 리스트 (조 우승자가 채워질 슬롯)"""
    size = next_power_of_two(slot_count)
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


def generate_prelim_groups(tournament: Tournament, player_ids: list[int]):
    """참가자를 조로 나누고(조당 group_size 명), 각 조 내 라운드로빈 경기를 만든다.
    동시에 각 조 우승자가 들어갈 본선 슬롯을 미리 배정해 둔다."""
    players = player_ids[:]
    random.shuffle(players)
    gsize = tournament.group_size or 3
    groups = [players[i:i + gsize] for i in range(0, len(players), gsize)]

    # 본선 대진표를 조 개수만큼 미리 생성 (부전승 슬롯 포함)
    first_round = build_main_bracket(tournament.id, len(groups))

    for gi, group in enumerate(groups, start=1):
        # 조 내 라운드로빈 매치 생성
        for a in range(len(group)):
            for b in range(a + 1, len(group)):
                m = Match(tournament_id=tournament.id, stage="prelim", group_no=gi,
                          player1_id=group[a], player2_id=group[b], status="시작전")
                db.session.add(m)
        # 이 조의 우승자가 들어갈 본선 1라운드 슬롯 기록
        slot_match = first_round[(gi - 1) // 2]
        slot_no = ((gi - 1) % 2) + 1
        slot_match.seed_group_no = gi
        slot_match.seed_slot = slot_no
    db.session.commit()
    return groups


def record_group_winner(tournament: Tournament, group_no: int, winner_id: int):
    """조별 예선이 끝나고 주최자가 우승자를 확정하면, 본선 대진표 해당 슬롯에 자동 반영"""
    slot_match = Match.query.filter_by(tournament_id=tournament.id, stage="main",
                                        seed_group_no=group_no).first()
    if slot_match:
        if slot_match.seed_slot == 1:
            slot_match.player1_id = winner_id
        else:
            slot_match.player2_id = winner_id
        db.session.commit()
    socketio.emit("bracket_update", {"tournament_id": tournament.id},
                   room=f"tournament_{tournament.id}")


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
        if User.query.filter_by(phone=f["phone"]).first():
            flash("이미 가입된 전화번호입니다.")
            return redirect(url_for("register"))
        u = User(name=f["name"], phone=f["phone"], region=f.get("region"),
                  club=f.get("club"), refund_bank=f.get("refund_bank"),
                  refund_account=f.get("refund_account"))
        u.set_password(f["password"])
        db.session.add(u)
        db.session.commit()
        login_user(u)
        flash("회원가입이 완료되었습니다.")
        return redirect(url_for("index"))
    return render_template("register.html")


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
    prelim = Match.query.filter_by(tournament_id=tid, stage="prelim").order_by(Match.group_no).all()
    main = Match.query.filter_by(tournament_id=tid, stage="main").all()
    groups = {}
    for m in prelim:
        groups.setdefault(m.group_no, []).append(m)
    my_participation = None
    if current_user.is_authenticated:
        my_participation = TournamentParticipant.query.filter_by(
            tournament_id=tid, user_id=current_user.id).first()
    participant_count = TournamentParticipant.query.filter_by(tournament_id=tid).count()
    return render_template("tournament.html", t=t, groups=groups, main=main,
                            call_types=CALL_TYPES, my_participation=my_participation,
                            participant_count=participant_count)


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
    if not current_user.is_admin:
        abort(403)
    status = request.json.get("status")
    if status not in ("접수중", "접수마감", "진행중", "종료"):
        return jsonify({"error": "잘못된 상태값입니다"}), 400
    t.status = status
    db.session.commit()
    return jsonify({"ok": True, "status": t.status})


@app.route("/admin")
@login_required
def admin_home():
    if not current_user.is_admin:
        abort(403)
    tournaments = Tournament.query.filter_by(organizer_id=current_user.organizer_id).order_by(
        Tournament.created_at.desc()).all()
    organizers = Organizer.query.all()
    return render_template("admin_home.html", tournaments=tournaments, organizers=organizers)


@app.route("/admin/tournaments/new", methods=["GET", "POST"])
@login_required
def new_tournament():
    if not current_user.is_admin:
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
        )
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
    if not current_user.is_admin:
        abort(403)
    participants = TournamentParticipant.query.filter_by(tournament_id=tid).order_by(
        TournamentParticipant.applied_at).all()
    confirmed_ids = {p.user_id for p in participants if p.status == "확정"}
    # 조 편성 대상: 주최자 소속 회원 + 이 대회에 실제로 신청한 회원(소속 무관, 공개가입자 포함)
    org_players = User.query.filter_by(organizer_id=t.organizer_id).all()
    applicant_ids = {p.user_id for p in participants}
    applicant_users = User.query.filter(User.id.in_(applicant_ids)).all() if applicant_ids else []
    players_map = {u.id: u for u in org_players}
    for u in applicant_users:
        players_map[u.id] = u
    players = sorted(players_map.values(), key=lambda u: u.name)
    refund_requests = RefundRequest.query.filter_by(tournament_id=tid).order_by(
        RefundRequest.created_at.desc()).all()
    calls = CallLog.query.filter_by(tournament_id=tid).order_by(CallLog.created_at.desc()).all()
    prelim = Match.query.filter_by(tournament_id=tid, stage="prelim").all()
    groups = sorted(set(m.group_no for m in prelim)) if prelim else []
    return render_template("admin_dashboard.html", t=t, players=players, calls=calls,
                            groups=groups, call_types=CALL_TYPES,
                            participants=participants, confirmed_ids=confirmed_ids,
                            refund_requests=refund_requests)


# ---------------------------------------------------------------------------
# API - 대진표
# ---------------------------------------------------------------------------

@app.route("/api/tournaments/<int:tid>/draw/generate", methods=["POST"])
@login_required
def api_generate_draw(tid):
    t = Tournament.query.get_or_404(tid)
    if not current_user.is_admin:
        abort(403)
    player_ids = request.json.get("player_ids", [])
    if len(player_ids) < 2:
        return jsonify({"error": "참가자가 2명 이상 필요합니다"}), 400
    generate_prelim_groups(t, player_ids)
    socketio.emit("bracket_update", {"tournament_id": tid}, room=f"tournament_{tid}")
    return jsonify({"ok": True})


@app.route("/api/matches/<int:mid>/score", methods=["POST"])
@login_required
def api_submit_score(mid):
    m = Match.query.get_or_404(mid)
    if not current_user.is_admin:
        abort(403)
    data = request.json
    m.score = data.get("score")
    m.winner_id = data.get("winner_id")
    m.status = "종료"
    db.session.commit()

    if m.stage == "main":
        advance_winner(m)

    socketio.emit("bracket_update", {"tournament_id": m.tournament_id},
                   room=f"tournament_{m.tournament_id}")
    return jsonify({"ok": True})


@app.route("/api/tournaments/<int:tid>/groups/<int:gno>/winner", methods=["POST"])
@login_required
def api_group_winner(tid, gno):
    t = Tournament.query.get_or_404(tid)
    if not current_user.is_admin:
        abort(403)
    winner_id = request.json.get("winner_id")
    record_group_winner(t, gno, winner_id)
    return jsonify({"ok": True})


@app.route("/api/tournaments/<int:tid>/draw")
def api_get_draw(tid):
    prelim = Match.query.filter_by(tournament_id=tid, stage="prelim").all()
    main = Match.query.filter_by(tournament_id=tid, stage="main").all()

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
    if not current_user.is_admin:
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
    existing = TournamentParticipant.query.filter_by(tournament_id=tid, user_id=current_user.id).first()
    if existing:
        return jsonify({"error": "이미 신청하셨습니다."}), 400
    status = "확정" if not t.entry_fee else "대기"
    p = TournamentParticipant(tournament_id=tid, user_id=current_user.id, status=status,
                               confirmed_at=datetime.utcnow() if status == "확정" else None)
    db.session.add(p)
    db.session.commit()
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
    return jsonify({"ok": True, "status": p.status})


@app.route("/api/participants/<int:pid>/confirm", methods=["POST"])
@login_required
def api_confirm_participant(pid):
    """관리자가 입금 확인 후 참가를 확정 처리"""
    p = TournamentParticipant.query.get_or_404(pid)
    if not current_user.is_admin:
        abort(403)
    p.status = "확정"
    p.confirmed_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "status": p.status})


@app.route("/api/participants/<int:pid>/cancel", methods=["POST"])
@login_required
def api_cancel_participant(pid):
    """본인 신청 취소 또는 관리자의 강제 취소"""
    p = TournamentParticipant.query.get_or_404(pid)
    if p.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    db.session.delete(p)
    db.session.commit()
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
    requests = (RefundRequest.query.join(Tournament)
                .filter(Tournament.organizer_id == current_user.organizer_id)
                .order_by(RefundRequest.created_at.desc()).all())
    return render_template("admin_refunds.html", requests=requests)


# ---------------------------------------------------------------------------
# API - 콜 기능 (선수 -> 주최자)
# ---------------------------------------------------------------------------

@app.route("/api/tournaments/<int:tid>/call", methods=["POST"])
@login_required
def api_call(tid):
    data = request.json
    call = CallLog(tournament_id=tid, match_id=data.get("match_id"),
                    user_id=current_user.id, call_type=data.get("call_type", "etc"),
                    court=data.get("court"))
    db.session.add(call)
    db.session.commit()

    socketio.emit("new_call", {
        "id": call.id, "player": current_user.name, "court": call.court,
        "type": CALL_TYPES.get(call.call_type, call.call_type),
        "created_at": call.created_at.strftime("%H:%M:%S"),
    }, room=f"tournament_{tid}_admin")
    return jsonify({"ok": True})


@app.route("/api/calls/<int:cid>/resolve", methods=["POST"])
@login_required
def api_resolve_call(cid):
    call = CallLog.query.get_or_404(cid)
    if not current_user.is_admin:
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
    if not current_user.is_admin:
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
# API - 멀티테넌트 (타 주최자 카테고리 생성)
# ---------------------------------------------------------------------------

@app.route("/api/organizers", methods=["POST"])
@login_required
def api_create_organizer():
    if not current_user.is_admin:
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
    join_room(f"tournament_{tid}")
    if data.get("as_admin"):
        join_room(f"tournament_{tid}_admin")


@socketio.on("join_refund")
def on_join_refund(data):
    rid = data.get("refund_id")
    if rid:
        join_room(f"refund_{rid}")


@app.cli.command("init-db")
def init_db():
    """flask --app app init-db  로 실행: 테이블 생성 + 샘플 관리자 계정 생성"""
    db.create_all()
    if not Organizer.query.filter_by(slug="wedante").first():
        org = Organizer(name="위단테", slug="wedante", contact_phone="010-6890-9123")
        db.session.add(org)
        db.session.commit()
        admin = User(name="관리자", phone="01000000000", organizer_id=org.id, is_admin=True)
        admin.set_password("admin1234")
        db.session.add(admin)
        db.session.commit()
        print("DB 초기화 완료. 관리자 로그인: 01000000000 / admin1234")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    socketio.run(app, debug=debug_mode, host="0.0.0.0", port=5000)

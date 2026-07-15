import os
import math
import random
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, abort
from flask_login import (LoginManager, login_user, logout_user, login_required,
                          current_user, UserMixin)
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv

from models import db, Organizer, User, Tournament, Match, CallLog, SmsLog, Banner, CALL_TYPES
from sms import send_sms

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
# 운영 기본값: PostgreSQL. 로컬에 Postgres가 없으면 .env에서 sqlite:///wedante.db 로 바꿔도 됩니다.
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "postgresql://wedante:wedante@localhost:5432/wedante"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

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
    return render_template("index.html", organizers=organizers, tournaments=tournaments, banners=banners)


@app.route("/org/<slug>")
def organizer_page(slug):
    org = Organizer.query.filter_by(slug=slug).first_or_404()
    tournaments = Tournament.query.filter_by(organizer_id=org.id).order_by(
        Tournament.event_date.desc()).all()
    return render_template("index.html", organizers=[org], tournaments=tournaments,
                            active_org=org, banners=[])


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
    return render_template("tournament.html", t=t, groups=groups, main=main,
                            call_types=CALL_TYPES)


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
            status="접수중",
        )
        db.session.add(t)
        db.session.commit()
        flash("대회가 생성되었습니다.")
        return redirect(url_for("admin_dashboard", tid=t.id))
    return render_template("new_tournament.html")


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

        org = Organizer(name=f["name"], slug=f["slug"], contact_phone=f.get("contact_phone"))
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
    players = User.query.filter_by(organizer_id=t.organizer_id).all()
    calls = CallLog.query.filter_by(tournament_id=tid).order_by(CallLog.created_at.desc()).all()
    prelim = Match.query.filter_by(tournament_id=tid, stage="prelim").all()
    groups = sorted(set(m.group_no for m in prelim)) if prelim else []
    return render_template("admin_dashboard.html", t=t, players=players, calls=calls,
                            groups=groups, call_types=CALL_TYPES)


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
        }
    return jsonify({"prelim": [m2d(m) for m in prelim], "main": [m2d(m) for m in main]})


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

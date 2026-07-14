"""데모용 샘플 데이터 생성. 실행: python seed_demo.py"""
from app import app
from models import db, Organizer, User, Tournament

NAMES = ["황지연", "정승빈", "임유정", "박지희", "이사론", "이명재",
         "박명희", "정승은", "조유미", "장하리", "김창현", "이승미"]

with app.app_context():
    db.create_all()
    org = Organizer.query.filter_by(slug="wedante").first()
    if not org:
        org = Organizer(name="위단테", slug="wedante", contact_phone="010-6890-9123")
        db.session.add(org)
        db.session.commit()

    admin = User.query.filter_by(phone="01000000000").first()
    if not admin:
        admin = User(name="관리자", phone="01000000000", organizer_id=org.id, is_admin=True)
        admin.set_password("admin1234")
        db.session.add(admin)

    for i, name in enumerate(NAMES):
        phone = f"010{1000000 + i:07d}"
        if not User.query.filter_by(phone=phone).first():
            u = User(name=name, phone=phone, organizer_id=org.id,
                      region="의정부시", club="위단테TV")
            u.set_password("1234")
            db.session.add(u)

    t = Tournament.query.filter_by(title="2026 제1회 헥사컵 전국 호진테니스대회").first()
    if not t:
        t = Tournament(organizer_id=org.id, title="2026 제1회 헥사컵 전국 호진테니스대회",
                        location="원흥실내테니스코트", event_date="2026-07-15",
                        entry_fee=77000, status="접수중", group_size=3)
        db.session.add(t)

    db.session.commit()
    print(f"완료! 관리자 로그인: 01000000000 / admin1234  (대회 ID={t.id})")

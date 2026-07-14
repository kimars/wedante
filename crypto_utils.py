"""
민감정보(환불계좌 등) 필드 암호화 유틸.
Fernet(대칭키) 방식 - 같은 키로만 복호화 가능.

키 생성 방법:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
생성된 값을 .env 의 ENCRYPTION_KEY 에 넣으세요. (운영 서버마다 동일한 키를 써야 기존 데이터를 복호화할 수 있습니다)
키가 없으면 매 프로세스 시작 시 임시 키를 생성합니다 - 개발용으로만 사용하고,
운영 배포 시에는 반드시 고정된 ENCRYPTION_KEY를 설정하세요 (안 그러면 재시작 후 기존 데이터를 못 읽습니다).
"""
import os
from cryptography.fernet import Fernet, InvalidToken

_key = os.environ.get("ENCRYPTION_KEY")
if not _key:
    _key = Fernet.generate_key().decode()
    print("[경고] ENCRYPTION_KEY가 설정되지 않아 임시 키를 사용합니다. "
          "운영 환경에서는 .env에 고정 키를 설정하세요.")

_fernet = Fernet(_key.encode() if isinstance(_key, str) else _key)


def encrypt_str(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return _fernet.encrypt(value.encode()).decode()


def decrypt_str(token: str | None) -> str | None:
    if token is None or token == "":
        return None
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        # 다른 키로 암호화된(또는 손상된) 데이터
        return "[복호화 불가]"

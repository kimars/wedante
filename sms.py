"""
문자 발송 모듈 - 솔라피(SOLAPI, 구 CoolSMS, https://solapi.com) API 사용.
다른 업체(NHN Toast, 카카오 알림톡 등)로 바꿔도 send_sms() 함수 내부만 교체하면 된다.

사용 전 준비물:
1. https://solapi.com 가입
2. 우측 상단 계정명 → API Key 관리 → API Key/Secret 발급
3. 발신번호 사전등록(본인인증 필요) - 하이픈 없이 '01068909123' 형식
4. .env(로컬) 또는 Railway → Variables 에 아래 값 채우기
   SOLAPI_API_KEY=...
   SOLAPI_API_SECRET=...
   SOLAPI_SENDER=01068909123     # 사전등록한 발신번호
"""
import hashlib
import hmac
import os
import uuid
from datetime import datetime, timezone

import requests

SOLAPI_URL = "https://api.solapi.com/messages/v4/send"


def _auth_header(api_key: str, api_secret: str) -> str:
    """솔라피 HMAC-SHA256 Authorization 헤더 생성.
    date + salt 를 API Secret으로 HMAC-SHA256 해싱한 값을 signature 로 사용한다."""
    date_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    salt = uuid.uuid4().hex
    signature = hmac.new(
        api_secret.encode("utf-8"),
        (date_time + salt).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return (
        f"HMAC-SHA256 apiKey={api_key}, date={date_time}, "
        f"salt={salt}, signature={signature}"
    )


def _normalize_phone(phone: str) -> str:
    """솔라피는 하이픈/공백 없는 숫자만 받는다."""
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def send_sms(receiver_phone: str, message: str) -> dict:
    """단건 문자 발송. 반환값: {"success": bool, "raw": <응답 dict 또는 에러 문자열>}
    본문이 90바이트를 넘으면 솔라피가 자동으로 LMS로 전환해 발송한다."""
    api_key = os.environ.get("SOLAPI_API_KEY")
    api_secret = os.environ.get("SOLAPI_API_SECRET")
    sender = os.environ.get("SOLAPI_SENDER")

    if not (api_key and api_secret and sender):
        # 개발/테스트 환경 - 실제 발송 대신 콘솔에 출력
        print(f"[SMS-DEV] {receiver_phone} <- {message}")
        return {"success": True, "raw": "dev-mode: SOLAPI_* env vars not set, printed to console"}

    receiver = _normalize_phone(receiver_phone)
    sender_norm = _normalize_phone(sender)

    payload = {
        "message": {
            "to": receiver,
            "from": sender_norm,
            "text": message,
            # type 미지정 시 솔라피가 본문 길이에 따라 SMS/LMS 자동 선택
        }
    }
    try:
        resp = requests.post(
            SOLAPI_URL,
            json=payload,
            headers={
                "Authorization": _auth_header(api_key, api_secret),
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"raw_text": resp.text}
        # 솔라피는 접수 성공 시 statusCode == '2000', 그 외 에러 코드는 응답 body 참고
        # https://docs.solapi.com/api-reference/overview/statuscode
        status_code = data.get("statusCode") if isinstance(data, dict) else None
        ok = resp.ok and str(status_code) == "2000"
        return {"success": ok, "raw": data}
    except Exception as e:
        return {"success": False, "raw": str(e)}


def send_bulk(receivers: list[str], message: str) -> list[dict]:
    """여러 명에게 동일 메시지 발송.
    솔라피는 send-many API 로 최대 10,000명 동시 발송을 지원하지만, 여기서는
    개별 결과(SmsLog)를 남기기 위해 반복 호출 방식을 유지한다."""
    return [send_sms(phone, message) for phone in receivers]

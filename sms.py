"""
문자 발송 모듈 - 알리고(Aligo, https://smartsms.aligo.in) API 사용 예시.
다른 업체(NHN Toast, 카카오 알림톡 등)로 바꿔도 send_sms() 함수 내부만 교체하면 된다.

사용 전 준비물 (알리고 기준):
1. https://smartsms.aligo.in 가입 및 발신번호 사전등록(본인인증 필요)
2. 마이페이지에서 API KEY, User ID 발급
3. .env 파일에 아래 값 채우기
   ALIGO_API_KEY=...
   ALIGO_USER_ID=...
   ALIGO_SENDER=0212345678   # 사전등록한 발신번호
"""
import os
import requests

ALIGO_URL = "https://apis.aligo.in/send/"


def send_sms(receiver_phone: str, message: str) -> dict:
    """단건 문자 발송. 반환값: {"success": bool, "raw": <응답 dict 또는 에러 문자열>}"""
    api_key = os.environ.get("ALIGO_API_KEY")
    user_id = os.environ.get("ALIGO_USER_ID")
    sender = os.environ.get("ALIGO_SENDER")

    if not (api_key and user_id and sender):
        # API 키가 설정되지 않은 개발/테스트 환경 - 실제 발송 대신 콘솔에 출력
        print(f"[SMS-DEV] {receiver_phone} <- {message}")
        return {"success": True, "raw": "dev-mode: no ALIGO_* env vars set, printed to console"}

    try:
        resp = requests.post(
            ALIGO_URL,
            data={
                "key": api_key,
                "user_id": user_id,
                "sender": sender,
                "receiver": receiver_phone,
                "msg": message,
                "msg_type": "SMS",
            },
            timeout=10,
        )
        data = resp.json()
        # 알리고는 result_code == '1' 이면 성공
        return {"success": str(data.get("result_code")) == "1", "raw": data}
    except Exception as e:
        return {"success": False, "raw": str(e)}


def send_bulk(receivers: list[str], message: str) -> list[dict]:
    """여러 명에게 동일 메시지 발송 (알리고는 콤마로 최대 1000명까지 동시발송 지원,
    여기서는 결과를 개별 기록하기 위해 반복 호출)"""
    return [send_sms(phone, message) for phone in receivers]

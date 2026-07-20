"""
Google Gemini API 호출 모듈. 상대선수 분석 코멘트 생성에 사용.

사용 전 준비:
1. https://aistudio.google.com/apikey 에서 API 키 발급
2. Railway → Variables 에 아래 두 개 설정
   GEMINI_API_KEY=AIza... 로 시작하는 키
   GEMINI_MODEL=gemini-3.5-flash   (선택, 기본값)

API 문서: https://ai.google.dev/api/generate-content
"""
import os
import requests

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-3.5-flash"


def _make_url(model: str) -> str:
    return f"{GEMINI_API_BASE}/{model}:generateContent"


def generate_text(prompt: str,
                   system_instruction: str | None = None,
                   temperature: float = 0.7,
                   max_output_tokens: int = 500,
                   timeout: int = 15) -> dict:
    """Gemini API 호출로 텍스트 생성.

    반환값: {"success": bool, "text": str | None, "raw": <응답 dict 또는 에러 문자열>}
    개발 환경(API 키 미설정)에서는 미리 만든 fallback 텍스트를 반환한다."""
    api_key = os.environ.get("GEMINI_API_KEY")
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)

    if not api_key:
        # 개발 모드 - API 키 없어도 동작하게 하되 실제 AI 응답은 아님
        return {
            "success": False,
            "text": None,
            "raw": "dev-mode: GEMINI_API_KEY env var not set. AI 분석을 사용하려면 Railway Variables에 GEMINI_API_KEY를 등록하세요.",
        }

    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": float(temperature),
            "maxOutputTokens": int(max_output_tokens),
        },
    }
    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    try:
        resp = requests.post(
            _make_url(model),
            json=body,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            timeout=timeout,
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"raw_text": resp.text}

        if not resp.ok:
            return {"success": False, "text": None, "raw": data}

        # 응답에서 텍스트 추출
        # 정상 응답 예:
        # { "candidates": [{"content": {"parts": [{"text": "..."}]}}] }
        try:
            text_parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in text_parts).strip()
            if not text:
                return {"success": False, "text": None, "raw": data}
            return {"success": True, "text": text, "raw": data}
        except (KeyError, IndexError, TypeError):
            # 안전 필터에 걸렸거나 형식이 다른 경우
            block_reason = None
            try:
                block_reason = data.get("promptFeedback", {}).get("blockReason")
            except Exception:
                pass
            return {
                "success": False,
                "text": None,
                "raw": {"blockReason": block_reason, "response": data} if block_reason else data,
            }
    except Exception as e:
        return {"success": False, "text": None, "raw": f"network error: {e}"}


def build_opponent_analysis_prompt(me: dict, opponent: dict) -> str:
    """상대 선수 분석용 프롬프트 생성.
    me / opponent 는 각각 {name, handedness, strengths, weaknesses, region, club} 형식."""
    def _fmt(u):
        parts = []
        if u.get("handedness"):
            parts.append(f"{'오른손잡이' if u['handedness']=='right' else '왼손잡이'}")
        if u.get("region"):
            parts.append(f"지역: {u['region']}")
        if u.get("club"):
            parts.append(f"소속: {u['club']}")
        info = " · ".join(parts) if parts else "(추가 정보 없음)"
        strengths = u.get("strengths") or "(등록된 강점 없음)"
        weaknesses = u.get("weaknesses") or "(등록된 약점 없음)"
        return f"기본 정보: {info}\n강점: {strengths}\n약점: {weaknesses}"

    return f"""당신은 아마추어 테니스 코치입니다. 아래 두 선수의 정보를 바탕으로,
"내" 선수가 "상대" 선수를 이기기 위한 실전 전략을 3~5문장으로 조언해주세요.

[내 선수: {me.get('name', '?')}]
{_fmt(me)}

[상대 선수: {opponent.get('name', '?')}]
{_fmt(opponent)}

작성 규칙:
- 반드시 "{me.get('name', '?')}님. {opponent.get('name', '?')} 선수를 분석하셨네요?" 라는 문장으로 시작하세요.
- 상대의 강점은 인정하고, 그 강점을 어떻게 피하거나 무력화할지 구체적으로 안내하세요.
- 상대의 약점을 어떻게 공략할지 실전 조언을 하나 이상 넣으세요.
- 친근하고 격려하는 한국어 문체(존댓말)로 3~5문장 안에 마무리하세요.
- 확실하지 않은 정보를 지어내지 말고, 등록된 정보만으로 조언하세요. 정보가 부족하면 "정보가 부족해 일반적인 조언만 드릴 수 있습니다" 라고 밝히세요.
- 답변에 마크다운이나 별표를 넣지 마세요. 자연스러운 평문으로만 작성하세요."""


def analyze_opponent(me: dict, opponent: dict) -> dict:
    """상대 분석 결과 반환. {"success": bool, "text": str, "raw": ...}
    실패 시 fallback 텍스트도 함께 제공."""
    prompt = build_opponent_analysis_prompt(me, opponent)
    result = generate_text(
        prompt,
        system_instruction="당신은 아마추어 테니스 대회를 위한 실전 코치이며, 짧고 실용적인 한국어 조언을 제공합니다.",
        temperature=0.7,
        max_output_tokens=600,
    )
    if not result["success"]:
        # AI 실패 시 최소한의 규칙 기반 분석 반환
        result["text"] = _fallback_analysis(me, opponent)
    return result


def _fallback_analysis(me: dict, opponent: dict) -> str:
    """AI 실패 시 규칙 기반 fallback 분석 텍스트."""
    my_name = me.get("name", "선수")
    op_name = opponent.get("name", "상대")
    op_hand = opponent.get("handedness")
    op_str = opponent.get("strengths")
    op_weak = opponent.get("weaknesses")

    lines = [f"{my_name}님. {op_name} 선수를 분석하셨네요?"]
    if op_hand or op_str:
        hand_txt = "오른손잡이" if op_hand == "right" else ("왼손잡이" if op_hand == "left" else "")
        s = f"{op_name} 선수는 "
        if hand_txt: s += f"{hand_txt}이며 "
        if op_str: s += f"'{op_str}'가 강점입니다."
        elif hand_txt: s = s.rstrip("이며 ") + "입니다."
        lines.append(s)
    if op_weak:
        lines.append(f"{op_name} 선수의 약점 '{op_weak}'를 집중적으로 공략하시면 좋은 결과가 있을 겁니다.")
    else:
        lines.append(f"{op_name} 선수의 약점 정보가 등록되지 않아 일반적인 전략만 드릴 수 있습니다. 상대의 백핸드와 체력을 살피며 경기를 운영해보세요.")
    lines.append("좋은 경기 하시길 응원합니다!")
    return " ".join(lines)

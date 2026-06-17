"""공통 LLM 클라이언트 모듈.

기존 에이전트 내부 LLM 코드는 그대로 유지합니다.
신규 코드(Planner 등)는 이 모듈을 사용합니다.
"""
from openai import OpenAI
from server.config.settings import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_MAX_TOKENS


def get_client() -> OpenAI:
    """설정된 LLM 클라이언트 반환 (OpenAI-compatible)."""
    return OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


def chat(messages: list[dict], model: str = LLM_MODEL,
         temperature: float = 0, max_tokens: int = LLM_MAX_TOKENS) -> str:
    """단순 chat completion 호출 → 응답 텍스트 반환."""
    client = get_client()
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content.strip()

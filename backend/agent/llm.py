import json,httpx
import httpx
from backend.config import OLLAMA_BASE_URL, LLM_TIMEOUT
import json
import re

class OllamaClient:
    def __init__(self,model,base_url=OLLAMA_BASE_URL,timeout=LLM_TIMEOUT):
        self.model=model
        self.base_url=base_url.rstrip('/')
        self.timeout=timeout

    async def chat(
            self,
            system: str,
            user: str
    ) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "options": {
                "temperature": 0,
                "num_predict": 512,
            },
            "messages": [
                {
                    "role": "system",
                    "content": system
                },
                {
                    "role": "user",
                    "content": user
                }
            ]
        }

        async with httpx.AsyncClient(
                timeout=self.timeout
        ) as client:
            response = await client.post(
                f"{self.base_url}/api/chat",
                json=payload
            )

            response.raise_for_status()

            data = response.json()

            print("\nOLLAMA RESPONSE:")
            print(data)

            content = (
                data
                .get("message", {})
                .get("content", "")
            )

            if not content.strip():
                raise RuntimeError(
                    f"LLM returned empty content. "
                    f"Full response: {data}"
                )

            return content



# Models routinely wrap code in a fence and then keep talking after the closing
# one. Only the first fenced block is the artifact; anything outside it is prose
# that would land in the .sv/.py file and break the tools.
_FENCE = re.compile(r"```[^\n]*\n(.*?)(?:\n```|\Z)", re.DOTALL)


def strip_fences(text: str) -> str:
    if not text:
        return ""

    text = text.strip()
    match = _FENCE.search(text)

    if match:
        return match.group(1).strip()

    return text


def parse_json(text: str):
    if not text:
        raise ValueError(
            "LLM returned an empty response. Cannot parse JSON."
        )

    cleaned = strip_fences(text)

    print("\n========== RAW LLM RESPONSE ==========")
    print(repr(text))
    print("========== CLEANED RESPONSE ==========")
    print(repr(cleaned))
    print("======================================\n")

    # Try direct JSON parsing
    try:
        return json.loads(cleaned)

    except json.JSONDecodeError:
        pass

    # Try extracting JSON object
    match = re.search(
        r"\{.*\}",
        cleaned,
        re.DOTALL
    )

    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(
        f"LLM returned invalid JSON:\n{cleaned}"
    )

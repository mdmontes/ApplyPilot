"""
Unified LLM client for ApplyPilot.

Strictly supports Google Gemini (default: gemini-2.5-flash).
Requires GEMINI_API_KEY in environment or .env.

LLM_MODEL env var overrides the model name.
"""

import logging
import os
import time

import httpx
from applypilot import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_provider() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) for Gemini.

    Reads env at call time so that load_env() called in _bootstrap() is visible.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    model_override = os.environ.get("LLM_MODEL", "")

    if not gemini_key:
        raise RuntimeError(
            "GEMINI_API_KEY not found. This project strictly requires a Gemini API key. "
            "Get one for free at https://aistudio.google.com and run 'applypilot init'."
        )

    return (
        "https://generativelanguage.googleapis.com/v1beta/openai",
        model_override or config.DEFAULTS["model_gemini"],
        gemini_key,
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 10


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.
    """

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)
        self.total_cost: float = 0.0

    # -- Native Gemini API --------------------------------------------------

    def _update_cost(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Update cumulative session cost based on Gemini 2.0 Flash pricing.
        
        Pricing: $0.10 / 1M input tokens, $0.40 / 1M output tokens.
        """
        cost = (prompt_tokens * 0.0000001) + (completion_tokens * 0.0000004)
        self.total_cost += cost

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        model_override: str | None = None,
    ) -> str:
        """Call the native Gemini generateContent API."""
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        model_to_use = model_override or self.model
        url = f"{_GEMINI_NATIVE_BASE}/models/{model_to_use}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        
        # Track usage
        usage = data.get("usageMetadata", {})
        self._update_cost(
            usage.get("promptTokenCount", 0),
            usage.get("candidatesTokenCount", 0)
        )
        
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # -- Gemini OpenAI-Compatible API ----------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        model_override: str | None = None,
    ) -> str:
        """Call the Gemini OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        model_to_use = model_override or self.model
        payload = {
            "model": model_to_use,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        if resp.status_code in (403, 404) and self._is_gemini:
            raise _GeminiCompatForbidden(resp)

        resp.raise_for_status()
        data = resp.json()
        
        # Track usage
        usage = data.get("usage", {})
        self._update_cost(
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0)
        )
        
        return data["choices"][0]["message"]["content"]

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
        model: str | None = None,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        model_to_use = model or self.model

        # Qwen3 optimization: prepend /no_think to skip chain-of-thought
        if "qwen" in model_to_use.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

        for attempt in range(_MAX_RETRIES):
            try:
                if self._use_native_gemini:
                    return self._chat_native_gemini(messages, temperature, max_tokens, model_override=model)

                return self._chat_compat(messages, temperature, max_tokens, model_override=model)

            except _GeminiCompatForbidden as exc:
                log.warning(
                    "Gemini compat endpoint returned %s for model '%s'. "
                    "Switching to native generateContent API.",
                    exc.response.status_code,
                    model_to_use,
                )
                self._use_native_gemini = True
                try:
                    return self._chat_native_gemini(messages, temperature, max_tokens, model_override=model)
                except httpx.HTTPStatusError as native_exc:
                    raise RuntimeError(
                        f"Both Gemini endpoints failed. Compat: 403 Forbidden. "
                        f"Native: {native_exc.response.status_code}"
                    ) from native_exc

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    retry_after = resp.headers.get("Retry-After") or resp.headers.get("X-RateLimit-Reset-Requests")
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning("LLM request timed out, retrying in %ds", wait)
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compatible endpoint returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None


def get_client() -> LLMClient:
    """Return (or create) the module-level LLMClient singleton."""
    global _instance
    if _instance is None:
        base_url, model, api_key = _detect_provider()
        log.info("LLM provider: %s  model: %s", base_url, model)
        _instance = LLMClient(base_url, model, api_key)
    return _instance

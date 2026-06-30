"""The Ollama HTTP client used to turn the findings context into a report."""

from __future__ import annotations

import httpx

from ..logging_config import safe


class OllamaClient:
    """Thin wrapper around Ollama's ``/api/chat`` endpoint.

    Injectable ``client`` parameter for testing (matches ``GotifyNotifier``
    pattern). HTTP errors propagate to the caller; ``run()`` handles them.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 300.0,
        thinking: bool = False,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._thinking = thinking
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout))

    def generate(self, system_prompt: str, user_message: str) -> str:
        """POST to ``/api/chat`` and return the generated text.

        Raises ``httpx.HTTPError`` on network / HTTP failures.
        Raises ``ValueError`` if the response shape is unexpected.
        """
        url = f"{self._base_url}/api/chat"
        payload: dict = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        if self._thinking:
            # qwen3-family extended thinking: the model reasons internally before
            # generating the response. Ignored by non-qwen3 models.
            payload["think"] = True
        response = self._client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        try:
            return data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"unexpected Ollama response shape — missing message.content: {safe(str(data)[:200])}"
            ) from exc

    def close(self) -> None:
        self._client.close()

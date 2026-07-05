"""The Ollama HTTP client used to turn the findings context into a report."""

from __future__ import annotations

import logging
import time

import httpx

from ..logging_config import safe

log = logging.getLogger("healthlog.narrate")

# One retry for transport-level blips (connect refused/reset, DNS hiccup).
# HTTP status errors are not retried: a 4xx/5xx from a reachable Ollama is a
# real answer, and generation is too expensive to repeat on guesswork.
_TRANSPORT_RETRIES = 1
_RETRY_DELAY_S = 2.0


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
        for attempt in range(_TRANSPORT_RETRIES + 1):
            try:
                response = self._client.post(url, json=payload)
                break
            except httpx.TransportError as exc:
                if attempt == _TRANSPORT_RETRIES:
                    raise
                log.warning("ollama transport error (%s); retrying in %.0fs", safe(str(exc)), _RETRY_DELAY_S)
                time.sleep(_RETRY_DELAY_S)
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

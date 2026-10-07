"""
Embedding client: one protocol, OpenAI-compatible ``POST {base_url}{path}``.

Every ``EmbeddingProvider`` preset speaks the same request shape
(``{"model": ..., "input": [...]}`` → ``data[].embedding``); the preset only
carries the base URL, the path (Z.AI mounts it under ``/api/paas/v4/embeddings``),
the batch limit and a *query hint* quirk — Voyage wants ``input_type``
``document``/``query`` and Jina a ``task`` of ``retrieval.passage`` /
``retrieval.query`` so that documents and queries land in the right space.

Texts are capped at ``MAX_TEXT_CHARS`` (truncated and counted, never rejected —
a long page still indexes), batched by the provider's ``max_batch``, and 429 /
5xx responses are retried with a short backoff. Usage is taken from the
provider's ``usage.total_tokens`` when present and estimated at ``len // 4``
otherwise (Ollama returns no counts on some versions).
"""

import logging
import time
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 8_000
TIMEOUT_SECONDS = 60
RETRY_BACKOFF = (0.5, 1.0, 2.0)
TEST_SENTENCE = "PyRun embedding provider test."


class EmbeddingError(Exception):
    """A provider call failed; the message is safe to show in the console."""


@dataclass
class EmbeddingResult:
    vectors: list = field(default_factory=list)
    tokens: int = 0
    truncated: int = 0
    calls: int = 0

    @property
    def dimension(self) -> int:
        return len(self.vectors[0]) if self.vectors else 0


def estimate_tokens(texts) -> int:
    return sum(max(1, len(t) // 4) for t in texts)


class EmbeddingClient:
    """Embeds through one ``EmbeddingProvider`` row (credential decrypted per call)."""

    def __init__(self, provider, *, session=None):
        self.provider = provider
        self.preset = provider.preset
        self._session = session or requests.Session()

    # ------------------------------------------------------------------ public

    def embed(self, texts, *, input_type: str = "document") -> EmbeddingResult:
        """Embed ``texts`` (documents by default; ``input_type="query"`` for queries)."""
        if isinstance(texts, str):
            raise EmbeddingError("embed() takes a list of strings")
        prepared, truncated = [], 0
        for text in texts:
            if not isinstance(text, str):
                raise EmbeddingError("every item to embed must be a string")
            if len(text) > MAX_TEXT_CHARS:
                truncated += 1
                text = text[:MAX_TEXT_CHARS]
            prepared.append(text if text.strip() else " ")
        result = EmbeddingResult(truncated=truncated)
        if not prepared:
            return result

        batch_size = max(1, int(self.provider.max_batch or 1))
        for start in range(0, len(prepared), batch_size):
            batch = prepared[start : start + batch_size]
            vectors, tokens = self._embed_batch(batch, input_type)
            result.vectors.extend(vectors)
            result.tokens += tokens
            result.calls += 1
        return result

    def embed_query(self, text: str) -> list:
        return self.embed([text], input_type="query").vectors[0]

    def test(self) -> EmbeddingResult:
        """Embed one sentence; the caller records ``result.dimension``."""
        result = self.embed([TEST_SENTENCE])
        if result.dimension == 0:
            raise EmbeddingError("provider returned no embedding for the test sentence")
        return result

    # ------------------------------------------------------------------ internals

    def _request_body(self, batch, input_type):
        body = {"model": self.provider.model, "input": batch}
        hint = self.preset.get("query_hint")
        if hint:
            value = hint.get("query" if input_type == "query" else "document")
            if value:
                body[hint["param"]] = value
        return body

    def _headers(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        credential = self.provider.get_credential()
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        return headers

    def _embed_batch(self, batch, input_type):
        url = self.provider.endpoint_url
        body = self._request_body(batch, input_type)
        headers = self._headers()
        last_error = None
        for attempt in range(len(RETRY_BACKOFF) + 1):
            try:
                response = self._session.post(
                    url, json=body, headers=headers, timeout=TIMEOUT_SECONDS
                )
            except requests.RequestException as e:
                last_error = EmbeddingError(f"could not reach {url}: {e}")
                if attempt < len(RETRY_BACKOFF):
                    time.sleep(RETRY_BACKOFF[attempt])
                    continue
                raise last_error from e

            if response.status_code == 429 or response.status_code >= 500:
                last_error = EmbeddingError(
                    f"provider answered HTTP {response.status_code}: "
                    f"{_error_message(response) or response.reason}"
                )
                if attempt < len(RETRY_BACKOFF):
                    time.sleep(RETRY_BACKOFF[attempt])
                    continue
                raise last_error
            if response.status_code in (401, 403):
                raise EmbeddingError("authentication failed — check the credential")
            if response.status_code == 404:
                raise EmbeddingError(
                    f"endpoint not found at {url} — check the base URL (and the model name)"
                )
            if response.status_code >= 400:
                raise EmbeddingError(
                    f"provider rejected the request (HTTP {response.status_code}): "
                    f"{_error_message(response) or response.reason}"
                )
            return self._parse(response, batch)
        raise last_error  # pragma: no cover — every path above returns or raises

    @staticmethod
    def _parse(response, batch):
        expected = len(batch)
        try:
            data = response.json()
        except ValueError as e:
            raise EmbeddingError("provider returned a non-JSON response") from e
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) != expected:
            got = len(items) if isinstance(items, list) else "no"
            raise EmbeddingError(
                f"provider returned {got} embeddings for {expected} inputs"
            )
        try:
            items = sorted(items, key=lambda it: int(it.get("index", 0)))
            vectors = [[float(x) for x in it["embedding"]] for it in items]
        except (KeyError, TypeError, ValueError) as e:
            raise EmbeddingError(f"provider response had no numeric 'embedding': {e}") from e
        if any(len(v) != len(vectors[0]) for v in vectors):
            raise EmbeddingError("provider returned embeddings of different sizes")
        if not vectors[0]:
            raise EmbeddingError("provider returned empty embeddings")

        # Usage: the provider's count when it gives one, else the len // 4 estimate
        # (Ollama's OpenAI route omits token counts on some versions).
        usage = data.get("usage") if isinstance(data, dict) else None
        tokens = 0
        if isinstance(usage, dict):
            tokens = usage.get("total_tokens") or usage.get("prompt_tokens") or 0
        try:
            tokens = int(tokens)
        except (TypeError, ValueError):
            tokens = 0
        if tokens <= 0:
            tokens = estimate_tokens(batch)
        return vectors, tokens


def _error_message(response) -> str:
    try:
        data = response.json()
    except ValueError:
        return (response.text or "")[:200]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or "")[:200]
        if isinstance(err, str):
            return err[:200]
        if "message" in data:
            return str(data["message"])[:200]
    return ""

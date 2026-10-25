"""Embedding providers for semantic memory and search by meaning.

* `OpenRouterEmbeddings` — real semantic embeddings behind the OpenRouter key (default `baai/bge-m3`, multilingual).
* `VoyageEmbeddings` — real semantic embeddings (multilingual), needs VOYAGE_API_KEY.
* `HashingEmbeddings` — deterministic local fallback (feature hashing of word/char n-grams). It is
  lexical, not semantic (`semantic = False`): search then stays purely lexical and nothing is sent anywhere.
* `FakeEmbeddings` — deterministic test double that puts synonyms close («стрижка» ↔ «барбер»).

Every vector is stored with the `model` that produced it; vectors of different models are never compared.
Vector similarity is only ever used for *retrieval*, never for authorization or critical data.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import math
import re
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from jarvis.core.config import Settings
from jarvis.core.errors import PermanentError, TransientError
from jarvis.core.http import ResilientHttp
from jarvis.db.models import EMBEDDING_DIMS

log = logging.getLogger(__name__)

_WORD = re.compile(r"[\w-]+", re.UNICODE)
OPENROUTER_URL = "https://openrouter.ai/api/v1/"
BudgetHook = Callable[[], Awaitable[None]]


class EmbeddingProvider(Protocol):
    model: str
    dims: int
    semantic: bool  # False: the vectors only reflect shared words (hashing) → not used for search by meaning

    async def embed(self, texts: list[str], *, kind: str) -> list[list[float]]: ...


PROVIDERS = ("auto", "openrouter", "voyage", "hashing")


def embeddings_provider_name(settings: Settings) -> str:
    """Which provider EMBEDDINGS_PROVIDER resolves to: auto = Voyage if its key, else OpenRouter if its key, else
    hashing. An explicit provider without its key also ends at hashing (search by words)."""
    choice = settings.embeddings_provider.strip().lower()
    if choice not in PROVIDERS:
        raise ValueError("EMBEDDINGS_PROVIDER must be auto, openrouter, voyage or hashing")

    def has(secret: Any) -> bool:
        return bool(secret is not None and secret.get_secret_value())

    if choice in ("auto", "voyage") and has(settings.voyage_api_key):
        return "voyage"
    if choice in ("auto", "openrouter") and has(settings.openrouter_api_key):
        return "openrouter"
    return "hashing"


def _batches(texts: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(texts), max(1, size)):
        yield texts[start : start + max(1, size)]


def _prefixed(model: str, texts: list[str], kind: str) -> list[str]:
    """E5 models are trained with «query: » / «passage: » prefixes; others (bge-m3, voyage) take plain text."""
    name = model.lower()
    if "multilingual-e5" in name or name.startswith("intfloat/e5"):
        prefix = "query: " if kind == "query" else "passage: "
        return [prefix + t for t in texts]
    return texts


def _vectors(provider: str, data: dict[str, Any], expected: int, dims: int) -> list[list[float]]:
    """Parse an OpenAI-shaped embeddings response: `data[i].embedding`, ordered by `data[i].index`."""
    items = data.get("data")
    if not isinstance(items, list):
        raise PermanentError(f"{provider}: unexpected response shape")
    items = sorted(items, key=lambda d: d.get("index", 0) if isinstance(d, dict) else 0)
    if len(items) != expected:
        raise PermanentError(f"{provider}: embedding count mismatch ({len(items)} for {expected})")
    vectors: list[list[float]] = []
    for item in items:
        raw = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(raw, list):
            raise PermanentError(f"{provider}: embedding is not a float list")
        vector = [float(v) for v in raw]
        if len(vector) != dims:
            raise PermanentError(f"{provider}: model returns {len(vector)}-d vectors, the index needs {dims}")
        if not all(math.isfinite(v) for v in vector):
            raise PermanentError(f"{provider}: non-finite embedding value")
        vectors.append(vector)
    return vectors


class HashingEmbeddings:
    dims = EMBEDDING_DIMS
    semantic = False

    def __init__(self) -> None:
        self.model = f"hashing-v1-{self.dims}"

    def _features(self, text: str) -> list[str]:
        words = [w.lower() for w in _WORD.findall(text)]
        feats = list(words)
        feats += [f"{a}_{b}" for a, b in itertools.pairwise(words)]
        for w in words:
            padded = f"#{w}#"
            feats += [padded[i : i + 4] for i in range(max(1, len(padded) - 3))]
        return feats

    async def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * self.dims
            for feat in self._features(text):
                digest = hashlib.blake2b(feat.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self.dims
                sign = 1.0 if digest[4] & 1 else -1.0
                vec[index] += sign
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


class OpenRouterEmbeddings:
    """`POST /api/v1/embeddings` (OpenAI-compatible) with the user's OpenRouter key.

    Several texts go in one call (`input` is a list); every call first consumes the daily budget. Retries for
    transient failures come from `ResilientHttp` (embedding has no side effects, so POST retries are safe).
    Texts are never logged.
    """

    semantic = True
    dims = EMBEDDING_DIMS

    def __init__(
        self,
        api_key: str,
        model: str = "baai/bge-m3",
        *,
        batch_size: int = 64,
        budget: BudgetHook | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.batch_size = batch_size
        self._budget = budget
        self._http = ResilientHttp(
            "openrouter-embeddings",
            base_url=OPENROUTER_URL,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "JARVIS"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in _batches(_prefixed(self.model, texts, kind), self.batch_size):
            vectors += await self._embed_batch(batch)
        return vectors

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        if any(not t.strip() for t in batch):
            raise PermanentError("openrouter-embeddings: empty text")
        if self._budget is not None:
            await self._budget()
        body = {"model": self.model, "input": batch, "encoding_format": "float"}
        data = (await self._http.post("embeddings", json=body, retry_non_idempotent=True)).json()
        if not isinstance(data, dict):
            raise PermanentError("openrouter-embeddings: unexpected response shape")
        if "data" not in data and isinstance(data.get("error"), dict):
            # An upstream failure can arrive as a 200 with an error object.
            code = data["error"].get("code")
            message = f"openrouter-embeddings: upstream error {code}"
            if code in (408, 429, 500, 502, 503, 504, 529):
                raise TransientError(message)
            raise PermanentError(message)
        vectors = _vectors("openrouter-embeddings", data, len(batch), self.dims)
        usage = data.get("usage") or {}
        log.debug("embeddings: %d texts, %s tokens, cost %s", len(batch), usage.get("total_tokens"), usage.get("cost"))
        return vectors

    async def probe(self) -> str:
        """The model is listed as an OpenRouter embedding model (free listing call, no embedding, no cost)."""
        listing = (await self._http.get("embeddings/models")).json().get("data") or []
        if self.model not in {m.get("id") for m in listing if isinstance(m, dict)}:
            raise PermanentError(f"embedding model {self.model!r} is not offered by OpenRouter")
        return f"openrouter: {self.model} available (no billable call)"


class VoyageEmbeddings:
    semantic = True
    dims = EMBEDDING_DIMS

    def __init__(
        self,
        api_key: str,
        model: str = "voyage-4-lite",
        *,
        batch_size: int = 64,
        budget: BudgetHook | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.batch_size = batch_size
        self._budget = budget
        self._http = ResilientHttp(
            "voyage", timeout=30.0, headers={"Authorization": f"Bearer {api_key}"}, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in _batches(texts, self.batch_size):
            if self._budget is not None:
                await self._budget()
            body = {
                "input": batch,
                "model": self.model,
                "input_type": "query" if kind == "query" else "document",
                "output_dimension": self.dims,
            }
            data = (
                await self._http.post("https://api.voyageai.com/v1/embeddings", json=body, retry_non_idempotent=True)
            ).json()
            vectors += _vectors("voyage", data, len(batch), self.dims)
        return vectors


# Words (by stem) that the fake model treats as one meaning. Only for tests.
FAKE_SYNONYMS: tuple[tuple[str, ...], ...] = (
    ("стриж", "постри", "барбер", "парикмах"),
    ("машин", "автомоб", "автосервис", "шиномонтаж"),
    ("врач", "доктор", "терапевт", "клиник"),
    ("продукт", "молок", "хлеб", "кефир"),
    ("пароль", "код", "pin"),
)


@dataclass
class FakeEmbeddings:
    """Deterministic «semantic» model for tests: a synonym group is one dimension, other words are hashed.

    «стрижка» and «Записаться к барберу» share the barber dimension, so they are close although they share no word.
    """

    model: str = "fake-semantic-v1"
    dims: int = EMBEDDING_DIMS
    semantic: bool = True
    calls: list[tuple[str, list[str]]] = field(default_factory=list)
    fail_with: Exception | None = None

    def _dimension(self, word: str) -> int | None:
        for index, stems in enumerate(FAKE_SYNONYMS):
            if any(word.startswith(stem) for stem in stems):
                return index
        if len(word) < 3:
            return None  # prepositions and the like carry no meaning here
        digest = hashlib.blake2b(word[:5].encode(), digest_size=4).digest()
        return len(FAKE_SYNONYMS) + int.from_bytes(digest, "big") % (self.dims - len(FAKE_SYNONYMS))

    async def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
        self.calls.append((kind, list(texts)))
        if self.fail_with is not None:
            raise self.fail_with
        vectors = []
        for text in texts:
            vec = [0.0] * self.dims
            for word in _WORD.findall(text.lower()):
                if (dim := self._dimension(word)) is not None:
                    vec[dim] += 1.0
            if not any(vec):
                vec[-1] = 1.0  # never a zero vector (cosine distance would be undefined)
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors

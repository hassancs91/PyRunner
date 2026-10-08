"""
VectorService — the single implementation behind every door.

The console, the loopback helper (``pyrunner_vectors``), the plugin SDK
(``VectorAPI``) and the in-process consumers (Brain, agents) all resolve a
``VectorCollection``, obtain a ``CollectionHandle`` and call the same methods.
The handle owns the contract: one filter grammar, scores normalized so higher is
better, usage counted on the collection and its provider, the pinned embedding
model enforced.

Backends: the Local store (``local_store``) and Qdrant (``qdrant_store``), picked
per collection from its connection. ``IMPLEMENTED_BACKENDS`` is what the console
offers when creating a collection.

Errors are ``VectorError`` with a stable ``code`` so the doors can map them
(``not_found`` → 404, ``unsupported_filter`` / ``invalid`` → 400,
``collection_full`` → 413, ``embedding_failed`` → 502,
``backend_unavailable`` → 503, …).
"""

import logging

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from core.models import EmbeddingProvider, VectorCollection, VectorConnection
from core.models.vector_collection import NAME_RULE, is_valid_name
from core.netguard import is_safe_endpoint_url
from core.vectors import filters
from core.vectors.errors import StoreError, VectorError

logger = logging.getLogger(__name__)

IMPLEMENTED_BACKENDS = {VectorConnection.ProviderType.LOCAL, VectorConnection.ProviderType.QDRANT}
MAX_DIMENSION = 8192
MAX_SEARCH_K = 1000


def vectors_dir_display() -> str:
    """The store directory as shown in the console: data-relative, never an absolute
    server path (which would leak the host layout into a page and its JSON)."""
    from pathlib import Path

    vectors_dir = Path(settings.VECTORS_DIR)
    try:
        return vectors_dir.relative_to(settings.BASE_DIR).as_posix() + "/"
    except ValueError:
        return vectors_dir.name + "/"


def _translate(exc) -> VectorError:
    """Map backend / client exceptions onto VectorError codes."""
    from core.vectors.embeddings import EmbeddingError

    if isinstance(exc, VectorError):
        return exc
    if isinstance(exc, filters.UnsupportedFilter):
        return VectorError(str(exc), "unsupported_filter")
    if isinstance(exc, StoreError):
        return VectorError(str(exc), exc.code)
    if isinstance(exc, EmbeddingError):
        return VectorError(str(exc), "embedding_failed")
    return VectorError(str(exc) or exc.__class__.__name__, "error")


class VectorService:
    """Create / resolve / delete collections and test the Services-page rows."""

    # ------------------------------------------------------------- availability

    @staticmethod
    def supported_connections():
        """Enabled connections whose backend is implemented (what the console offers)."""
        return VectorConnection.objects.filter(
            enabled=True, provider_type__in=[str(t) for t in IMPLEMENTED_BACKENDS]
        )

    @classmethod
    def is_available(cls) -> bool:
        return cls.supported_connections().exists()

    @staticmethod
    def default_connection():
        connection = VectorConnection.get_default()
        if connection is not None and connection.provider_type in IMPLEMENTED_BACKENDS:
            return connection
        return VectorService.supported_connections().first()

    # ------------------------------------------------------------- collections

    @classmethod
    def create_collection(
        cls,
        *,
        workspace,
        name: str,
        connection=None,
        embedding_provider=None,
        dimension=None,
        metric: str = VectorCollection.Metric.COSINE,
        description: str = "",
        owner_plugin=None,
        owner_key=None,
        created_by=None,
        include_in_backup: bool = True,
    ) -> VectorCollection:
        name = (name or "").strip()
        if not is_valid_name(name, owner_plugin):
            if owner_plugin and ":" in name:
                raise VectorError(
                    f"A plugin collection is named '{owner_plugin}:<key>' with a key of "
                    f"{NAME_RULE}; the whole name must fit 64 characters (got {name!r}).",
                    "invalid",
                )
            raise VectorError(f"Collection name must be {NAME_RULE}.", "invalid")
        if VectorCollection.objects.filter(workspace=workspace, name__iexact=name).exists():
            raise VectorError(
                f"A collection named '{name}' already exists in this workspace.", "invalid"
            )
        if metric not in VectorCollection.Metric.values:
            raise VectorError(f"Unknown metric '{metric}'.", "invalid")

        connection = connection or cls.default_connection()
        if connection is None:
            raise VectorError(
                "No vector store connection is enabled. Enable one under Services → Vector stores.",
                "invalid",
            )
        if not connection.enabled:
            raise VectorError(f"Connection '{connection.name}' is disabled.", "invalid")
        if connection.provider_type not in IMPLEMENTED_BACKENDS:
            raise VectorError(
                f"The {connection.get_provider_type_display()} backend is not available.",
                "invalid",
            )

        embedding_model = ""
        if embedding_provider is not None:
            if not embedding_provider.enabled:
                raise VectorError(
                    f"Embedding provider '{embedding_provider.name}' is disabled.", "invalid"
                )
            if embedding_provider.dimension is None:
                raise VectorError(
                    f"Embedding provider '{embedding_provider.name}' has not been tested yet — "
                    "run Test on it so its vector dimension is known.",
                    "invalid",
                )
            if dimension not in (None, embedding_provider.dimension):
                raise VectorError(
                    f"Dimension {dimension} does not match the provider's recorded "
                    f"{embedding_provider.dimension}.",
                    "invalid",
                )
            dimension = embedding_provider.dimension
            embedding_model = embedding_provider.model
        if dimension is None:
            raise VectorError(
                "A dimension is required for a vectors-only collection.", "invalid"
            )
        try:
            dimension = int(dimension)
        except (TypeError, ValueError):
            raise VectorError("Dimension must be a whole number.", "invalid") from None
        if not 1 <= dimension <= MAX_DIMENSION:
            raise VectorError(f"Dimension must be between 1 and {MAX_DIMENSION}.", "invalid")

        with transaction.atomic():
            collection = VectorCollection(
                workspace=workspace,
                name=name,
                description=description or "",
                owner_plugin=owner_plugin,
                owner_key=owner_key,
                connection=connection,
                embedding_provider=embedding_provider,
                embedding_model=embedding_model,
                dimension=dimension,
                metric=metric,
                backend_ref="",
                created_by=created_by,
                include_in_backup=include_in_backup,
            )
            if connection.is_local:
                collection.backend_ref = VectorCollection.local_backend_ref(collection.id)
            else:
                collection.backend_ref = cls._unique_qdrant_ref(collection)
            collection.save()

        # The store is created after the row is committed: a rolled-back row must
        # never leave an orphan file (or server collection) behind, and a failed
        # store leaves a visible row in status=error instead of a mystery.
        try:
            cls._create_backend(collection)
        except Exception as e:
            err = _translate(e)
            collection.status = VectorCollection.STATUS_ERROR
            collection.last_error = str(err)
            collection.save(update_fields=["status", "last_error", "updated_at"])
            logger.error("Vector collection %s backend creation failed: %s", collection.name, err)
            raise err
        return collection

    @staticmethod
    def _unique_qdrant_ref(collection) -> str:
        """The physical Qdrant name, suffixed with the row's id when another row on
        the same connection already maps to it (names are unique per workspace,
        but ``ws8`` is only 8 hex digits and owned names write ``:`` as ``__``)."""
        ref = VectorCollection.qdrant_backend_ref(
            collection.connection.collection_prefix, collection.workspace_id, collection.name
        )
        taken = VectorCollection.objects.filter(
            connection=collection.connection, backend_ref=ref
        ).exclude(pk=collection.pk)
        return f"{ref}_{collection.id.hex[:8]}" if taken.exists() else ref

    @classmethod
    def _create_backend(cls, collection) -> None:
        if collection.is_local:
            from core.vectors.local_store import LocalStore

            LocalStore.create(collection.file_path, collection.dimension, collection.metric)
        else:
            cls._qdrant(collection).create()

    @classmethod
    def retry_backend(cls, collection) -> VectorCollection:
        """Re-create a missing store for a row in status=error.

        Local: a lost file is recreated empty. Qdrant: the server collection is
        created (or an empty one of the same shape adopted) — the same rule as
        at creation, since a row in error never had points of its own.
        """
        from core.vectors.local_store import LocalStore

        path = collection.file_path
        try:
            if not collection.is_local:
                cls._qdrant(collection).create()
            elif path is not None and not path.exists():
                LocalStore.create(path, collection.dimension, collection.metric)
            else:
                LocalStore.open(path)
        except Exception as e:
            err = _translate(e)
            collection.last_error = str(err)
            collection.save(update_fields=["last_error", "updated_at"])
            raise err
        collection.status = VectorCollection.STATUS_READY
        collection.last_error = ""
        collection.save(update_fields=["status", "last_error", "updated_at"])
        return collection

    @classmethod
    def delete_collection(cls, collection) -> None:
        """Remove the backend store (best effort, logged) and the row.

        A Qdrant drop that fails (server down) leaves the server collection
        behind; a later collection mapping to the same name refuses to adopt it
        while it holds points, so the leftover can't leak into a new collection.
        """
        try:
            cls._delete_backend(collection)
        except Exception as e:
            logger.warning("Vector collection %s store cleanup failed: %s", collection.name, e)
        collection.delete()

    @classmethod
    def _delete_backend(cls, collection) -> None:
        if not collection.is_local:
            cls._qdrant(collection).drop()
            return
        from core.vectors.local_store import LocalStore

        path = collection.file_path
        if path is not None:
            LocalStore(path, collection.dimension, collection.metric).delete_files()

    @staticmethod
    def _qdrant(collection):
        from core.vectors.qdrant_store import QdrantStore

        connection = collection.connection
        return QdrantStore(
            connection.url,
            collection.backend_ref,
            collection.dimension,
            collection.metric,
            api_key=connection.get_credentials().get("api_key", ""),
        )

    @classmethod
    def store_for(cls, collection):
        """The store behind ``collection``: the Local file (opened, meta checked) or Qdrant."""
        from core.vectors.local_store import LocalStore, LocalStoreError

        if not collection.connection.enabled:
            raise VectorError(
                f"Collection '{collection.name}': its vector store "
                f"'{collection.connection.name}' is disabled.",
                "backend_unavailable",
            )
        if not collection.is_local:
            try:
                return cls._qdrant(collection)
            except StoreError as e:
                raise VectorError(f"Collection '{collection.name}': {e}", e.code) from e

        path = collection.file_path
        try:
            store = LocalStore.open(path)
        except LocalStoreError as e:
            raise VectorError(f"Collection '{collection.name}': {e}", "backend_unavailable") from e
        if store.dim != collection.dimension or store.metric != collection.metric:
            raise VectorError(
                f"Collection '{collection.name}': the store file says dim={store.dim} "
                f"metric={store.metric} but the row is pinned to dim={collection.dimension} "
                f"metric={collection.metric}.",
                "dimension_mismatch",
            )
        return store

    @classmethod
    def handle(cls, collection) -> "CollectionHandle":
        return CollectionHandle(collection, cls.store_for(collection))

    @staticmethod
    def size_bytes(collection) -> int:
        """Bytes on the data volume (Local only; a Qdrant collection's size is the server's)."""
        from core.vectors.local_store import LocalStore

        path = collection.file_path
        if path is None or not path.exists():
            return 0
        return LocalStore(path, collection.dimension, collection.metric).size_bytes()

    @staticmethod
    def credentials_for(collection) -> dict:
        """The native-access escape hatch: the Qdrant URL + key for one collection.

        Only for a collection on a Qdrant connection whose operator turned on
        "share credentials with runs" (``VectorConnection.exposes_credentials``).
        The payload also names the physical collection and the id mapping, so a
        script using ``qdrant-client`` reads and writes points PyRun understands.
        """
        from core.vectors.qdrant_store import ID_KEY, POINT_NAMESPACE, TEXT_KEY

        connection = collection.connection
        if connection.is_local:
            raise VectorError(
                f"Collection '{collection.name}' is on the Local store; credentials() "
                "exists only for collections on a Qdrant connection.",
                "unsupported",
            )
        if not connection.exposes_credentials:
            raise VectorError(
                f"The operator has not allowed runs to receive the credentials of "
                f"'{connection.name}' (Services → Vector stores → Share credentials with runs).",
                "credentials_not_allowed",
            )
        return {
            "backend": "qdrant",
            "url": connection.url,
            "api_key": connection.get_credentials().get("api_key", ""),
            "collection": collection.backend_ref,
            "dimension": collection.dimension,
            "metric": collection.metric,
            "point_id_namespace": str(POINT_NAMESPACE),
            "id_payload_key": ID_KEY,
            "text_payload_key": TEXT_KEY,
        }

    # ------------------------------------------------------------- services page

    @staticmethod
    def test_connection(connection) -> tuple[bool, str]:
        """Probe a connection; returns ``(ok, message)``. Never raises."""
        try:
            if connection.is_local:
                from core.checks import json1_available
                from core.vectors.local_store import LocalStore

                if not json1_available():
                    return False, "This Python's SQLite has no JSON1 functions."
                probe = settings.VECTORS_DIR / f".probe-{timezone.now().timestamp():.0f}.sqlite"
                try:
                    store = LocalStore.create(probe, 3, "cosine")
                    store.upsert([{"id": "p", "vector": [1.0, 0.0, 0.0], "payload": {"k": 1}}])
                    hits = store.search([1.0, 0.0, 0.0], k=1, flt={"k": 1})
                    if not hits or hits[0]["id"] != "p":
                        return False, "Probe search returned no hit."
                finally:
                    LocalStore(probe, 3, "cosine").delete_files()
                return True, f"Local store OK — files under {vectors_dir_display()}"

            url = connection.url.rstrip("/")
            if not url:
                return False, "A Qdrant URL is required."
            ok, why = is_safe_endpoint_url(url)
            if not ok:
                return False, why
            headers = {"Accept": "application/json"}
            api_key = connection.get_credentials().get("api_key")
            if api_key:
                headers["api-key"] = api_key
            response = requests.get(f"{url}/collections", headers=headers, timeout=10)
            if response.status_code in (401, 403):
                return False, "Qdrant rejected the API key."
            if response.status_code >= 400:
                return False, f"Qdrant answered HTTP {response.status_code}."
            count = len((response.json().get("result") or {}).get("collections") or [])
            return True, f"Qdrant reachable — {count} collection(s) on the server."
        except requests.RequestException as e:
            return False, f"Could not reach the server: {e}"
        except Exception as e:
            logger.exception("Vector connection test failed")
            return False, str(e)

    @staticmethod
    def test_provider(provider):
        """Embed the test sentence; record dimension, last_tested_at and usage."""
        from core.vectors.embeddings import EmbeddingClient

        try:
            result = EmbeddingClient(provider).test()
        except Exception as e:
            raise _translate(e) from e
        # A UUID pk exists before the first save, so "saved" is the adding flag,
        # not the pk: a transient probe from the Services form must not be written.
        if not provider._state.adding:
            EmbeddingProvider.objects.filter(pk=provider.pk).update(
                dimension=result.dimension,
                last_tested_at=timezone.now(),
                tokens_embedded=F("tokens_embedded") + result.tokens,
                embed_calls=F("embed_calls") + result.calls,
            )
            provider.refresh_from_db()
        else:
            provider.dimension = result.dimension
        return result


class CollectionHandle:
    """Every operation on one collection — the contract every door exposes."""

    def __init__(self, collection: VectorCollection, store):
        self.collection = collection
        self.store = store

    # ------------------------------------------------------------- writes

    def add_texts(self, items) -> dict:
        """``[{"id", "text", "payload"?}]`` → embed with the pinned provider, upsert."""
        provider = self._provider_for_writes()
        items = list(items or [])
        if not items:
            return {"added": 0, "tokens": 0, "truncated": 0}
        texts = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise VectorError("each item needs a string 'text'", "invalid")
            texts.append(item["text"])
        result = self._embed(provider, texts, input_type="document")
        points = [
            {
                "id": item.get("id"),
                "vector": vector,
                "payload": item.get("payload") or {},
                "text": item["text"],
            }
            for item, vector in zip(items, result.vectors)
        ]
        added = self._upsert(points)
        return {"added": added, "tokens": result.tokens, "truncated": result.truncated}

    def add_vectors(self, items) -> dict:
        """``[{"id", "vector", "payload"?, "text"?}]`` → upsert as given."""
        added = self._upsert(list(items or []))
        return {"added": added}

    def delete(self, ids=None, filter=None) -> dict:
        try:
            removed = self.store.delete(ids=ids, flt=filter)
        except Exception as e:
            raise _translate(e) from e
        self._refresh_count()
        return {"deleted": removed}

    # ------------------------------------------------------------- reads

    def search(self, query: str, k: int = 5, filter=None) -> list:
        if not isinstance(query, str) or not query.strip():
            raise VectorError("search needs a non-empty query string", "invalid")
        provider = self._provider_for_reads()
        vector = self._embed(provider, [query], input_type="query").vectors[0]
        return self.search_vector(vector, k=k, filter=filter)

    def search_vector(self, vector, k: int = 5, filter=None) -> list:
        try:
            k = int(k)
        except (TypeError, ValueError):
            raise VectorError("k must be a whole number", "invalid") from None
        if not 1 <= k <= MAX_SEARCH_K:
            raise VectorError(f"k must be between 1 and {MAX_SEARCH_K}", "invalid")
        try:
            return self.store.search(vector, k=k, flt=filter)
        except Exception as e:
            raise _translate(e) from e

    def get(self, ids, include_vectors: bool = False) -> list:
        try:
            return self.store.get(list(ids or []), include_vectors=include_vectors)
        except Exception as e:
            raise _translate(e) from e

    def count(self, filter=None) -> int:
        try:
            return self.store.count(flt=filter)
        except Exception as e:
            raise _translate(e) from e

    def scroll(self, cursor=None, limit: int = 100, filter=None, include_vectors: bool = False):
        try:
            return self.store.scroll(
                cursor=cursor, limit=limit, flt=filter, include_vectors=include_vectors
            )
        except Exception as e:
            raise _translate(e) from e

    def embed(self, texts, input_type: str = "document") -> list:
        """Raw provider call through the collection (counted); returns the vectors."""
        provider = self._provider_for_reads()
        return self._embed(provider, list(texts or []), input_type=input_type).vectors

    def stats(self) -> dict:
        return {
            "points": self.collection.points_count,
            "size_bytes": VectorService.size_bytes(self.collection),
            "dimension": self.collection.dimension,
            "metric": self.collection.metric,
            "embedding_model": self.collection.embedding_model,
            "tokens_embedded": self.collection.tokens_embedded,
            "embed_calls": self.collection.embed_calls,
        }

    # ------------------------------------------------------------- internals

    def _provider_for_reads(self) -> EmbeddingProvider:
        provider = self.collection.embedding_provider
        if provider is None:
            raise VectorError(
                f"Collection '{self.collection.name}' has no embedding provider; "
                "use search_vector()/add_vectors() with your own vectors.",
                "invalid",
            )
        if not provider.enabled:
            raise VectorError(f"Embedding provider '{provider.name}' is disabled.", "invalid")
        return provider

    def _provider_for_writes(self) -> EmbeddingProvider:
        provider = self._provider_for_reads()
        if provider.model != self.collection.embedding_model:
            raise VectorError(
                f"Collection '{self.collection.name}' was embedded with model "
                f"'{self.collection.embedding_model}', but provider '{provider.name}' now uses "
                f"'{provider.model}'. Create a new collection for the new model and re-embed.",
                "model_mismatch",
            )
        if provider.dimension not in (None, self.collection.dimension):
            raise VectorError(
                f"Provider '{provider.name}' now produces {provider.dimension}-dimensional "
                f"vectors; collection '{self.collection.name}' is pinned to "
                f"{self.collection.dimension}. Create a new collection.",
                "dimension_mismatch",
            )
        return provider

    def _embed(self, provider, texts, *, input_type):
        from core.vectors.embeddings import EmbeddingClient

        try:
            result = EmbeddingClient(provider).embed(texts, input_type=input_type)
        except Exception as e:
            raise _translate(e) from e
        self._record_usage(provider, result)
        return result

    def _record_usage(self, provider, result) -> None:
        if not result.calls:
            return
        VectorCollection.objects.filter(pk=self.collection.pk).update(
            tokens_embedded=F("tokens_embedded") + result.tokens,
            embed_calls=F("embed_calls") + result.calls,
        )
        EmbeddingProvider.objects.filter(pk=provider.pk).update(
            tokens_embedded=F("tokens_embedded") + result.tokens,
            embed_calls=F("embed_calls") + result.calls,
        )
        self.collection.refresh_from_db(fields=["tokens_embedded", "embed_calls"])
        provider.refresh_from_db(fields=["tokens_embedded", "embed_calls"])

    def _upsert(self, points) -> int:
        try:
            added = self.store.upsert(points)
        except Exception as e:
            raise _translate(e) from e
        self._refresh_count()
        return added

    def _refresh_count(self) -> None:
        count = self.store.count()
        VectorCollection.objects.filter(pk=self.collection.pk).update(points_count=count)
        self.collection.points_count = count

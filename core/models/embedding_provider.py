"""
Embedding provider profiles (vector seam).

One row per configured embeddings endpoint. Deliberately a separate model from
``AIProvider``: that profile is shaped around the Claude Code CLI (subscription
tokens, Anthropic-compatible chat endpoints) and has no embeddings API, while
every embeddings vendor speaks the same OpenAI-compatible ``/v1/embeddings``
shape. Presets carry the base URL, default model, batch limit and the one or two
request quirks; the row stores what the user entered plus the ``dimension`` the
Test button recorded — a collection pins that dimension (and the model) when it
is created, so a changed model can never silently change the vector space.
"""

import uuid

from django.db import models


class EmbeddingProvider(models.Model):
    class ProviderType(models.TextChoices):
        OPENAI = "openai", "OpenAI"
        VOYAGE = "voyage", "Voyage AI"
        OLLAMA = "ollama", "Ollama (local)"
        ZAI = "zai", "Z.AI"
        JINA = "jina", "Jina AI"
        CUSTOM = "custom", "Custom OpenAI-compatible"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider_type = models.CharField(
        max_length=20,
        choices=ProviderType.choices,
        help_text="Which embeddings vendor this profile talks to",
    )
    name = models.CharField(
        max_length=100,
        unique=True,
        help_text="Display name, e.g. 'OpenAI small' or 'Ollama nomic'",
    )
    base_url = models.CharField(
        max_length=255,
        blank=True,
        help_text="Endpoint base URL; the embeddings path is appended per preset",
    )
    credential_encrypted = models.TextField(
        blank=True,
        help_text="API key (encrypted). Optional for Ollama and custom endpoints.",
    )
    model = models.CharField(
        max_length=100,
        help_text="Embedding model id sent with every request",
    )
    dimension = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Vector size recorded by the last successful Test (null until tested)",
    )
    max_batch = models.PositiveIntegerField(
        default=64,
        help_text="Texts per request (preset default; lower it if the vendor rejects batches)",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Switch off without losing the configuration",
    )
    last_tested_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this provider last embedded the test sentence successfully",
    )
    tokens_embedded = models.BigIntegerField(
        default=0,
        help_text="Running total of tokens sent for embedding (provider count or len//4 estimate)",
    )
    embed_calls = models.BigIntegerField(
        default=0,
        help_text="Running total of embeddings requests made through this profile",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "embedding_providers"
        ordering = ["name"]
        verbose_name = "embedding provider"
        verbose_name_plural = "embedding providers"

    def __str__(self):
        return f"{self.name} ({self.get_provider_type_display()})"

    @property
    def preset(self) -> dict:
        return EMBEDDING_PRESETS[self.provider_type]

    @property
    def credential_required(self) -> bool:
        return bool(self.preset["credential_required"])

    @property
    def is_tested(self) -> bool:
        return self.dimension is not None

    @property
    def endpoint_url(self) -> str:
        """``base_url`` + the preset's embeddings path."""
        return self.base_url.rstrip("/") + self.preset["embeddings_path"]

    def get_credential(self) -> str:
        """Decrypt the API key ('' when none is stored)."""
        if not self.credential_encrypted:
            return ""
        from core.services import EncryptionService

        return EncryptionService.decrypt(self.credential_encrypted)

    def set_credential(self, plaintext: str) -> None:
        from core.services import EncryptionService

        self.credential_encrypted = EncryptionService.encrypt(plaintext) if plaintext else ""


# Static per-type defaults and UI hints, consumed by the Services → Embedding
# providers card and the client. Code-level by design (same stance as
# ``PROVIDER_PRESETS``): the DB stores only what the user entered.
#
# ``query_hint`` is the one protocol quirk: vendors that embed documents and
# queries differently take a parameter naming which side a text is on.
EMBEDDING_PRESETS = {
    EmbeddingProvider.ProviderType.OPENAI: {
        "label": "OpenAI",
        "base_url": "https://api.openai.com",
        "base_url_editable": True,
        "embeddings_path": "/v1/embeddings",
        "default_model": "text-embedding-3-small",
        "model_placeholder": "text-embedding-3-small",
        "max_batch": 100,
        "credential_required": True,
        "credential_label": "OpenAI API key",
        "credential_help": "From platform.openai.com/api-keys.",
        "query_hint": None,
        "docs_url": "https://platform.openai.com/docs/guides/embeddings",
        "notes": "",
    },
    EmbeddingProvider.ProviderType.VOYAGE: {
        "label": "Voyage AI",
        "base_url": "https://api.voyageai.com",
        "base_url_editable": False,
        "embeddings_path": "/v1/embeddings",
        "default_model": "voyage-3.5-lite",
        "model_placeholder": "voyage-3.5-lite",
        "max_batch": 128,
        "credential_required": True,
        "credential_label": "Voyage API key",
        "credential_help": "From dash.voyageai.com.",
        "query_hint": {"param": "input_type", "document": "document", "query": "query"},
        "docs_url": "https://docs.voyageai.com/docs/embeddings",
        "notes": "Documents and queries are embedded with the matching input_type automatically.",
    },
    EmbeddingProvider.ProviderType.OLLAMA: {
        "label": "Ollama (local)",
        "base_url": "http://localhost:11434",
        "base_url_editable": True,
        "embeddings_path": "/v1/embeddings",
        "default_model": "nomic-embed-text",
        "model_placeholder": "nomic-embed-text",
        "max_batch": 32,
        "credential_required": False,
        "credential_label": "Token (optional)",
        "credential_help": "Not needed for a plain Ollama; set one if a proxy in front requires it.",
        "query_hint": None,
        "docs_url": "https://docs.ollama.com/api/openai-compatibility",
        "notes": (
            "Pull the model first (ollama pull nomic-embed-text). From Docker, use "
            "http://host.docker.internal:11434 to reach Ollama on the host. A localhost "
            "or private-network URL needs PYRUNNER_ALLOW_PRIVATE_NETWORK=true."
        ),
    },
    EmbeddingProvider.ProviderType.ZAI: {
        "label": "Z.AI",
        "base_url": "https://api.z.ai/api/paas/v4",
        "base_url_editable": True,
        "embeddings_path": "/embeddings",
        "default_model": "embedding-3",
        "model_placeholder": "embedding-3",
        "max_batch": 32,
        "credential_required": True,
        "credential_label": "Z.AI API key",
        "credential_help": "From z.ai — a standard API key (Coding Plan keys do not cover embeddings).",
        "query_hint": None,
        "docs_url": "https://docs.z.ai/",
        "notes": "",
    },
    EmbeddingProvider.ProviderType.JINA: {
        "label": "Jina AI",
        "base_url": "https://api.jina.ai",
        "base_url_editable": False,
        "embeddings_path": "/v1/embeddings",
        "default_model": "jina-embeddings-v3",
        "model_placeholder": "jina-embeddings-v3",
        "max_batch": 100,
        "credential_required": True,
        "credential_label": "Jina API key",
        "credential_help": "From jina.ai/embeddings.",
        "query_hint": {"param": "task", "document": "retrieval.passage", "query": "retrieval.query"},
        "docs_url": "https://jina.ai/embeddings/",
        "notes": "Documents and queries are embedded with the matching task automatically.",
    },
    EmbeddingProvider.ProviderType.CUSTOM: {
        "label": "Custom OpenAI-compatible",
        "base_url": "",
        "base_url_editable": True,
        "embeddings_path": "/v1/embeddings",
        "default_model": "",
        "model_placeholder": "model id the endpoint expects",
        "max_batch": 64,
        "credential_required": False,
        "credential_label": "API key (optional)",
        "credential_help": "Sent as a Bearer token when set.",
        "query_hint": None,
        "docs_url": "",
        "notes": "Any endpoint that answers POST /v1/embeddings in the OpenAI shape: LiteLLM, vLLM, text-embeddings-inference, LM Studio, …",
    },
}

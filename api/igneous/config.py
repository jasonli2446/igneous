from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

API_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Every setting is IGNEOUS_<NAME> in the environment or in api/.env, wherever the process starts."""
    model_config = SettingsConfigDict(env_prefix="IGNEOUS_", env_file=API_DIR / ".env", extra="ignore")

    database_url: str = "postgresql+psycopg://igneous:igneous@localhost:5432/igneous"
    # The OAuth client, the key that encrypts stored tokens, and the Enron mailboxes. A relative path is relative
    # to the api folder.
    data_dir: str = "../data"
    # Where the browser UI lives; the Google sign-in callback sends you back here.
    web_url: str = "http://localhost:3000"
    # The Local files folders, comma-separated. Empty: the Documents, Desktop, Downloads and iCloud Drive folders
    # that exist (sources.fs_roots).
    filesystem_roots: str = ""
    # Local documents and Apple Notes never go to an embedding or chat model unless explicitly enabled.
    local_sources_model_enabled: bool = False
    # Drive API page size for files.list and changes.list (1-1000).
    drive_page_size: int = 100
    # Gmail API quota units per minute the connector paces itself to. Google publishes 15,000; a project in
    # Testing mode is cut off near 730, so the default stays under that. Raise it to your project's quota.
    gmail_units_per_min: float = 600
    embedding_provider: str = "fastembed"
    # chunks.embedding is vector(384) (migration 0001): a model with another dimension, or another model's vector
    # space, needs a migration that changes the column and re-embeds every chunk.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    embedding_batch_size: int = 128
    rerank_provider: str = "cross_encoder"
    # Cross-encoder runs only in Q&A after graph expansion. It never blocks search if unavailable.
    reranker_enabled: bool = True
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    reranker_pool_size: int = 32
    rerank_batch_size: int = 64
    chat_provider: str = "bedrock"
    # Kill switch for every outbound model call (CO-1). Search and the graph still work without it.
    llm_enabled: bool = True
    # Comma-separated source ids (the Sources page shows them) whose records a hosted model may read: Ask,
    # extraction, link checks, thread naming. Empty, the default, sends nothing.
    llm_sources: str = ""
    llm_model: str = "us.anthropic.claude-sonnet-5-5"
    # High-volume yes/no judgments (verifying edges, pair same/related, umbrellas, summaries) use the cheap model;
    # nuanced ones (naming threads, picking their records, confirming a merge) use llm_model.
    llm_cheap_model: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    hosted_retries: int = 2
    hosted_retry_seconds: float = 1.0
    hosted_batch_size: int = 4

    @field_validator("data_dir")
    @classmethod
    def _relative_to_api(cls, value: str) -> str:
        path = Path(value).expanduser()
        return str(path if path.is_absolute() else (API_DIR / path).resolve())


settings = Settings()

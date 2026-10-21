from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IGNEOUS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://igneous:igneous@localhost:5432/igneous"
    data_dir: str = "../data"
    # Where the browser UI lives; the Google sign-in callback sends you back here.
    web_url: str = "http://localhost:3000"
    # Comma-separated filesystem roots used by ``igneous sync fs`` when --path is omitted.
    filesystem_roots: str = ""
    # Drive API page size for files.list and changes.list (1-1000).
    drive_page_size: int = 100
    # Fixed by the schema (see docs/decisions/0001-embeddings.md); changing it needs a migration.
    embedding_provider: str = "fastembed"
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
    # Sources eligible for automatic hosted-model extraction. Empty is deliberately
    # safe: importing/syncing a source never sends it to a model unless named here.
    llm_sources: str = ""
    llm_model: str = "us.anthropic.claude-sonnet-5-5"
    hosted_retries: int = 2
    hosted_retry_seconds: float = 1.0
    hosted_batch_size: int = 4


settings = Settings()

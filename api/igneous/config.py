from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IGNEOUS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://igneous:igneous@localhost:5432/igneous"
    data_dir: str = "../data"
    # Comma-separated filesystem roots used by ``igneous sync fs`` when --path is omitted.
    filesystem_roots: str = ""
    # Fixed by the schema (see docs/decisions/0001-embeddings.md); changing it needs a migration.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    # Kill switch for every outbound model call (CO-1). Search and the graph still work without it.
    llm_enabled: bool = True
    llm_model: str = "us.anthropic.claude-sonnet-5-5"


settings = Settings()

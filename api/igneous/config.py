from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IGNEOUS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://igneous:igneous@localhost:5432/igneous"
    data_dir: str = "../data"
    # Fixed by the schema (see docs/decisions/0001-embeddings.md); changing it needs a migration.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384


settings = Settings()

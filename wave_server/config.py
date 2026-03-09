from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "WAVE_", "env_file": ".env", "env_file_encoding": "utf-8"}

    data_dir: Path = Path("./data")
    database_url: str | None = None
    default_concurrency: int = 4
    default_timeout_ms: int = 300_000  # 5 minutes
    default_model: str = "claude-opus-4-6"
    runtime: str = "claude"
    default_model_worker: str | None = None
    default_model_test_writer: str | None = None
    default_model_wave_verifier: str | None = None
    github_token: str | None = None
    git_committer_name: str | None = None
    git_committer_email: str | None = None
    git_signing_key: str | None = None
    agents_dir: Path | None = None  # directory with agent .md files; falls back to hardcoded prompts
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:3001"]

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        db_path = self.data_dir / "wave-server.db"
        return f"sqlite+aiosqlite:///{db_path}"

    @property
    def storage_dir(self) -> Path:
        return self.data_dir / "storage"


settings = Settings()

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "WAVE_"}

    data_dir: Path = Path("./data")
    database_url: str | None = None
    default_concurrency: int = 4
    default_timeout_ms: int = 300_000  # 5 minutes
    runtime: str = "claude"
    cors_origins: list[str] = ["http://localhost:3000"]

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

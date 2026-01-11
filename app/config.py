from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # AssemblyAI
    assemblyai_api_key: str

    # Server
    environment: str = "development"
    debug: bool = False

    # CORS - comma-separated string that gets parsed to list
    cors_origins: str = "http://localhost:3000,http://localhost:8081,https://meetingsai.app,https://www.meetingsai.app,https://meetingsai-three.vercel.app"

    # Timeouts (in seconds)
    yt_dlp_timeout: int = 120
    transcription_timeout: int = 600  # 10 minutes for long videos

    # Limits
    max_video_duration_seconds: int = 7200  # 2 hours max

    # Tor proxy settings (disabled by default in development)
    tor_proxy_enabled: bool = False
    tor_proxy_url: str = "socks5://127.0.0.1:9050"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.environment.lower() in ("development", "dev", "local")

    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return self.environment.lower() == "production"

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS origins from comma-separated string to list."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()

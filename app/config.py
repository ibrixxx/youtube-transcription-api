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

    # yt-dlp sleep intervals (low values are fine for single-video requests)
    ytdlp_sleep_interval: int = 0
    ytdlp_max_sleep_interval: int = 2
    ytdlp_sleep_interval_requests: int = 0

    # Residential proxy (primary - for YouTube downloads)
    # Format: http://username:password@proxy-host:port
    residential_proxy_enabled: bool = False
    residential_proxy_url: str = ""

    # Legacy aliases for backward compatibility with .env files
    webshare_proxy_enabled: bool = False
    webshare_proxy_url: str = ""

    # Tor proxy settings (fallback when residential proxy not configured)
    # Using socks5h:// ensures DNS is also resolved through the proxy
    tor_proxy_enabled: bool = True
    tor_proxy_url: str = "socks5h://127.0.0.1:9050"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def proxy_enabled(self) -> bool:
        """Check if residential proxy is enabled (supports both new and legacy env vars)."""
        return self.residential_proxy_enabled or self.webshare_proxy_enabled

    @property
    def proxy_url(self) -> str:
        """Get residential proxy URL (supports both new and legacy env vars)."""
        return self.residential_proxy_url or self.webshare_proxy_url

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

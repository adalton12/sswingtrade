"""
Application Configuration & Settings Management
Uses environment variables with Pydantic BaseSettings
"""

from typing import List

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # ========== Application ==========
    APP_NAME: str = "SSWingTrade"
    APP_VERSION: str = "0.1.0"
    APP_ENV: str = "development"  # development, staging, production
    LOG_LEVEL: str = "DEBUG"

    # ========== Database ==========
    DATABASE_URL: str = "postgresql://trading:trading_dev_pwd@localhost:5432/sswingtrade"
    DATABASE_ECHO: bool = True  # Log SQL queries in development
    DATABASE_POOL_SIZE: int = 20
    DATABASE_POOL_RECYCLE: int = 3600

    # ========== Cache/Redis ==========
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_CACHE_TTL: int = 3600  # 1 hour

    # ========== Ollama (Local LLM) ==========
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "qwen2.5:3b"  # Lightweight model for CPU/GPU local inference
    OLLAMA_TIMEOUT: int = 30  # seconds

    # ========== Trading Capital Management ==========
    INITIAL_CAPITAL: float = 500.00  # BRL
    MONTHLY_DEPOSIT: float = 500.00  # BRL - added first business day of month
    MAX_DAILY_ALLOCATION: float = 100.00  # BRL - per operation/day
    MAX_WEEKLY_CAPITAL: float = 500.00  # BRL - max simultaneous exposure
    MAX_DAILY_LOSS_PERCENT: float = 1.5  # % of total capital (circuit breaker)

    # ========== Position Sizing & Risk ==========
    POSITION_SIZE_DYNAMIC: bool = True  # Scale allocation with compound growth
    STOP_LOSS_ATR_MULTIPLIER: float = 1.5  # 1.5x ATR for stop loss
    TAKE_PROFIT_RATIO: float = 2.0  # Risk/Reward ratio (target 1:2)
    MIN_SCORE_TO_TRADE: float = 60.0  # Minimum composite score (0-100)

    # ========== Feature Flags ==========
    ENABLE_PAPER_TRADING: bool = True
    ENABLE_REAL_TRADING: bool = False  # Only enable after extensive FASE 8 testing
    ENABLE_LLM_ANALYSIS: bool = True
    ENABLE_ML_MODELS: bool = True

    # ========== Market Data ==========
    MARKET_DATA_SOURCE: str = "yfinance"  # yfinance, eodhd, alpaca_free
    MARKET_DATA_UPDATE_FREQUENCY: str = "daily"  # daily, hourly
    MARKET_TIMEZONE: str = "America/Sao_Paulo"  # B3 timezone

    # ========== FASE 2: Data Collection ==========
    SCHEDULER_ENABLED: bool = True  # Enable/disable automatic data collection
    DAILY_COLLECTION_HOUR: int = 18  # Hour (BRT) to collect daily candles
    DAILY_COLLECTION_MINUTE: int = 30  # Minute to collect daily candles
    INTRADAY_COLLECTION_HOUR: int = 18  # Hour (BRT) to collect intraday candles
    INTRADAY_COLLECTION_MINUTE: int = 45
    DEFAULT_DAYS_BACK: int = 60  # Default history window for initial sync
    INTRADAY_DEFAULT_PERIOD: str = "5d"  # Default period for intraday data
    INTRADAY_DEFAULT_INTERVAL: str = "1h"  # Default intraday interval

    # ========== FASE 6: News / LLM batch ==========
    NEWS_AUTO_FETCH: bool = True          # fetch Google News RSS in the nightly batch
    NEWS_LOOKBACK_DAYS: int = 5
    LLM_BATCH_LIMIT: int = 40             # max news analysed per batch run (CPU inference is slow)

    # ========== FASE 5: ML ==========
    ML_MODEL_PATH: str = "/app/models"
    ML_MIN_TRAIN_ROWS: int = 600

    # ========== Monitoring & Observability ==========
    PROMETHEUS_ENABLED: bool = True
    PROMETHEUS_METRICS_PORT: int = 9090
    OTEL_TRACING_ENABLED: bool = False  # FASE 2+
    OTEL_COLLECTOR_ENDPOINT: str = "http://localhost:4317"

    # ========== Logging ==========
    LOG_FORMAT: str = "json"  # json or standard
    LOG_FILE_PATH: str = "/app/logs/sswingtrade.log"
    LOG_ROTATION: str = "10 MB"

    # ========== API & CORS ==========
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000",  # Frontend dev
        "http://localhost:8000",  # API docs
        "http://localhost:5173",  # Vite dev
    ]
    API_RATE_LIMIT: int = 1000  # requests per minute
    API_TIMEOUT: int = 30  # seconds

    # ========== Security ==========
    SECRET_KEY: str = "dev-secret-key-change-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    class Config:
        """Pydantic settings configuration."""
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


# Global settings instance
settings = Settings()


# ============================================================================
# Configuration Validation & Display
# ============================================================================

def validate_settings():
    """Validate critical settings at startup."""
    errors = []

    # Database URL
    if not settings.DATABASE_URL or not settings.DATABASE_URL.startswith("postgresql://"):
        errors.append("Invalid DATABASE_URL - must be PostgreSQL connection string")

    # Redis URL
    if not settings.REDIS_URL or not settings.REDIS_URL.startswith("redis://"):
        errors.append("Invalid REDIS_URL - must be Redis connection string")

    # Ollama
    if not settings.OLLAMA_BASE_URL or not settings.OLLAMA_BASE_URL.startswith("http"):
        errors.append("Invalid OLLAMA_BASE_URL - must be HTTP(S) URL")

    # Capital constraints
    if settings.MAX_DAILY_ALLOCATION > settings.MAX_WEEKLY_CAPITAL:
        errors.append(
            f"MAX_DAILY_ALLOCATION ({settings.MAX_DAILY_ALLOCATION}) "
            f"cannot exceed MAX_WEEKLY_CAPITAL ({settings.MAX_WEEKLY_CAPITAL})"
        )

    if errors:
        raise ValueError(f"Configuration validation errors:\n" + "\n".join(errors))


def print_settings():
    """Print active settings (mask sensitive data)."""
    settings_dict = settings.model_dump()

    # Mask sensitive fields
    masked_fields = ["SECRET_KEY", "DATABASE_URL", "REDIS_URL"]
    for field in masked_fields:
        if field in settings_dict:
            settings_dict[field] = "***MASKED***"

    print("\n" + "="*60)
    print("📋 SSWingTrade Configuration")
    print("="*60)
    for key, value in sorted(settings_dict.items()):
        print(f"  {key:<30} = {value}")
    print("="*60 + "\n")


if __name__ == "__main__":
    validate_settings()
    print_settings()

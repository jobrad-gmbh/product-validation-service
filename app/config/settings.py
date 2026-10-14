from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    API_V1_BASE_URL: str = "/api/v1"
    DATABASE_URL: str = "postgresql+psycopg://accessory_validator:accessory_validator@localhost:5432/accessory_validator"

    APP_ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT_STRING: str = "%(levelname)s  [%(name)s] %(message)s"

    # Optional so database-only commands (e.g. migrations) load without auth
    # configuration; the API refuses to start without it.
    AUTH_ISSUER: str | None = None
    AUTH_AUDIENCE: str = Field(default="accessory-validator", min_length=1)
    AUTH_JWKS_TIMEOUT_SECONDS: float = Field(default=5, gt=0, le=30)

    @field_validator("AUTH_ISSUER", mode="before")
    @classmethod
    def validate_issuer(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("AUTH_ISSUER must be a URL")
        # Validate only: tokens and discovery must match the issuer exactly, and
        # HttpUrl normalization (trailing slash, lowercase host) would change it.
        HttpUrl(value)
        return value


settings = Settings()

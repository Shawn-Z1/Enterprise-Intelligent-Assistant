from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """读取环境配置；凭证使用 SecretStr，禁止在日志中输出明文。"""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")
    app_name: str = "知行 · 企业智能助理"
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "zhixing"
    postgres_user: str = "zhixing"
    postgres_password: SecretStr
    business_db_user: str = "zhixing_business_reader"
    business_db_password: SecretStr
    redis_host: str = "redis"
    redis_port: int = 6379
    milvus_host: str = "milvus"
    milvus_port: int = 19530
    app_minio_endpoint: str = "minio:9000"
    app_minio_access_key: str
    app_minio_secret_key: SecretStr
    app_minio_bucket: str = "application-files"
    dependency_timeout_seconds: float = Field(default=4, ge=1, le=30)
    worker_interval_seconds: int = Field(default=10, ge=1, le=60)
    worker_stale_seconds: int = Field(default=35, ge=5, le=180)
    worker_state_path: str = "/tmp/zhixing-worker-health.json"
    session_hours: int = Field(default=24,ge=1,le=168)
    session_cookie_secure: bool = False
    login_limit_per_minute: int = Field(default=20,ge=3,le=100)
    primary_base_url: str = "https://api.deepseek.com"
    primary_api_key: SecretStr = SecretStr("")
    primary_model: str = "deepseek-flash"
    fallback_base_url: str = ""
    fallback_api_key: SecretStr = SecretStr("")
    fallback_model: str = "qwen-plus"
    primary_context_tokens: int = Field(default=0,ge=0,le=2_000_000)
    fallback_context_tokens: int = Field(default=0,ge=0,le=2_000_000)
    model_output_tokens: int = Field(default=2048,ge=64,le=32768)
    model_read_timeout_seconds: float = Field(default=45,ge=1,le=180)
    model_retry_backoff_seconds: float = Field(default=0.5,ge=0,le=10)
    model_circuit_failures: int = Field(default=3,ge=1,le=20)
    model_circuit_seconds: float = Field(default=30,ge=0.01,le=300)
    run_timeout_seconds: int = Field(default=240,ge=5,le=900)
    worker_concurrency: int = Field(default=2,ge=1,le=4)
    run_lease_seconds: int = Field(default=60,ge=20,le=300)
    knowledge_max_file_bytes: int = Field(default=20*1024*1024,ge=1024,le=50*1024*1024)
    knowledge_max_batch_bytes: int = Field(default=50*1024*1024,ge=1024,le=200*1024*1024)
    knowledge_max_files: int = Field(default=10,ge=1,le=30)
    knowledge_parse_timeout_seconds: int = Field(default=30,ge=2,le=120)


@lru_cache
def get_settings() -> Settings:
    """无参数读取并缓存进程配置；返回经类型验证的 Settings 实例。"""
    return Settings()

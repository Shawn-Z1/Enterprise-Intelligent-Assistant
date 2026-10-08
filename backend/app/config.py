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


@lru_cache
def get_settings() -> Settings:
    """无参数读取并缓存进程配置；返回经类型验证的 Settings 实例。"""
    return Settings()

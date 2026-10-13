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
    # 控制密钥仅供应用访问专用服务，执行客体不继承凭证。
    sandbox_enabled: bool = False
    sandbox_runner_url: str = "http://sandbox-runner:8091"
    sandbox_control_token: SecretStr = SecretStr("")
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
    # 审核职责独立配置，不随备用模型切换，也不退回主模型自审。
    review_base_url: str = ""
    review_api_key: SecretStr = SecretStr("")
    review_model: str = "qwen-plus"
    review_context_tokens: int = Field(default=0,ge=0,le=2_000_000)
    primary_context_tokens: int = Field(default=0,ge=0,le=2_000_000)
    fallback_context_tokens: int = Field(default=0,ge=0,le=2_000_000)
    # 输入、工具定义和输出预留合计最多使用窗口的这一比例，余量补偿参考分词及隐藏模板差异。
    model_context_safety_ratio: float = Field(default=0.95,ge=0.5,le=0.99)
    model_output_tokens: int = Field(default=4096,ge=64,le=32768)
    # 压缩触发线低于最终安全线；近期轮数是目标，当前输入和活动协议始终保留。
    context_compress_trigger_ratio: float = Field(default=0.75,ge=0.3,le=0.9)
    context_keep_turns: int = Field(default=4,ge=0,le=12)
    context_summary_tokens: int = Field(default=2048,ge=256,le=8192)
    context_compression_max_chunks: int = Field(default=16,ge=1,le=64)
    context_cache_ttl_seconds: int = Field(default=1800,ge=10,le=86400)
    context_cache_max_bytes: int = Field(default=2*1024*1024,ge=1024,le=16*1024*1024)
    # 自动记忆无用户开关；提取输出、画像和目录分别有界，索引故障不阻塞主对话。
    memory_extract_chars: int = Field(default=24000,ge=2000,le=100000)
    memory_overview_tokens: int = Field(default=4096,ge=1024,le=8192)
    memory_directory_tokens: int = Field(default=2048,ge=256,le=4096)
    memory_job_timeout_seconds: int = Field(default=180,ge=20,le=600)
    memory_collection_prefix: str = 'personal_memory'
    model_read_timeout_seconds: float = Field(default=45,ge=1,le=180)
    model_retry_backoff_seconds: float = Field(default=0.5,ge=0,le=10)
    model_circuit_failures: int = Field(default=3,ge=1,le=20)
    model_circuit_seconds: float = Field(default=30,ge=0.01,le=300)
    run_timeout_seconds: int = Field(default=240,ge=5,le=900)
    worker_concurrency: int = Field(default=2,ge=1,le=4)
    worker_shutdown_seconds: int = Field(default=60,ge=1,le=300)
    # 沙箱队列容量与排队时限独立于聊天worker额度，普通满额不直接失败。
    sandbox_global_concurrency: int = Field(default=2,ge=1,le=16)
    sandbox_user_concurrency: int = Field(default=1,ge=1,le=16)
    sandbox_queue_limit: int = Field(default=50,ge=1,le=500)
    sandbox_wait_seconds: int = Field(default=300,ge=10,le=1800)
    run_lease_seconds: int = Field(default=60,ge=20,le=300)
    knowledge_max_file_bytes: int = Field(default=20*1024*1024,ge=1024,le=50*1024*1024)
    knowledge_max_batch_bytes: int = Field(default=50*1024*1024,ge=1024,le=200*1024*1024)
    knowledge_max_files: int = Field(default=10,ge=1,le=30)
    knowledge_parse_timeout_seconds: int = Field(default=30,ge=2,le=120)
    retrieval_model_url: str = 'http://retrieval-models:8090'
    rag_collection_prefix: str = 'knowledge_chunks'
    rag_chunk_size: int = Field(default=600, ge=100, le=600)
    rag_chunk_overlap: int = Field(default=0, ge=0, le=0)
    rag_recall_limit: int = Field(default=20, ge=1, le=50)
    rag_rerank_limit: int = Field(default=20, ge=1, le=32)
    rag_final_limit: int = Field(default=5, ge=1, le=10)
    rag_min_score: float = Field(default=0.3, ge=0, le=1)
    rag_rrf_k: int = Field(default=60, ge=1, le=200)
    # 搜索服务独立于主备文本模型，空密钥明确返回未配置，不伪造联网结果。
    web_search_provider: str = 'aliyun_iqs'
    web_search_base_url: str = 'https://cloud-iqs.aliyuncs.com'
    web_search_engine_type: str = 'CNLiteBasic'
    web_search_api_key: SecretStr = SecretStr('')
    web_search_timeout_seconds: float = Field(default=12, ge=1, le=30)
    web_search_max_results: int = Field(default=5, ge=1, le=5)
    web_search_max_pages: int = Field(default=3, ge=0, le=3)
    web_search_response_bytes: int = Field(default=2097152, ge=1024, le=4194304)
    web_search_text_chars: int = Field(default=2500, ge=300, le=5000)
    web_search_per_minute: int = Field(default=10, ge=1, le=60)


@lru_cache
def get_settings() -> Settings:
    """无参数读取并缓存进程配置；返回经类型验证的 Settings 实例。"""
    return Settings()

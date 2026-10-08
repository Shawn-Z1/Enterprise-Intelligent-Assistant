import asyncio
from time import perf_counter
from typing import Awaitable, Callable

import asyncpg
import urllib3
from minio import Minio
from pymilvus import MilvusClient
from redis.asyncio import Redis

from app.config import Settings
from app.contracts import DependencyStatus


async def check_postgres(settings: Settings) -> str:
    """接收配置并实际连接 PG 执行 SELECT 1；返回证明文本，连接始终关闭。"""
    connection = await asyncpg.connect(host=settings.postgres_host, port=settings.postgres_port,
                                      user=settings.postgres_user, database=settings.postgres_db,
                                      password=settings.postgres_password.get_secret_value(),
                                      timeout=settings.dependency_timeout_seconds)
    try:
        assert await connection.fetchval("SELECT 1") == 1
        return "SELECT 1 成功"
    finally:
        await connection.close()


async def check_redis(settings: Settings) -> str:
    """接收配置，通过服务名发送 Redis PING；返回真实 PONG 结果说明。"""
    client = Redis(host=settings.redis_host, port=settings.redis_port,
                   socket_connect_timeout=settings.dependency_timeout_seconds,
                   socket_timeout=settings.dependency_timeout_seconds)
    try:
        assert await client.ping()
        return "PING / PONG 成功"
    finally:
        await client.aclose()


def probe_milvus(settings: Settings) -> str:
    """接收配置并通过 gRPC 查询 Milvus collection 目录；返回真实目录数量。"""
    client = MilvusClient(uri=f"http://{settings.milvus_host}:{settings.milvus_port}",
                          timeout=settings.dependency_timeout_seconds)
    try:
        collections = client.list_collections(timeout=settings.dependency_timeout_seconds)
        return f"gRPC list_collections 成功，集合数 {len(collections)}"
    finally:
        client.close()


def probe_minio(settings: Settings) -> str:
    """接收配置，以应用专属凭证验证 Bucket 存在；返回文件存储连通说明。"""
    http_client = urllib3.PoolManager(timeout=urllib3.Timeout(total=settings.dependency_timeout_seconds),
                                     retries=False)
    try:
        client = Minio(settings.app_minio_endpoint, access_key=settings.app_minio_access_key,
                       secret_key=settings.app_minio_secret_key.get_secret_value(),
                       secure=False, region="us-east-1", http_client=http_client)
        if not client.bucket_exists(settings.app_minio_bucket):
            raise RuntimeError("应用 Bucket 未初始化")
        return "应用专属凭证 / Bucket HEAD 成功"
    finally:
        http_client.clear()


async def measure(name: str, target: str, probe: Callable[[], Awaitable[str]], timeout: float) -> tuple[str, DependencyStatus]:
    """接收探针名称、目标、异步探针与超时；返回安全的耗时及成功/失败状态。"""
    started = perf_counter()
    try:
        detail = await asyncio.wait_for(probe(), timeout=timeout)
        status = "ready"
    except Exception as error:
        # 仅暴露异常类型，防止驱动异常中的连接参数和凭证泄漏。
        detail = f"依赖不可用（{type(error).__name__}）"
        status = "unavailable"
    return name, DependencyStatus(status=status, target=target,
                                  latency_ms=round((perf_counter() - started) * 1000, 2), detail=detail)


async def check_dependencies(settings: Settings) -> dict[str, DependencyStatus]:
    """接收配置并并发验证四种真实依赖；返回按服务名索引的探针结果。"""
    # 探针绑定容器服务名，避免将容器自己的 localhost 当作其他服务。
    probes = [
        ("postgres", f"{settings.postgres_host}:{settings.postgres_port}", lambda: check_postgres(settings)),
        ("redis", f"{settings.redis_host}:{settings.redis_port}", lambda: check_redis(settings)),
        ("milvus", f"{settings.milvus_host}:{settings.milvus_port}", lambda: asyncio.to_thread(probe_milvus, settings)),
        ("minio", settings.app_minio_endpoint, lambda: asyncio.to_thread(probe_minio, settings)),
    ]
    results = await asyncio.gather(*(measure(name, target, probe, settings.dependency_timeout_seconds + 1)
                                     for name, target, probe in probes))
    return dict(results)

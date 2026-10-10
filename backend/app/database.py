import json

import asyncpg

from app.config import Settings
from app.timezones import encode_timestamp, decode_timestamp


async def configure_connection(connection: asyncpg.Connection) -> None:
    """接收 PG 连接，配置 JSON/JSONB 编解码；后续读写返回原生对象，无返回值。"""
    for kind in ("json", "jsonb"):
        await connection.set_type_codec(kind, schema="pg_catalog", encoder=json.dumps, decoder=json.loads)
    await connection.execute("SET TIME ZONE 'Asia/Shanghai'")
    await connection.set_type_codec('timestamptz', schema='pg_catalog', format='tuple',
                                    encoder=encode_timestamp, decoder=decode_timestamp)


async def create_app_pool(settings: Settings) -> asyncpg.Pool:
    """接收应用配置，创建低权限连接池；返回可用于应用事务与审计的连接池。"""
    return await asyncpg.create_pool(host=settings.postgres_host, port=settings.postgres_port,
                                    database=settings.postgres_db, user=settings.postgres_user,
                                    password=settings.postgres_password.get_secret_value(),
                                    min_size=1, max_size=5, command_timeout=10,
                                    init=configure_connection)


async def connect_business(settings: Settings) -> asyncpg.Connection:
    """接收配置，使用独立只读身份连接业务 schema；返回连接，调用方负责关闭。"""
    connection = await asyncpg.connect(host=settings.postgres_host, port=settings.postgres_port,
                                      database=settings.postgres_db, user=settings.business_db_user,
                                      password=settings.business_db_password.get_secret_value(),
                                      timeout=4, command_timeout=10)
    await configure_connection(connection)
    # 生成SQL显式限定业务视图；函数解析只走pg_catalog，不能被public同名函数劫持。
    await connection.execute('SET search_path = pg_catalog')
    return connection

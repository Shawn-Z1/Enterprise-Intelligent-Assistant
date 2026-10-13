import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from app.ids import ShortId, new_id

import asyncpg
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.audit import AuditRecorder
from app.database import configure_connection
from app.passwords import hash_password


class BootstrapSettings(BaseSettings):
    """仅显式管理任务取得管理员与种子凭证，API/worker 不持有数据库管理员密码。"""

    model_config = SettingsConfigDict(extra="ignore")
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "zhixing"
    postgres_user: str = "zhixing"
    postgres_password: SecretStr
    app_db_user: str = "zhixing_app"
    app_db_password: SecretStr
    business_db_user: str = "zhixing_business_reader"
    business_db_password: SecretStr
    initial_admin_password: SecretStr
    initial_demo_password: SecretStr
    database_root: str = "/database"


def identifier(value: str) -> str:
    """接收可配置角色/数据库标识，校验限定字符并返回带引号标识，拒绝 SQL 注入。"""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,50}", value):
        raise ValueError("数据库和角色名称必须使用小写字母、数字或下划线")
    return f'"{value}"'


def scram_verifier(password: str) -> str:
    """接收数据库明文密码，按 SCRAM-SHA-256 生成验证值；返回哈希，SQL 中不放明文密码。"""
    if len(password) < 12 or password == "replace-with-random-password":
        raise ValueError("数据库密码至少 12 位，且不能使用示例占位值")
    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 4096)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    encode = base64.b64encode
    return f"SCRAM-SHA-256$4096:{encode(salt).decode()}${encode(stored_key).decode()}:{encode(server_key).decode()}"


async def connect_admin(settings: BootstrapSettings) -> asyncpg.Connection:
    """接收管理任务配置，连接管理员身份并配置 JSON 编解码；返回需关闭的连接。"""
    conn = await asyncpg.connect(host=settings.postgres_host, port=settings.postgres_port,
                                database=settings.postgres_db, user=settings.postgres_user,
                                password=settings.postgres_password.get_secret_value(), timeout=10)
    await configure_connection(conn)
    return conn


async def provision_roles(conn: asyncpg.Connection, settings: BootstrapSettings) -> str:
    """接收管理员连接和配置，创建分离角色并收紧公共权限；返回无登录结构拥有者名称。"""
    owner = settings.app_db_user + "_owner"
    names = {owner, settings.app_db_user, settings.business_db_user, settings.postgres_user}
    if len(names) != 4:
        raise ValueError("数据库管理员、结构拥有者、应用和业务身份必须分开")
    for name, password in ((owner, None), (settings.app_db_user, settings.app_db_password),
                           (settings.business_db_user, settings.business_db_password)):
        role = identifier(name)
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)", name):
            login = "NOLOGIN" if password is None else f"LOGIN PASSWORD '{scram_verifier(password.get_secret_value())}'"
            await conn.execute(f"CREATE ROLE {role} {login} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
        else:
            unsafe = await conn.fetchval("SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname=$1", name)
            if unsafe:
                raise RuntimeError("已有受控角色具备超出设计的权限，停止初始化")
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname=$1)", name):
                raise RuntimeError("受控角色不应具有其他角色的成员权限")
    database = identifier(settings.postgres_db)
    await conn.execute(f"REVOKE ALL ON DATABASE {database} FROM PUBLIC; REVOKE ALL ON SCHEMA public FROM PUBLIC")
    # 无登录结构拥有者需要事务内临时映射表；应用和业务只读身份仍没有建表权限。
    await conn.execute(f"GRANT CONNECT,CREATE,TEMPORARY ON DATABASE {database} TO {identifier(owner)}")
    await conn.execute(f"GRANT CONNECT ON DATABASE {database} TO {identifier(settings.app_db_user)}, {identifier(settings.business_db_user)}")
    await conn.execute(f"ALTER ROLE {identifier(settings.business_db_user)} SET default_transaction_read_only=on")
    await conn.execute(f"ALTER ROLE {identifier(settings.business_db_user)} SET search_path=demo_business,pg_catalog")
    await conn.execute(f"ALTER DATABASE {database} SET timezone='Asia/Shanghai'")
    for role_name in (settings.postgres_user, owner, settings.app_db_user, settings.business_db_user):
        await conn.execute(f"ALTER ROLE {identifier(role_name)} SET timezone='Asia/Shanghai'")
    return owner


async def grant_runtime_access(conn: asyncpg.Connection, settings: BootstrapSettings) -> None:
    """接收管理连接和配置，按 schema 授予应用权限和五张业务表的只读权限；无返回值。"""
    app_role = identifier(settings.app_db_user)
    reader = identifier(settings.business_db_user)
    app_schemas = "app_system,app_runtime,app_knowledge,app_memory"
    all_schemas = app_schemas + ",app_audit,demo_business"
    await conn.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {all_schemas} FROM PUBLIC")
    await conn.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {all_schemas} FROM PUBLIC")
    await conn.execute(f"GRANT USAGE ON SCHEMA {app_schemas},app_audit TO {app_role}")
    await conn.execute(f"GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA {app_schemas} TO {app_role}")
    await conn.execute(f"REVOKE ALL ON app_system.schema_migrations,app_system.seed_receipts FROM {app_role}")
    await conn.execute(f"GRANT SELECT ON app_system.schema_migrations TO {app_role}")
    for table in ("app_runtime.messages", "app_runtime.run_events", "app_runtime.checkpoints",
                  "app_runtime.context_summaries", "app_runtime.retrievals", "app_knowledge.reviews", "app_knowledge.parses", "app_memory.extractions", "app_memory.snapshots", "app_memory.consolidations"):
        await conn.execute(f"REVOKE UPDATE ON {table} FROM {app_role}")
    await conn.execute(f"GRANT SELECT,INSERT ON ALL TABLES IN SCHEMA app_audit TO {app_role}")
    await conn.execute(f"GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {app_schemas},app_audit TO {app_role}")
    await conn.execute(f"GRANT USAGE ON SCHEMA demo_business TO {reader}")
    if await conn.fetchval("SELECT to_regclass('demo_business.visible_employees')"):
        # 业务账号只读有能力及行范围保护的视图，不能绕过视图直接访问底表。
        await conn.execute(f"REVOKE SELECT ON demo_business.departments,demo_business.employees,demo_business.attendance_records,demo_business.approval_records,demo_business.tickets FROM {reader}")
        await conn.execute(f"GRANT SELECT ON demo_business.visible_departments,demo_business.visible_employees,demo_business.visible_attendance_records,demo_business.visible_approval_records,demo_business.visible_tickets TO {reader}")
        await conn.execute(f"GRANT EXECUTE ON FUNCTION demo_business.authorized_query_scope() TO {reader}")
        if await conn.fetchval("SELECT to_regclass('demo_business.visible_code_submissions')"):
            await conn.execute(f"REVOKE ALL ON demo_business.code_submissions FROM {reader}")
            await conn.execute(f"GRANT SELECT ON demo_business.visible_code_submissions TO {reader}")
    else:
        await conn.execute(f"GRANT SELECT ON demo_business.departments,demo_business.employees,demo_business.attendance_records,demo_business.approval_records,demo_business.tickets TO {reader}")


async def migrate(conn: asyncpg.Connection, settings: BootstrapSettings) -> dict:
    """接收管理连接及配置，校验并事务执行外置迁移；返回应用和跳过的版本列表。"""
    applied, skipped = [], []
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(8200201)")
        owner = await provision_roles(conn, settings)
        for path in sorted((Path(settings.database_root) / "migrations").glob("*.sql")):
            content = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(content.encode()).hexdigest()
            existing = None
            if await conn.fetchval("SELECT to_regclass('app_system.schema_migrations')"):
                existing = await conn.fetchval("SELECT sha256 FROM app_system.schema_migrations WHERE version=$1", path.name)
            if existing:
                if existing != digest:
                    raise RuntimeError("已应用迁移被修改，请新增迁移文件")
                skipped.append(path.name)
                continue
            await conn.execute(f"SET LOCAL ROLE {identifier(owner)}")
            await conn.execute(content)
            await conn.execute("INSERT INTO app_system.schema_migrations(version,sha256) VALUES($1,$2)", path.name, digest)
            await conn.execute("RESET ROLE")
            applied.append(path.name)
        if not applied and not skipped:
            raise RuntimeError("未找到外置结构迁移文件")
        await grant_runtime_access(conn, settings)
        await AuditRecorder(None, "db-admin").append(new_id(), "database.migrated", {"applied": applied, "skipped": skipped}, connection=conn)
    return {"applied": applied, "skipped": skipped}


async def seed(conn: asyncpg.Connection, settings: BootstrapSettings) -> dict:
    """接收管理连接及配置，事务装载外置 SQL/账号清单；凭回执保持幂等且不重置权限或密码。"""
    applied, skipped, created_accounts = [], [], 0
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(8200201)")
        # 初始化脚本中的未标注时区时间统一按北京时间解释；不改已导入记录或历史文件指纹。
        await conn.execute("SET LOCAL TIME ZONE 'Asia/Shanghai'")
        # 新版种子独立保存，历史文件及其指纹不变，兼容现有数据库升级和全新部署。
        seed_root = Path(settings.database_root) / "seeds_short_ids"
        for path in sorted(seed_root.iterdir()):
            if path.suffix not in (".sql", ".json"):
                continue
            content = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(content.encode()).hexdigest()
            source_key = "short_ids/" + path.name
            existing = await conn.fetchval("SELECT sha256 FROM app_system.seed_receipts WHERE source_key=$1", source_key)
            if existing:
                if existing != digest:
                    raise RuntimeError("已导入种子文件被修改，请新增种子文件")
                skipped.append(path.name)
                continue
            if path.suffix == ".sql":
                await conn.execute(content)
            else:
                for account in json.loads(content):
                    current = await conn.fetch("SELECT user_id,username FROM app_system.users WHERE user_id=$1 OR username=$2", account["user_id"], account["username"])
                    if current:
                        if len(current) != 1 or current[0]["user_id"] != account["user_id"] or current[0]["username"] != account["username"]:
                            raise RuntimeError("种子账号与已有身份冲突，拒绝猜测关联")
                        # 已迁移的同一身份保留现有密码和授权，不能被初始配置重置。
                        continue
                    password = settings.initial_admin_password if account["role"] == "admin" else settings.initial_demo_password
                    encoded = hash_password(password.get_secret_value())
                    await conn.execute("""INSERT INTO app_system.users(user_id,username,display_name,password_hash,role,
                        department_id,grade,employee_id,knowledge_enabled,business_query_enabled)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""", account["user_id"], account["username"],
                        account["display_name"], encoded, account["role"], account["department_id"], account["grade"],
                        account["employee_id"], account["knowledge_enabled"], account["business_query_enabled"])
                    created_accounts += 1
            await conn.execute("INSERT INTO app_system.seed_receipts(source_key,sha256) VALUES($1,$2)", source_key, digest)
            applied.append(path.name)
        if not applied and not skipped:
            raise RuntimeError("未找到外置种子文件")
        await AuditRecorder(None, "db-admin").append(new_id(), "database.seeded",
            {"applied": applied, "skipped": skipped, "created_accounts": created_accounts}, connection=conn)
    return {"applied": applied, "skipped": skipped, "created_accounts": created_accounts}


async def main(command: str) -> None:
    """接收 migrate 或 seed 命令；执行显式管理任务并仅打印安全结果摘要。"""
    settings = BootstrapSettings()
    conn = await connect_admin(settings)
    try:
        result = await (migrate(conn, settings) if command == "migrate" else seed(conn, settings))
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="知行显式数据库管理")
    parser.add_argument("command", choices=("migrate", "seed"))
    args = parser.parse_args()
    try:
        asyncio.run(main(args.command))
    except Exception as error:
        # 管理错误不打印 SQL、凭证或栈中的密码参数。
        print(f"数据库初始化失败：{type(error).__name__}")
        raise SystemExit(1)

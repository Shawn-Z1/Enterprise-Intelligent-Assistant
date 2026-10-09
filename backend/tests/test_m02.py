import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from app.ids import new_id, ShortId
from unittest.mock import patch

import asyncpg
from pydantic import SecretStr, ValidationError
from fastapi.testclient import TestClient

from app.audit import AuditRecorder, Usage
from app.config import Settings
from app.database import configure_connection, connect_business, create_app_pool
from app.db_admin import BootstrapSettings, connect_admin, identifier, migrate, seed
from app.passwords import verify_password


class DatabaseFoundationTests(unittest.IsolatedAsyncioTestCase):
    """每项测试使用真实 PG 空数据库，独立验证迁移、数据和权限，不修改演示主库。"""

    async def asyncSetUp(self):
        """无参数创建本测试专属空库并导入外置文件；为测试准备真实受限连接。"""
        self.bootstrap = BootstrapSettings()
        self.control = await connect_admin(self.bootstrap)
        # 测试库名称固定前缀加随机标识，清理范围仅限本测试创建的数据库。
        self.database_name = "m02_verify_" + new_id().replace("-", "").replace("_", "").lower()[:12]
        await self.control.execute(f"CREATE DATABASE {identifier(self.database_name)}")
        self.settings = self.bootstrap.model_copy(update={"postgres_db": self.database_name})
        self.admin = await connect_admin(self.settings)
        self.first_migration = await migrate(self.admin, self.settings)
        self.first_seed = await seed(self.admin, self.settings)
        self.app_settings = Settings(postgres_host=self.settings.postgres_host, postgres_port=self.settings.postgres_port,
            postgres_db=self.database_name, postgres_user=self.settings.app_db_user, postgres_password=self.settings.app_db_password,
            business_db_user=self.settings.business_db_user, business_db_password=self.settings.business_db_password,
            app_minio_access_key="unused-in-database-test", app_minio_secret_key=SecretStr("unused-in-database-test"))
        self.pool = await create_app_pool(self.app_settings)
        self.reader = await connect_business(self.app_settings)
        self.audit = AuditRecorder(self.pool, "M02-test", (self.settings.initial_admin_password.get_secret_value(),))

    async def asyncTearDown(self):
        """无参数关闭连接并清理本测试专属数据库；不删除主库或共享角色。"""
        if hasattr(self, "reader"):
            await self.reader.close()
        if hasattr(self, "pool"):
            await self.pool.close()
        if hasattr(self, "admin"):
            await self.admin.close()
        if hasattr(self, "control"):
            if not self.database_name.startswith("m02_verify_"):
                raise RuntimeError("拒绝清理非测试数据库")
            await self.control.execute(f"DROP DATABASE {identifier(self.database_name)} WITH (FORCE)")
            await self.control.close()

    async def test_empty_database_relationships_and_passwords(self):
        """验证空库迁移、五表关联及管理员/四成员 Argon2id 密码；无参数，无返回值。"""
        self.assertEqual(self.first_migration["applied"], [path.name for path in sorted((Path(self.settings.database_root)/"migrations").glob("*.sql"))])
        self.assertEqual(self.first_seed["created_accounts"], 5)
        tables = await self.reader.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema='demo_business'")
        self.assertEqual({row[0] for row in tables}, {"departments","employees","attendance_records","approval_records","tickets"})
        users = await self.admin.fetch("""SELECT u.username,u.password_hash,u.role,u.grade,u.employee_id,e.user_id
            FROM app_system.users u JOIN demo_business.employees e ON e.employee_id=u.employee_id AND e.user_id=u.user_id ORDER BY u.username""")
        self.assertEqual(len(users), 5)
        self.assertEqual({row["username"]:row["grade"] for row in users}, {"admin":10,"zhangsan":3,"lisi":5,"wangwu":6,"zhaoliu":9})
        for row in users:
            password = self.settings.initial_admin_password if row["role"] == "admin" else self.settings.initial_demo_password
            self.assertTrue(verify_password(row["password_hash"], password.get_secret_value()))
            self.assertFalse(verify_password(row["password_hash"], "wrong-password"))
        self.assertEqual(await self.reader.fetchval("SELECT count(*) FROM demo_business.attendance_records"), 25)

    async def test_repeat_import_preserves_permissions_passwords_and_history(self):
        """修改测试账号并追加原始记录后重复初始化；验证不重置账号或历史。"""
        user = str("U2")
        conversation, message, trace = new_id(), new_id(), new_id()
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE app_system.users SET grade=7,business_query_enabled=false,permission_version=2 WHERE user_id=$1", user)
            await conn.execute("INSERT INTO app_runtime.conversations(conversation_id,user_id,title) VALUES($1,$2,'验收原始会话')", conversation, user)
            await conn.execute("INSERT INTO app_runtime.messages(message_id,conversation_id,user_id,role,content) VALUES($1,$2,$3,'user','必须永久保留的原始消息')", message, conversation, user)
        await self.audit.append(trace, "test.original", {"content":"必须保留的审计正文"})
        before = dict(await self.admin.fetchrow("SELECT * FROM app_system.users WHERE user_id=$1", user))
        original = await self.audit.read_trace(trace)
        migration = await migrate(self.admin, self.settings)
        seeded = await seed(self.admin, self.settings.model_copy(update={"initial_demo_password":SecretStr("changed-password-not-applied")}))
        self.assertEqual(migration["applied"], [])
        self.assertEqual(seeded["created_accounts"], 0)
        self.assertEqual(len(seeded["skipped"]), len([path for path in (Path(self.settings.database_root)/"seeds_short_ids").iterdir() if path.suffix in (".sql",".json")]))
        self.assertEqual(dict(await self.admin.fetchrow("SELECT * FROM app_system.users WHERE user_id=$1", user)), before)
        self.assertEqual(await self.audit.read_trace(trace), original)
        self.assertEqual(await self.admin.fetchval("SELECT content FROM app_runtime.messages WHERE message_id=$1", message), "必须永久保留的原始消息")
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_system.users"), 5)

    async def test_business_identity_cannot_write_or_read_system(self):
        """真实业务连接只能读取五表；关闭默认只读开关后仍不能写入、提权或读取系统数据。"""
        self.assertEqual(await self.reader.fetchval("SHOW default_transaction_read_only"), "on")
        await self.reader.execute("SET default_transaction_read_only=off")
        forbidden = ["SELECT * FROM app_system.users", "SELECT * FROM app_runtime.messages",
                     "SELECT * FROM app_memory.snapshots", "SELECT * FROM app_audit.events",
                     "UPDATE demo_business.employees SET name=name", "DELETE FROM demo_business.tickets",
                     "CREATE TABLE demo_business.forbidden(id integer)", "CREATE TEMP TABLE forbidden(id integer)",
                     f"SET ROLE {identifier(self.settings.app_db_user)}",
                     f"SET ROLE {identifier(self.settings.app_db_user + '_owner')}"]
        for sql in forbidden:
            with self.subTest(sql=sql), self.assertRaises(asyncpg.InsufficientPrivilegeError):
                await self.reader.execute(sql)
        self.assertEqual(await self.reader.fetchval("SELECT count(*) FROM employees"), 5)

    async def test_immutable_audit_and_original_records(self):
        """通过应用身份及普通管理员 DML 验证审计与原始记录不能修改、删除或清空。"""
        trace = new_id()
        await self.audit.append(trace, "test.immutable", {"note":"保留"})
        async with self.pool.acquire() as conn:
            for table in ("app_audit.events", "app_audit.traces", "app_audit.spans"):
                for statement in (f"UPDATE {table} SET created_at=now()", f"DELETE FROM {table}", f"TRUNCATE {table}"):
                    with self.subTest(statement=statement), self.assertRaises(asyncpg.InsufficientPrivilegeError):
                        await conn.execute(statement)
            with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("ALTER TABLE app_audit.events DISABLE TRIGGER ALL")
        with self.assertRaises(asyncpg.InsufficientPrivilegeError):
            await self.admin.execute("DELETE FROM app_audit.events")
        self.assertEqual((await self.audit.read_trace(trace))[0]["raw_json"], {"note":"保留"})

    async def test_trace_spans_json_usage_and_redaction(self):
        """实际写入 trace/span 树、三种用量和原始 JSON；核验来源、空值及密钥脱敏。"""
        trace, parent, child = new_id(), new_id(), new_id()
        await self.audit.append(trace, "test.plan", {"text":"保留中文正文"}, span_id=parent)
        secret = self.settings.initial_admin_password.get_secret_value()
        await self.audit.append(trace, "test.model", {"api_key":"example-key", "nested":{"password":"example-password"},
            "text":"中文 " + secret, "token_count":13}, span_id=child, parent_span_id=parent,
            usage=Usage(source="reported",input_tokens=8,output_tokens=5,total_tokens=13), call_id="call-original")
        await self.audit.append(trace, "test.estimate", {"text":"估算正文"}, usage=Usage(source="estimated",total_tokens=9,method="test-character-count",model="test-model"))
        rows = await self.audit.read_trace(trace)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["usage_source"], "unavailable")
        self.assertIsNone(rows[0]["total_tokens"])
        self.assertEqual(rows[1]["raw_json"], {"api_key":"[REDACTED]","nested":{"password":"[REDACTED]"},"text":"中文 [REDACTED]","token_count":13})
        self.assertEqual(rows[1]["total_tokens"], 13)
        self.assertEqual(rows[2]["usage_source"], "estimated")
        self.assertEqual(await self.admin.fetchval("SELECT parent_span_id FROM app_audit.spans WHERE span_id=$1", child), parent)
        self.assertTrue(all(row["created_at"].utcoffset().total_seconds() == 8*3600 for row in rows))
        with self.assertRaises(ValidationError):
            Usage(total_tokens=0)
        with self.assertRaises(ValidationError):
            Usage(source="estimated",total_tokens=9)

    async def test_audit_failure_rolls_back_business_change(self):
        """在真实 PG 撤销审计写入后尝试业务修改；验证共用事务失败且没有副作用。"""
        user = str("U2")
        before = await self.admin.fetchval("SELECT display_name FROM app_system.users WHERE user_id=$1", user)
        await self.admin.execute(f"REVOKE INSERT ON app_audit.events FROM {identifier(self.settings.app_db_user)}")
        async with self.pool.acquire() as conn:
            with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await conn.execute("UPDATE app_system.users SET display_name='不得提交的修改' WHERE user_id=$1", user)
                    await self.audit.append(new_id(), "test.blocked", {"action":"修改账号"}, connection=conn)
        self.assertEqual(await self.admin.fetchval("SELECT display_name FROM app_system.users WHERE user_id=$1", user), before)

    async def test_modified_migration_and_seed_rejected(self):
        """用本测试临时外置文件模拟已导入内容被篡改；验证回执指纹拒绝重写。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "migrations").mkdir()
            original = Path(self.settings.database_root) / "migrations/001_foundation.sql"
            (root / "migrations/001_foundation.sql").write_text(original.read_text() + "\n-- 验收修改", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                await migrate(self.admin, self.settings.model_copy(update={"database_root":directory}))
            (root / "seeds_short_ids").mkdir()
            original = Path(self.settings.database_root) / "seeds_short_ids/001_departments.sql"
            (root / "seeds_short_ids/001_departments.sql").write_text(original.read_text() + "\n-- 验收修改", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                await seed(self.admin, self.settings.model_copy(update={"database_root":directory}))

    async def test_concurrent_audit_appends_are_unique(self):
        """并发写真实 PG 的同一 trace，验证事件无丢失且编号唯一递增。"""
        trace = new_id()
        await asyncio.gather(*(self.audit.append(trace, "test.concurrent", {"number":number}) for number in range(12)))
        rows = await self.audit.read_trace(trace)
        self.assertEqual(len(rows), 12)
        self.assertEqual(len({row["event_id"] for row in rows}), 12)
        self.assertEqual({row["raw_json"]["number"] for row in rows}, set(range(12)))

    async def test_api_fails_closed_on_real_audit_permission_failure(self):
        """把测试 API 接到真实隔离库并撤销写入权限；验证普通请求 503、存活探针 200。"""
        from app.main import app
        # 仅注入测试库配置；数据库、审计事务与 API 请求全部实际执行。
        with patch("app.main.get_settings", return_value=self.app_settings), TestClient(app) as client:
            await self.admin.execute(f"REVOKE INSERT ON app_audit.events FROM {identifier(self.settings.app_db_user)}")
            before = await self.admin.fetchval("SELECT count(*) FROM app_audit.events")
            response = client.get("/api/events")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "AUDIT_UNAVAILABLE")
            self.assertEqual(client.get("/api/health/live").status_code, 200)
            self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_audit.events"), before)


if __name__ == "__main__":
    unittest.main()

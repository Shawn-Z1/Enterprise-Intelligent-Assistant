import hashlib
import json
import unittest
from pathlib import Path
from uuid import UUID

import asyncpg

from app.db_admin import BootstrapSettings, connect_admin, grant_runtime_access, identifier, migrate, provision_roles, seed
from app.ids import new_id
from app.passwords import hash_password, verify_password


class ShortIdMigrationTests(unittest.IsolatedAsyncioTestCase):
    """构建真实旧版 UUID 数据库，验证升级、关联和历史保护，而非只验证全新空库。"""

    async def asyncSetUp(self):
        """无参数创建本测试专属数据库，保存管理连接和原始外置文件目录，无返回值。"""
        base = BootstrapSettings()
        self.control = await connect_admin(base)
        self.name = 'ids_verify_' + new_id().replace('-', '').replace('_', '').lower()[:12]
        await self.control.execute(f'CREATE DATABASE {identifier(self.name)}')
        self.settings = base.model_copy(update={'postgres_db': self.name})
        self.conn = await connect_admin(self.settings)
        self.root = Path(base.database_root)

    async def asyncTearDown(self):
        """无参数关闭连接，只删除本测试创建的独立数据库，不改主库或共享角色，无返回值。"""
        await self.conn.close()
        if not self.name.startswith('ids_verify_'):
            raise RuntimeError('拒绝清理非测试数据库')
        await self.control.execute(f'DROP DATABASE {identifier(self.name)} WITH (FORCE)')
        await self.control.close()

    async def prepare_legacy(self):
        """无参数导入原始两版迁移和 UUID 种子，并记录真实指纹回执；返回旧版用户密码哈希。"""
        async with self.conn.transaction():
            owner = await provision_roles(self.conn, self.settings)
            await self.conn.execute(f'SET LOCAL ROLE {identifier(owner)}')
            for name in ('001_foundation.sql', '002_identity.sql'):
                content = (self.root/'migrations'/name).read_text(encoding='utf-8')
                await self.conn.execute(content)
                await self.conn.execute('INSERT INTO app_system.schema_migrations(version,sha256) VALUES($1,$2)',name,hashlib.sha256(content.encode()).hexdigest())
            await self.conn.execute('RESET ROLE')
            for path in sorted((self.root/'seeds').iterdir()):
                if path.suffix not in ('.sql', '.json'):
                    continue
                content = path.read_text(encoding='utf-8')
                if path.suffix == '.sql':
                    await self.conn.execute(content)
                else:
                    for account in json.loads(content):
                        password = self.settings.initial_admin_password if account['role']=='admin' else self.settings.initial_demo_password
                        await self.conn.execute('''INSERT INTO app_system.users(user_id,username,display_name,password_hash,role,
                            department_id,grade,employee_id,knowledge_enabled,business_query_enabled) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)''',
                            UUID(account['user_id']),account['username'],account['display_name'],hash_password(password.get_secret_value()),
                            account['role'],UUID(account['department_id']),account['grade'],UUID(account['employee_id']),account['knowledge_enabled'],account['business_query_enabled'])
                await self.conn.execute('INSERT INTO app_system.seed_receipts(source_key,sha256) VALUES($1,$2)',path.name,hashlib.sha256(content.encode()).hexdigest())
        return await self.conn.fetchval("SELECT password_hash FROM app_system.users WHERE username='zhangsan'")

    async def test_upgrade_preserves_relationships_sessions_arrays_and_raw_history(self):
        """升级含真实身份、会话、消息、知识 ACL 数组和审计的旧库，验证短 ID、原文及不重置语义。"""
        password_hash = await self.prepare_legacy()
        # 固定旧 UUID 仅用于构造迁移前数据，新应用及新种子均使用短编号。
        old_user = UUID('20000000-0000-0000-0000-000000000002')
        old_department = UUID('10000000-0000-0000-0000-000000000002')
        conversation = UUID('60000000-0000-0000-0000-000000000001')
        message = UUID('60000000-0000-0000-0000-000000000002')
        trace = UUID('60000000-0000-0000-0000-000000000003')
        span = UUID('60000000-0000-0000-0000-000000000004')
        async with self.conn.transaction():
            await self.conn.execute("UPDATE app_system.users SET grade=7,business_query_enabled=false,permission_version=8 WHERE user_id=$1",old_user)
            await self.conn.execute("INSERT INTO app_system.auth_sessions(session_id,user_id,token_hash,csrf_token_hash,expires_at) VALUES($1,$2,'unchanged-token-hash','unchanged-csrf-hash',now()+interval '1 day')",trace,old_user)
            await self.conn.execute("INSERT INTO app_runtime.conversations(conversation_id,user_id,title) VALUES($1,$2,'保留会话')",conversation,old_user)
            await self.conn.execute("INSERT INTO app_runtime.messages(message_id,conversation_id,user_id,role,content) VALUES($1,$2,$3,'user','保留原始正文')",message,conversation,old_user)
            await self.conn.execute("INSERT INTO app_runtime.context_summaries(summary_id,conversation_id,version,covered_message_ids,summary_text) VALUES($1,$2,1,$3,'保留摘要')",span,conversation,[message])
            await self.conn.execute("INSERT INTO app_knowledge.applications(application_id,creator_user_id,title,original_sha256,object_key,allowed_department_ids,acl_snapshot) VALUES($1,$2,'保留知识','unchanged','unchanged',$3,'{}')",span,old_user,[old_department,old_department])
            await self.conn.execute("INSERT INTO app_audit.traces(trace_id,user_id,conversation_id,service) VALUES($1,$2,$3,'legacy-test')",trace,old_user,conversation)
            await self.conn.execute("INSERT INTO app_audit.spans(span_id,trace_id,name) VALUES($1,$2,'legacy')",span,trace)
            await self.conn.execute("INSERT INTO app_audit.events(trace_id,span_id,user_id,event_type,raw_json) VALUES($1,$2,$3,'legacy.original',$4)",trace,span,old_user,{'user_id':str(old_user),'正文':'原始审计不能改写'})
        before_event = dict(await self.conn.fetchrow("SELECT event_id,raw_json,created_at FROM app_audit.events WHERE event_type='legacy.original'"))
        receipts = [dict(row) for row in await self.conn.fetch('SELECT * FROM app_system.seed_receipts ORDER BY source_key')]
        result = await migrate(self.conn,self.settings)
        self.assertEqual(result['applied'],[path.name for path in sorted((self.root/'migrations').glob('*.sql')) if path.name not in ('001_foundation.sql','002_identity.sql')])
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM information_schema.columns WHERE table_schema IN ('app_system','app_runtime','app_knowledge','app_memory','app_audit','demo_business') AND udt_name IN ('uuid','_uuid')"),0)
        user = await self.conn.fetchrow("SELECT * FROM app_system.users WHERE username='zhangsan'")
        self.assertEqual((user['user_id'],user['department_id'],user['employee_id']),('U2','D2','E2'))
        self.assertEqual((user['grade'],user['permission_version'],user['business_query_enabled']),(7,8,False))
        self.assertEqual(user['password_hash'],password_hash)
        session = await self.conn.fetchrow("SELECT * FROM app_system.auth_sessions WHERE token_hash='unchanged-token-hash'")
        self.assertEqual((session['user_id'],session['csrf_token_hash']),('U2','unchanged-csrf-hash'))
        self.assertEqual(len(session['session_id']),16)
        self.assertEqual(await self.conn.fetchval('SELECT allowed_department_ids FROM app_knowledge.applications'),['D2','D2'])
        self.assertEqual(await self.conn.fetchval('SELECT covered_message_ids FROM app_runtime.context_summaries'),[await self.conn.fetchval('SELECT message_id FROM app_runtime.messages')])
        self.assertEqual(dict(await self.conn.fetchrow("SELECT event_id,raw_json,created_at FROM app_audit.events WHERE event_type='legacy.original'")),before_event)
        self.assertEqual([dict(row) for row in await self.conn.fetch('SELECT * FROM app_system.seed_receipts ORDER BY source_key')],receipts)
        self.assertEqual(set(await self.conn.fetchval('SELECT array_agg(attendance_id) FROM demo_business.attendance_records')),{'A'+str(number) for number in range(1,26)})
        imported = await seed(self.conn,self.settings)
        self.assertEqual(imported['created_accounts'],0)
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM demo_business.attendance_records'),25)
        self.assertEqual(await self.conn.fetchval("SELECT password_hash FROM app_system.users WHERE user_id='U2'"),password_hash)
        self.assertTrue(verify_password(password_hash,self.settings.initial_demo_password.get_secret_value()))
        self.assertEqual((await seed(self.conn,self.settings))['applied'],[])
        self.assertEqual((await migrate(self.conn,self.settings))['applied'],[])
        await grant_runtime_access(self.conn,self.settings)
        app_conn = await asyncpg.connect(host=self.settings.postgres_host,database=self.name,user=self.settings.app_db_user,password=self.settings.app_db_password.get_secret_value())
        try:
            with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                await app_conn.execute("UPDATE app_audit.events SET raw_json='{}'")
            with self.assertRaises(asyncpg.ForeignKeyViolationError):
                await app_conn.execute("INSERT INTO app_runtime.conversations(conversation_id,user_id,title) VALUES('invalid','missing','孤立记录')")
        finally:
            await app_conn.close()

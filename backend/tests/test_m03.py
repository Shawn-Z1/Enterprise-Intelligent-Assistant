import hashlib
import asyncio
import json
import os
import unittest
from io import BytesIO
from unittest.mock import patch
from app.ids import new_id, ShortId

from fastapi.testclient import TestClient
from minio import Minio

from app.auth import CSRF_COOKIE, SESSION_COOKIE, digest
from app.config import Settings
from app.db_admin import BootstrapSettings, connect_admin, identifier, migrate, seed
from app.main import app
from app.permissions import can_read_knowledge, AuthContext


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    """在真实独立 PostgreSQL 库与真实 MinIO 上验证身份、权限和资源隔离。"""

    async def asyncSetUp(self):
        """无参数创建专属测试库、迁移和导入；只替换连接配置，不替换任何业务结果。"""
        base = BootstrapSettings()
        self.control = await connect_admin(base)
        self.database_name = "m03_verify_"+new_id().replace("-", "").replace("_", "").lower()[:12]
        await self.control.execute(f"CREATE DATABASE {identifier(self.database_name)}")
        self.bootstrap = base.model_copy(update={"postgres_db":self.database_name})
        self.admin = await connect_admin(self.bootstrap)
        await migrate(self.admin,self.bootstrap)
        await seed(self.admin,self.bootstrap)
        self.settings = Settings(postgres_host=base.postgres_host,postgres_db=self.database_name,
            postgres_user=base.app_db_user,postgres_password=base.app_db_password,
            business_db_user=base.business_db_user,business_db_password=base.business_db_password,
            app_minio_access_key=os.environ["APP_MINIO_ACCESS_KEY"],app_minio_secret_key=os.environ["APP_MINIO_SECRET_KEY"])
        self.override = patch("app.main.get_settings",return_value=self.settings)
        self.override.start()
        self.client = TestClient(app)
        self.client.__enter__()
        self.objects = []
        self.s3 = Minio(self.settings.app_minio_endpoint,access_key=self.settings.app_minio_access_key,
            secret_key=self.settings.app_minio_secret_key.get_secret_value(),secure=False,region="us-east-1")

    async def asyncTearDown(self):
        """无参数关闭客户端并清理本测试专属对象和数据库；不改变主库或演示账号。"""
        self.client.__exit__(None,None,None)
        self.override.stop()
        for key in self.objects:
            self.s3.remove_object(self.settings.app_minio_bucket,key)
        await self.admin.close()
        if not self.database_name.startswith("m03_verify_"):
            raise RuntimeError("拒绝删除非测试数据库")
        await self.control.execute(f"DROP DATABASE {identifier(self.database_name)} WITH (FORCE)")
        await self.control.close()

    def login_as(self,username: str,password: str | None = None) -> dict:
        """接收演示账号及可选密码，实际登录后返回仅供测试请求使用的会话凭证。"""
        self.client.cookies.clear()
        password = password or (self.bootstrap.initial_admin_password if username=="admin" else self.bootstrap.initial_demo_password).get_secret_value()
        response = self.client.post("/api/auth/login",json={"username":username,"password":password})
        self.assertEqual(response.status_code,200,response.text)
        self.last_login_response = response
        return {"token":self.client.cookies.get(SESSION_COOKIE),"csrf":self.client.cookies.get(CSRF_COOKIE)}

    def call(self,session: dict,method: str,path: str,body=None,headers=None):
        """接收指定会话和请求参数，显式发送对应 Cookie 与 CSRF，避免不同用户混用客户端 Cookie。"""
        values = {"Cookie":f'{SESSION_COOKIE}={session["token"]}; {CSRF_COOKIE}={session["csrf"]}',"X-CSRF-Token":session["csrf"]}
        values.update(headers or {})
        return self.client.request(method,"/api"+path,json=body,headers=values)

    def new_user(self) -> tuple[dict,dict]:
        """无参数创建测试账号并实际自动登录；返回会话与当前服务端资料，不赋业务权限。"""
        self.client.cookies.clear()
        response = self.client.post("/api/auth/register",json={"username":"user_"+new_id().replace("-", "").replace("_", "")[:12],"display_name":"验收新职员","password":"M03-test-password-123"})
        self.assertEqual(response.status_code,201,response.text)
        session = {"token":self.client.cookies.get(SESSION_COOKIE),"csrf":self.client.cookies.get(CSRF_COOKIE)}
        return session,self.call(session,"GET","/auth/me").json()

    async def test_default_accounts_login_and_logout(self):
        """管理员和两个普通演示账号真实登录；校验角色、Cookie 属性、撤销及旧会话拒绝。"""
        for username,role in (("admin","admin"),("zhangsan","member"),("lisi","member")):
            session = self.login_as(username)
            session_header = next(value for value in self.last_login_response.headers.get_list("set-cookie") if value.startswith(SESSION_COOKIE+"="))
            self.assertIn("HttpOnly",session_header)
            self.assertIn("SameSite=lax",session_header)
            me = self.call(session,"GET","/auth/me")
            self.assertEqual(me.json()["role"],role)
            self.assertEqual(me.json()["username"],username)
            self.assertNotIn("csrf_token_hash",me.json())
            self.assertNotIn("password_hash",me.json())
            self.assertIn("no-store",me.headers["Cache-Control"])
            self.assertEqual(self.call(session,"POST","/auth/logout").status_code,200)
            self.assertEqual(self.call(session,"GET","/auth/me").status_code,401)
        session = self.login_as("admin")
        stored = await self.admin.fetchval("SELECT token_hash FROM app_system.auth_sessions WHERE token_hash=$1",digest(session["token"]))
        self.assertEqual(stored,digest(session["token"]))
        self.assertNotEqual(stored,session["token"])

    async def test_zero_permission_registration_and_spoofed_fields(self):
        """注册账号默认零权限；伪造角色、职级和他人 ID 的注册请求返回 422 且不创建账号。"""
        session,me = self.new_user()
        self.assertEqual(me["role"],"member")
        self.assertIsNone(me["department_id"])
        self.assertIsNone(me["grade"])
        self.assertIsNone(me["employee_id"])
        self.assertFalse(me["can_use_knowledge"])
        self.assertFalse(me["can_query_business"])
        self.assertEqual(self.call(session,"GET","/conversations").json(),[])
        for extra in ({"role":"admin"},{"grade":10},{"user_id":str(new_id())},{"department_id":"D2"}):
            response = self.client.post("/api/auth/register",json={"username":"spoof_"+new_id().replace("-", "").replace("_", "")[:8],"display_name":"不能赋权","password":"M03-test-password-123",**extra})
            self.assertEqual(response.status_code,422)
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_system.users"),6)

    async def test_permissions_before_business_actions_and_admin_spoof(self):
        """未授权 RAG/SQL 请求先拒绝，高职级成员也不能通过伪造管理员身份调用管理接口。"""
        session,me = self.new_user()
        for path in ("/tools/search_knowledge","/tools/query_business_data"):
            self.assertEqual(self.call(session,"POST",path,{"question":"查询企业信息"}).status_code,403)
            self.assertEqual(self.call(session,"POST",path,{"question":"查询","user_id":str(new_id()),"grade":10}).status_code,422)
        member = self.login_as("zhaoliu")
        response = self.call(member,"GET","/admin/users",headers={"X-Role":"admin","X-User-ID":"U1"})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.call(member,"GET","/admin/audit/status").status_code,403)
        response = self.call(member,"GET","/auth/me?user_id=U1")
        self.assertEqual(response.json()["username"],"zhaoliu")
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_runtime.tool_calls"),0)

    async def test_admin_grant_revoke_and_stale_session_revalidation(self):
        """管理员赋权和撤销后，同一旧 Cookie 立即读到最新权限，并保留变更审计快照。"""
        session,me = self.new_user()
        admin = self.login_as("admin")
        body = {"department_id":"D2","grade":3,"business_query_enabled":True,"position":"验收工程师","expected_permission_version":me["permission_version"]}
        response = self.call(admin,"PATCH",f'/admin/users/{me["user_id"]}',body)
        self.assertEqual(response.status_code,200,response.text)
        current = self.call(session,"GET","/auth/me").json()
        self.assertTrue(current["can_use_knowledge"])
        self.assertTrue(current["can_query_business"])
        self.assertEqual(current["position"],"验收工程师")
        self.assertEqual(self.call(session,"POST","/tools/query_business_data",{"question":"查询本人数据"}).status_code,501)
        self.assertEqual(self.call(admin,"PATCH",f'/admin/users/{me["user_id"]}',body).status_code,409)
        # 构造尚未执行的测试查询确认，撤权后必须失效，不能复用旧权限版本。
        conversation,run,call,approval = new_id(),new_id(),new_id(),new_id()
        target = str(me["user_id"])
        await self.admin.execute("INSERT INTO app_runtime.conversations(conversation_id,user_id,title) VALUES($1,$2,'待确认测试')",conversation,target)
        await self.admin.execute("INSERT INTO app_runtime.runs(run_id,trace_id,conversation_id,user_id) VALUES($1,$2,$3,$4)",run,new_id(),conversation,target)
        await self.admin.execute("INSERT INTO app_runtime.tool_calls(call_id,run_id,tool_name,input_json,status,idempotency_key) VALUES($1,$2,'query_business_data','{}','pending',$3)",call,run,new_id().replace("-", "").replace("_", ""))
        await self.admin.execute("INSERT INTO app_runtime.query_approvals(approval_id,user_id,run_id,call_id,query_fingerprint,permission_version,expires_at) VALUES($1,$2,$3,$4,'test-fingerprint',$5,now()+interval '30 minutes')",approval,target,run,call,current["permission_version"])
        revoke = {"department_id":None,"grade":None,"business_query_enabled":False,"expected_permission_version":current["permission_version"]}
        self.assertEqual(self.call(admin,"PATCH",f'/admin/users/{me["user_id"]}',revoke).status_code,200)
        self.assertFalse(self.call(session,"GET","/auth/me").json()["can_query_business"])
        self.assertEqual(await self.admin.fetchval("SELECT decision FROM app_runtime.query_approvals WHERE approval_id=$1",approval),"invalidated")
        self.assertEqual(self.call(session,"POST","/tools/query_business_data",{"question":"使用旧授权"}).status_code,403)
        events = await self.admin.fetch("SELECT raw_json FROM app_audit.events WHERE event_type='identity.permissions_changed' ORDER BY event_id")
        self.assertEqual(len(events),2)
        self.assertEqual(events[-1][0]["after"]["grade"],None)

    async def test_admin_mutation_fields_and_member_profile_cannot_elevate(self):
        """成员修改角色/职级或管理他人被拒；管理员也不能用参数新增管理员或职级 0。"""
        member = self.login_as("zhangsan")
        self.assertEqual(self.call(member,"PATCH","/profile",{"display_name":"张三","role":"admin","grade":10}).status_code,422)
        self.assertEqual(self.call(member,"PATCH","/admin/users/U3",{"department_id":None,"grade":None,"business_query_enabled":False}).status_code,403)
        admin = self.login_as("admin")
        base = {"department_id":"D2","grade":3,"business_query_enabled":True}
        for extra in ({"grade":0},{"role":"admin"},{"employee_id":str(new_id())}):
            self.assertEqual(self.call(admin,"PATCH","/admin/users/U2",{**base,**extra}).status_code,422)

    async def test_csrf_origin_expiry_and_disabled_accounts(self):
        """校验缺失/伪造 CSRF、跨站 Origin、过期会话和禁用账号，均不能继续操作。"""
        session = self.login_as("zhangsan")
        for headers in ({"X-CSRF-Token":""},{"X-CSRF-Token":"wrong"},{"Origin":"http://evil.invalid"}):
            self.assertEqual(self.call(session,"PATCH","/profile",{"display_name":"不可保存"},headers).status_code,403)
        await self.admin.execute("UPDATE app_system.auth_sessions SET expires_at=now()-interval '1 second' WHERE token_hash=$1",digest(session["token"]))
        self.assertEqual(self.call(session,"GET","/auth/me").status_code,401)
        session = self.login_as("lisi")
        await self.admin.execute("UPDATE app_system.users SET status='disabled' WHERE username='lisi'")
        self.assertEqual(self.call(session,"GET","/auth/me").status_code,401)

    async def test_profile_password_change_and_all_sessions_revoked(self):
        """修改本人姓名与密码，不触碰可信职级；旧会话全部撤销且新密码可真实登录。"""
        first = self.login_as("zhangsan")
        second = self.login_as("zhangsan")
        self.assertEqual(self.call(first,"PATCH","/profile",{"display_name":"张三本人"}).status_code,200)
        self.assertEqual(self.call(second,"GET","/auth/me").json()["display_name"],"张三本人")
        self.assertEqual(self.call(first,"POST","/profile/password",{"current_password":"incorrect","new_password":"M03-new-password-456"}).status_code,403)
        response = self.call(first,"POST","/profile/password",{"current_password":self.bootstrap.initial_demo_password.get_secret_value(),"new_password":"M03-new-password-456"})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.call(first,"GET","/auth/me").status_code,401)
        self.assertEqual(self.call(second,"GET","/auth/me").status_code,401)
        self.assertEqual(self.call(self.login_as("zhangsan","M03-new-password-456"),"GET","/auth/me").json()["grade"],3)

    async def test_conversation_attachment_ownership_and_real_download(self):
        """真实消息和 MinIO 附件只对本人可读，成员及管理员不能借 ID 猜测跨用户读取。"""
        owner = self.login_as("zhangsan")
        other = self.login_as("lisi")
        admin = self.login_as("admin")
        user = str("U2")
        conversation,attachment = new_id(),new_id()
        await self.admin.execute("INSERT INTO app_runtime.conversations(conversation_id,user_id,title) VALUES($1,$2,'本人测试会话')",conversation,user)
        await self.admin.execute("INSERT INTO app_runtime.messages(message_id,conversation_id,user_id,role,content) VALUES($1,$2,$3,'user','仅本人原始消息')",new_id(),conversation,user)
        payload = "仅本人附件正文".encode()
        key = "verification/m03/"+new_id().replace("-", "").replace("_", "")+".txt"
        self.s3.put_object(self.settings.app_minio_bucket,key,BytesIO(payload),len(payload))
        self.objects.append(key)
        await self.admin.execute("INSERT INTO app_runtime.attachments(attachment_id,user_id,conversation_id,bucket,object_key,mime_type,sha256) VALUES($1,$2,$3,$4,$5,'text/plain',$6)",attachment,user,conversation,self.settings.app_minio_bucket,key,hashlib.sha256(payload).hexdigest())
        self.assertEqual(self.call(owner,"GET",f"/conversations/{conversation}").json()["messages"][0]["content"],"仅本人原始消息")
        self.assertEqual(self.call(owner,"GET",f"/attachments/{attachment}").status_code,200)
        response = self.call(owner,"GET",f"/attachments/{attachment}/download")
        self.assertEqual(response.content,payload)
        self.assertIn("attachment;",response.headers["Content-Disposition"])
        for session in (other,admin):
            for path in (f"/conversations/{conversation}",f"/attachments/{attachment}",f"/attachments/{attachment}/download"):
                self.assertEqual(self.call(session,"GET",path).status_code,404)

    async def test_auth_failure_audit_contains_no_credentials(self):
        """失败登录、伪造参数和拒绝访问均有永久审计，正文不包含密码或会话凭证。"""
        response = self.client.post("/api/auth/login",json={"username":"admin","password":"private-wrong-password"})
        self.assertEqual(response.status_code,401)
        session = self.login_as("zhangsan")
        self.call(session,"GET","/admin/users")
        self.call(session,"PATCH","/profile",{"display_name":"失败","grade":10})
        rows = await self.admin.fetch("SELECT event_type,raw_json FROM app_audit.events")
        kinds = {row[0] for row in rows}
        self.assertTrue({"auth.login_failed","permissions.denied","request.validation_rejected"} <= kinds)
        raw = json.dumps([row[1] for row in rows],ensure_ascii=False)
        self.assertNotIn("private-wrong-password",raw)
        self.assertNotIn(self.bootstrap.initial_demo_password.get_secret_value(),raw)
        self.assertNotIn(session["token"],raw)
        self.assertNotIn(session["csrf"],raw)

    async def test_identity_mutation_rolls_back_when_business_audit_fails(self):
        """只拒绝权限变更事件的真实触发器故障，确保前置审计成功后业务修改仍完整回滚。"""
        admin = self.login_as("admin")
        await self.admin.execute("""CREATE FUNCTION app_audit.test_fail_identity() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.event_type='identity.permissions_changed' THEN RAISE EXCEPTION '验收故障'; END IF; RETURN NEW; END; $$;
            CREATE TRIGGER test_identity_failure BEFORE INSERT ON app_audit.events FOR EACH ROW EXECUTE FUNCTION app_audit.test_fail_identity()""")
        before = dict(await self.admin.fetchrow("SELECT grade,business_query_enabled,permission_version FROM app_system.users WHERE username='zhangsan'"))
        response = self.call(admin,"PATCH","/admin/users/U2",{"department_id":"D2","grade":8,"business_query_enabled":False})
        self.assertEqual(response.status_code,500)
        self.assertEqual(dict(await self.admin.fetchrow("SELECT grade,business_query_enabled,permission_version FROM app_system.users WHERE username='zhangsan'")),before)

    async def test_knowledge_acl_contract(self):
        """验证可信权限契约的部门与职级交集、未授权拒绝、创建者无特例及管理员角色。"""
        session = self.login_as("zhangsan")
        context = AuthContext(**{**self.call(session,"GET","/auth/me").json(),"session_id":new_id(),"csrf_token_hash":digest(session["csrf"])})
        self.assertTrue(can_read_knowledge(context,"retrievable",[],None))
        self.assertTrue(can_read_knowledge(context,"retrievable",[context.department_id],3))
        self.assertFalse(can_read_knowledge(context,"retrievable",[context.department_id],4))
        self.assertFalse(can_read_knowledge(context,"retrievable",[new_id()],1))
        self.assertFalse(can_read_knowledge(context,"deleted",[],None))
        self.assertFalse(can_read_knowledge(context.model_copy(update={"knowledge_enabled":False}),"retrievable",[],None))

    async def test_concurrent_failed_login_does_not_exhaust_audit_pool(self):
        """真实并发失败登录数超过连接池容量，仍能完成认证拒绝与审计，不互相等待。"""
        requests = [asyncio.to_thread(self.client.post,"/api/auth/login",json={"username":"missing_user","password":"incorrect"}) for _ in range(7)]
        results = await asyncio.wait_for(asyncio.gather(*requests),timeout=20)
        self.assertTrue(all(response.status_code==401 for response in results))
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_audit.events WHERE event_type='auth.login_failed'"),7)


if __name__ == "__main__":
    unittest.main()

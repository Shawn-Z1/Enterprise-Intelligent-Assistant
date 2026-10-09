import asyncio
import hashlib
import json
import os
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

import asyncpg
from fastapi.testclient import TestClient
from minio import Minio
from minio.error import S3Error
from PIL import Image

import test_m03 as identity
from app.audit import AuditRecorder
from app.config import Settings
from app.database import create_app_pool
from app.db_admin import BootstrapSettings, connect_admin, grant_runtime_access, identifier, migrate, seed
from app.ids import new_id
from app.knowledge_service import KnowledgeService
from app.knowledge_storage import KnowledgeStorage
from app.knowledge_worker import KnowledgeProcessor
from app.main import app
from app.permissions import AuthContext


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    """所有格式、对象、审核与权限使用真实 PG/MinIO/解析进程，测试结束只清理独立库和自己的原件。"""
    login_as = identity.IdentityTests.login_as
    call = identity.IdentityTests.call
    new_user = identity.IdentityTests.new_user

    async def asyncSetUp(self):
        """无参数创建专属库并迁移导入，启动真实 API 与后台处理器；不替换任何正常路径结果。"""
        base = BootstrapSettings()
        self.control = await connect_admin(base)
        self.name = 'm05_verify_'+new_id().replace('-','').replace('_','').lower()[:12]
        await self.control.execute(f'CREATE DATABASE {identifier(self.name)}')
        self.bootstrap = base.model_copy(update={'postgres_db':self.name})
        self.admin = await connect_admin(self.bootstrap)
        await migrate(self.admin,self.bootstrap)
        await seed(self.admin,self.bootstrap)
        self.settings = Settings(postgres_host=base.postgres_host,postgres_db=self.name,postgres_user=base.app_db_user,
            postgres_password=base.app_db_password,business_db_user=base.business_db_user,business_db_password=base.business_db_password,
            app_minio_access_key=os.environ['APP_MINIO_ACCESS_KEY'],app_minio_secret_key=os.environ['APP_MINIO_SECRET_KEY'])
        self.override = patch('app.main.get_settings',return_value=self.settings)
        self.override.start()
        self.client = TestClient(app)
        self.client.__enter__()
        self.pool = await create_app_pool(self.settings)
        self.storage = KnowledgeStorage(self.settings)
        self.audit = AuditRecorder(self.pool,'M05-test')
        self.processor = KnowledgeProcessor(self.pool,self.settings,self.storage,self.audit)
        self.service = KnowledgeService(self.pool,self.storage,self.audit)
        self.fixture = Path('/tests/fixtures/m05')
        self.s3 = self.storage.client

    async def asyncTearDown(self):
        """无参数关闭 API 与连接，仅清理本独立库记载的测试对象，保留主库和永久生产历史。"""
        self.client.__exit__(None,None,None)
        self.override.stop()
        for row in await self.admin.fetch('SELECT object_key FROM app_knowledge.applications'):
            self.s3.remove_object(self.settings.app_minio_bucket,row['object_key'])
        await self.pool.close()
        await self.admin.close()
        if not self.name.startswith('m05_verify_'):
            raise RuntimeError('拒绝清理非本次测试库')
        await self.control.execute(f'DROP DATABASE {identifier(self.name)} WITH (FORCE)')
        await self.control.close()

    def headers(self,session):
        """接收测试会话，构造明确 Cookie/CSRF 头，防止不同用户混用客户端全局 Cookie。"""
        return {'Cookie':f'zhixing_session={session["token"]}; zhixing_csrf={session["csrf"]}','X-CSRF-Token':session['csrf']}

    def upload(self,session,files=None,acl=None):
        """接收指定用户、真实文件字节和 ACL，实际调用 multipart API，返回逐文件结果响应。"""
        files = files or [('policy.txt',(self.fixture/'policy.txt').read_bytes())]
        acl = acl or {'allowed_department_ids':['D2'],'minimum_grade':3}
        return self.client.post('/api/knowledge/applications',headers=self.headers(session),data={'metadata':json.dumps(acl)},
            files=[('files',(name,data,'application/octet-stream')) for name,data in files])

    def submitted(self,session,files=None,acl=None):
        """接收上传参数，要求真实提交成功并返回申请列表；不把失败结果当作成功。"""
        response = self.upload(session,files,acl)
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(all(item['success'] for item in response.json()['results']),response.text)
        return [item['application'] for item in response.json()['results']]

    async def drain(self):
        """无参数用生产执行器实际处理独立库待办，直到队列清空；防止无限循环并返回处理数量。"""
        count = 0
        for _ in range(30):
            job = await self.processor.claim()
            if job:
                await self.processor.process(job)
                count += 1
            elif not await self.admin.fetchval("SELECT EXISTS(SELECT 1 FROM app_knowledge.jobs WHERE status='pending')"):
                return count
        raise AssertionError('知识队列没有结束')

    def review(self,session,entry,decision='approved',**extra):
        """接收用户、申请快照及审核决定，实际调用受限审核接口，返回 HTTP 响应。"""
        return self.call(session,'POST',f"/knowledge/applications/{entry['application_id']}/review",
            {'decision':decision,'expected_fingerprint':entry['fingerprint'],'comment':'虚构材料验收',**extra})

    async def test_four_real_formats_preview_before_review_has_no_formal_document(self):
        """四种真实原件可解析，中文 PDF 页面与 DOCX 表格顺序正确；待审核期间没有文档、切片或索引。"""
        member = self.login_as('zhangsan')
        originals = [('policy.'+kind,(self.fixture/('policy.'+kind)).read_bytes()) for kind in ('pdf','docx','txt','md')]
        entries = self.submitted(member,originals)
        self.assertTrue(all(item['status']=='pending_review' for item in entries))
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.documents'),0)
        await self.drain()
        for entry,(_,original) in zip(entries,originals):
            response = self.call(member,'GET',f"/knowledge/applications/{entry['application_id']}/preview")
            self.assertEqual(response.status_code,200,response.text)
            self.assertIn('企业演示制度',response.json()['text'])
            download = self.call(member,'GET',f"/knowledge/applications/{entry['application_id']}/download")
            self.assertEqual(download.content,original)
            self.assertIn('attachment',download.headers['content-disposition'])
            self.assertEqual(download.headers['x-content-type-options'],'nosniff')
            if entry['file_format']=='pdf':
                self.assertEqual(response.json()['page_count'],2)
                png = self.call(member,'GET',f"/knowledge/applications/{entry['application_id']}/pages/2")
                self.assertEqual(png.status_code,200,png.text[:60] if png.status_code!=200 else '')
                image = Image.open(BytesIO(png.content))
                self.assertGreater(image.width,100)
                self.assertLessEqual(image.width*image.height,2_010_000)
                image.close()
            if entry['file_format']=='docx':
                types = [item['type'] for item in response.json()['blocks']]
                self.assertEqual(types,['paragraph','paragraph','table','paragraph'])
                self.assertIn('通过前不可检索',response.json()['text'])
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.documents'),0)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.chunks'),0)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.parses'),4)

    async def test_member_approval_snapshot_pending_index_and_immutable_parse(self):
        """管理员通过后保存不可变审核和 ACL，后台只标待索引；解析/审核不能普通 DML 修改。"""
        member,admin = self.login_as('zhangsan'),self.login_as('admin')
        entry = self.submitted(member)[0]
        await self.drain()
        response = self.review(admin,entry)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['status'],'approved_pending_ingest')
        await self.drain()
        row = self.call(member,'GET',f"/knowledge/applications/{entry['application_id']}").json()
        self.assertEqual(row['status'],'pending_index')
        self.assertFalse(row['retrievable'])
        self.assertEqual(row['allowed_department_ids'],['D2'])
        document = await self.admin.fetchrow('SELECT * FROM app_knowledge.documents')
        self.assertEqual(document['acl_snapshot'],{'allowed_department_ids':['D2'],'minimum_grade':3})
        self.assertEqual(self.call(member,'GET',f"/knowledge/documents/{document['document_id']}/preview").status_code,200)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.chunks'),0)
        with self.assertRaises(asyncpg.InsufficientPrivilegeError):
            await self.admin.execute("UPDATE app_knowledge.reviews SET decision='rejected'")
        with self.assertRaises(asyncpg.InsufficientPrivilegeError):
            await self.admin.execute("UPDATE app_knowledge.parses SET content_json='{}'")

    async def test_admin_direct_no_self_review_and_duplicate_upload_is_independent(self):
        """管理员上传直接产生统一发布任务，无自我审核；同名同内容重新上传仍独立申请、对象和文档。"""
        admin = self.login_as('admin')
        first = self.submitted(admin)[0]
        second = self.submitted(admin)[0]
        self.assertNotEqual(first['application_id'],second['application_id'])
        self.assertEqual(first['status'],'approved_pending_ingest')
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.reviews'),0)
        self.assertEqual(await self.admin.fetchval('SELECT count(DISTINCT object_key) FROM app_knowledge.applications'),2)
        await self.drain()
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_knowledge.documents WHERE status='pending_index'"),2)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.chunks'),0)

    async def test_low_grade_creator_metadata_only_senior_formal_reader(self):
        """创建者主动选择高职级 ACL 也没有正文特权，管理资料可见；合法高职级同部门可读正式来源但不能管理。"""
        member,admin = self.login_as('zhangsan'),self.login_as('admin')
        entry = self.submitted(member,acl={'allowed_department_ids':['D2'],'minimum_grade':8})[0]
        self.assertFalse(entry['can_preview'])
        for endpoint in ('preview','download','pages/1'):
            self.assertEqual(self.call(member,'GET',f"/knowledge/applications/{entry['application_id']}/{endpoint}").status_code,403)
        await self.drain()
        self.assertEqual(self.review(admin,entry).status_code,200)
        await self.drain()
        document_id = await self.admin.fetchval('SELECT document_id FROM app_knowledge.documents')
        self.assertEqual(self.call(member,'GET',f'/knowledge/documents/{document_id}/preview').status_code,403)
        self.assertEqual(self.call(member,'GET',f"/knowledge/applications/{entry['application_id']}").status_code,200)
        senior = self.login_as('zhaoliu')
        self.assertEqual(self.call(senior,'GET',f'/knowledge/documents/{document_id}/preview').status_code,200)
        self.assertEqual(self.call(senior,'GET',f"/knowledge/applications/{entry['application_id']}").status_code,404)
        self.assertEqual(self.call(senior,'GET','/knowledge/applications').json(),[])
        foreign = self.login_as('lisi')
        self.assertEqual(self.call(foreign,'GET',f'/knowledge/documents/{document_id}/download').status_code,403)

    async def test_forged_role_acl_override_and_foreign_management_rejected(self):
        """普通成员不能审核或管理他人，高职级不能代替管理员；管理员不能通过审批参数修改 ACL。"""
        member,admin = self.login_as('zhangsan'),self.login_as('admin')
        entry = self.submitted(member)[0]
        await self.drain()
        self.assertEqual(self.review(member,entry).status_code,403)
        self.assertEqual(self.review(admin,entry,minimum_grade=1).status_code,422)
        self.assertEqual(await self.admin.fetchval('SELECT minimum_grade FROM app_knowledge.applications'),3)
        other = self.login_as('lisi')
        self.assertEqual(self.call(other,'GET','/knowledge/applications?scope=all').status_code,403)
        for action in ('withdraw','delete','retry'):
            self.assertEqual(self.call(other,'POST',f"/knowledge/applications/{entry['application_id']}/{action}").status_code,404)
        malformed = self.upload(member,acl={'allowed_department_ids':[],'minimum_grade':0})
        self.assertEqual(malformed.status_code,422)
        spoof = self.upload(member,acl={'allowed_department_ids':[],'creator_user_id':'U1'})
        self.assertEqual(spoof.status_code,422)

    async def test_unqualified_and_csrf_fail_before_upload(self):
        """零权限注册不能提交或获取上传目录；有身份但无 CSRF 同样在文件处理前拒绝。"""
        unqualified,_ = self.new_user()
        self.assertEqual(self.upload(unqualified).status_code,403)
        self.assertEqual(self.call(unqualified,'GET','/knowledge/options').status_code,403)
        member = self.login_as('zhangsan')
        headers = self.headers(member)
        headers.pop('X-CSRF-Token')
        response = self.client.post('/api/knowledge/applications',headers=headers,data={'metadata':'{}'},files={'files':('test.txt',b'text')})
        self.assertEqual(response.status_code,403)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.applications'),0)

    async def test_stale_review_and_changed_acl_invalidate_queued_publish(self):
        """旧页面指纹拒绝审批，已批准任务的 ACL 被改变时作废原批准，审核历史仍保持原快照。"""
        member,admin = self.login_as('zhangsan'),self.login_as('admin')
        entry = self.submitted(member)[0]
        await self.drain()
        changed = {**entry,'fingerprint':'0'*64}
        self.assertEqual(self.review(admin,changed).status_code,409)
        self.assertEqual(self.review(admin,entry).status_code,200)
        await self.admin.execute('UPDATE app_knowledge.applications SET minimum_grade=9 WHERE application_id=$1',entry['application_id'])
        await self.drain()
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_knowledge.applications'),'invalidated')
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_knowledge.documents'),'invalidated')
        self.assertEqual(await self.admin.fetchval("SELECT acl_snapshot->>'minimum_grade' FROM app_knowledge.reviews"),'3')
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.chunks'),0)

    async def test_object_changed_after_approval_invalidates_source(self):
        """真实 MinIO 原件被改写时 SHA 校验使批准失效，不让已批准任务处理新字节。"""
        member,admin = self.login_as('zhangsan'),self.login_as('admin')
        entry = self.submitted(member)[0]
        await self.drain()
        self.assertEqual(self.review(admin,entry).status_code,200)
        key = await self.admin.fetchval('SELECT object_key FROM app_knowledge.applications')
        altered = b'changed original'
        self.s3.put_object(self.settings.app_minio_bucket,key,BytesIO(altered),len(altered))
        await self.drain()
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_knowledge.applications'),'invalidated')
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_knowledge.documents'),'invalidated')

    async def test_withdraw_delete_and_late_job_never_reactivates(self):
        """待审核撤回取消任务；发布中删除立即停止读取，晚到解析不会重新激活；原审核与解析历史保留。"""
        member,admin = self.login_as('zhangsan'),self.login_as('admin')
        withdrawn = self.submitted(member)[0]
        self.assertEqual(self.call(member,'POST',f"/knowledge/applications/{withdrawn['application_id']}/withdraw").status_code,200)
        self.assertEqual(self.review(admin,withdrawn).status_code,409)
        entry = self.submitted(member)[0]
        await self.drain()
        self.assertEqual(self.review(admin,entry).status_code,200)
        job = await self.processor.claim()
        self.assertEqual(job['job_type'],'publish')
        self.assertEqual(self.call(member,'POST',f"/knowledge/applications/{entry['application_id']}/delete").status_code,200)
        self.assertEqual(self.call(admin,'GET',f"/knowledge/applications/{entry['application_id']}/download").status_code,404)
        await self.processor.process(job)
        await self.drain()
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_knowledge.applications WHERE application_id=$1',entry['application_id']),'invalidated')
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_knowledge.documents'),'invalidated')
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.reviews'),1)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.parses'),1)

    async def test_batch_independent_unsupported_empty_and_fake_files(self):
        """同批每文件独立结果，Excel、空文件、伪 PDF、路径文件名明确失败；仅合法文件形成申请。"""
        member = self.login_as('zhangsan')
        result = self.upload(member,[('good.txt',b'valid text'),('bad.xlsx',b'x'),('empty.txt',b''),('fake.pdf',b'not pdf'),('../escape.txt',b'text')])
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual([item['success'] for item in result.json()['results']],[True,False,False,False,False])
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.applications'),1)

    async def test_corrupt_docx_xml_and_binary_text_fail_without_fake_success(self):
        """支持扩展名也不伪造解析；危险 XML 与二进制文本进入明确失败，无法批准且没有正式文档。"""
        member,admin = self.login_as('zhangsan'),self.login_as('admin')
        content = BytesIO()
        with ZipFile(content,'w',ZIP_DEFLATED) as archive:
            archive.writestr('[Content_Types].xml','<Types/>')
            archive.writestr('word/document.xml','<!DOCTYPE x [<!ENTITY foo SYSTEM "file:///etc/passwd">]><x>&foo;</x>')
        entries = self.submitted(member,[('bad.docx',content.getvalue()),('binary.txt',b'hello\0world')])
        await self.drain()
        rows = await self.admin.fetch('SELECT parse_status,error_json FROM app_knowledge.applications ORDER BY filename')
        self.assertTrue(all(row['parse_status']=='failed' for row in rows))
        self.assertEqual({row['error_json']['code'] for row in rows},{'UNSAFE_XML','BINARY_TEXT'})
        self.assertEqual(self.review(admin,entries[0]).status_code,409)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.parses'),0)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.documents'),0)

    async def test_size_and_upload_field_limits(self):
        """单文件限额逐项拒绝，总量限额在保存前拒绝；额外身份字段不被静默接收。"""
        member = self.login_as('zhangsan')
        app.state.settings = self.settings.model_copy(update={'knowledge_max_file_bytes':1024,'knowledge_max_batch_bytes':10000})
        response = self.upload(member,[('small.txt',b'text'),('big.txt',b'x'*1025)])
        self.assertEqual([item['success'] for item in response.json()['results']],[True,False])
        app.state.settings = self.settings.model_copy(update={'knowledge_max_batch_bytes':1024})
        response = self.upload(member,[('too-big.txt',b'x'*70000)])
        self.assertEqual(response.status_code,413)
        response = self.client.post('/api/knowledge/applications',headers=self.headers(member),data={'metadata':'{}','user_id':'U1'},files={'files':('x.txt',b'text')})
        self.assertIn(response.status_code,(400,422))
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.applications'),1)

    async def test_audit_failure_rolls_back_and_cleans_new_minio_object(self):
        """选择性审计拒绝真实上传事务时申请与任务都回滚，本次新对象清理，其他原件不受影响。"""
        member = self.login_as('zhangsan')
        await self.admin.execute("""CREATE FUNCTION app_audit.m05_reject_submission() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.event_type='knowledge.submitted' THEN RAISE EXCEPTION '验收拒绝提交审计' USING ERRCODE='42501'; END IF; RETURN NEW; END; $$;
            CREATE TRIGGER m05_reject_submission BEFORE INSERT ON app_audit.events FOR EACH ROW EXECUTE FUNCTION app_audit.m05_reject_submission();""")
        keys = []
        original = app.state.knowledge_storage.client.put_object
        def recording(*args,**kwargs):
            """接收实际上传参数，记录本次对象键后调用真实 SDK，供检查清理结果。"""
            keys.append(args[1])
            return original(*args,**kwargs)
        with patch.object(app.state.knowledge_storage.client,'put_object',side_effect=recording):
            response = self.upload(member)
        self.assertEqual(response.status_code,200,response.text)
        self.assertFalse(response.json()['results'][0]['success'])
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.applications'),0)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.jobs'),0)
        self.assertEqual(len(keys),1)
        with self.assertRaises(S3Error) as failed:
            self.s3.stat_object(self.settings.app_minio_bucket,keys[0])
        self.assertEqual(failed.exception.code,'NoSuchKey')

    async def test_failure_retry_bounds_and_revocation(self):
        """解析失败有真实状态和最多三次显式任务；撤销知识资格后旧 Cookie 不能继续正文读取。"""
        member = self.login_as('zhangsan')
        entry = self.submitted(member,[('broken.txt',b'\0broken')])[0]
        await self.drain()
        for _ in range(2):
            self.assertEqual(self.call(member,'POST',f"/knowledge/applications/{entry['application_id']}/retry").status_code,200)
            await self.drain()
        self.assertEqual(self.call(member,'POST',f"/knowledge/applications/{entry['application_id']}/retry").status_code,409)
        good = self.submitted(member)[0]
        await self.drain()
        await self.admin.execute("UPDATE app_system.users SET knowledge_enabled=false,permission_version=permission_version+1 WHERE user_id='U2'")
        self.assertEqual(self.call(member,'GET',f"/knowledge/applications/{good['application_id']}/preview").status_code,403)
        self.assertEqual(self.call(member,'GET',f"/knowledge/applications/{good['application_id']}").status_code,200)

    async def test_parse_audit_failure_and_lease_recovery_keep_publication_closed(self):
        """正式处理审计失败不留下成功解析或待索引；租约失效明确失败，保留批准供管理员显式重试。"""
        admin = self.login_as('admin')
        entry = self.submitted(admin)[0]
        job = await self.processor.claim()
        await self.admin.execute("UPDATE app_knowledge.jobs SET lease_until=now()-interval '1 second' WHERE job_id=$1",job['job_id'])
        await self.processor.recover()
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_knowledge.documents'),'failed')
        self.assertEqual(self.call(admin,'POST',f"/knowledge/applications/{entry['application_id']}/retry").status_code,200)
        await self.admin.execute("""CREATE FUNCTION app_audit.m05_reject_parse() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.event_type='knowledge.parsed_pending_index' THEN RAISE EXCEPTION '验收拒绝解析审计' USING ERRCODE='42501'; END IF; RETURN NEW; END; $$;
            CREATE TRIGGER m05_reject_parse BEFORE INSERT ON app_audit.events FOR EACH ROW EXECUTE FUNCTION app_audit.m05_reject_parse();""")
        await self.drain()
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_knowledge.documents'),'failed')
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.parses'),0)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.chunks'),0)


if __name__=='__main__':
    unittest.main()

import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import test_m03 as identity
import test_m05 as files
from app.audit import AuditRecorder
from app.auth import digest
from app.config import Settings
from app.database import create_app_pool
from app.db_admin import BootstrapSettings, connect_admin, identifier, migrate, seed
from app.ids import new_id
from app.knowledge_indexer import KnowledgeIndexer, split_content
from app.knowledge_storage import KnowledgeStorage
from app.knowledge_worker import KnowledgeProcessor
from app.main import app
from app.rag_index import RagIndex, identifiers
from app.rag_service import RagService, live_context, rrf

# 全部制度和数值仅作为虚构验收材料，不对应真实企业政策。
TRAVEL = '企业虚构差旅管理制度 ZX-2026-A17\n2026-10-01 生效。员工出差前必须在办公系统提交差旅申请，部门负责人批准后才能预订机票。差旅报销须提供电子发票，返程后十个工作日内提交，住宿报销上限每天 650 元。'
SECURITY = '企业虚构设备安全制度 SAFE-2026-B20\n所有笔记本必须开启磁盘加密，禁止将内部资料复制到未登记的移动硬盘，遗失设备须立即通知信息安全组。'


class RagTests(unittest.IsolatedAsyncioTestCase):
    """真实 PG、MinIO、ONNX CPU 与 Milvus 双路索引；只在失败边界注入明确故障。"""
    login_as = identity.IdentityTests.login_as
    call = identity.IdentityTests.call
    new_user = identity.IdentityTests.new_user
    headers = files.KnowledgeTests.headers
    upload = files.KnowledgeTests.upload
    submitted = files.KnowledgeTests.submitted
    review = files.KnowledgeTests.review
    drain = files.KnowledgeTests.drain

    async def asyncSetUp(self):
        """无参数建立独立测试库与集合，加载真实生产服务；正常路径不替换任何模型或数据库结果。"""
        base = BootstrapSettings()
        self.control = await connect_admin(base)
        self.name = 'm06_verify_'+new_id().replace('-','').replace('_','').lower()[:12]
        await self.control.execute(f'CREATE DATABASE {identifier(self.name)}')
        self.bootstrap = base.model_copy(update={'postgres_db':self.name})
        self.admin = await connect_admin(self.bootstrap)
        await migrate(self.admin,self.bootstrap)
        await seed(self.admin,self.bootstrap)
        self.settings = Settings(postgres_host=base.postgres_host,postgres_db=self.name,postgres_user=base.app_db_user,
            postgres_password=base.app_db_password,business_db_user=base.business_db_user,business_db_password=base.business_db_password,
            app_minio_access_key=os.environ['APP_MINIO_ACCESS_KEY'],app_minio_secret_key=os.environ['APP_MINIO_SECRET_KEY'],rag_collection_prefix=self.name)
        self.override = patch('app.main.get_settings',return_value=self.settings)
        self.override.start()
        self.client = TestClient(app)
        self.client.__enter__()
        self.pool = await create_app_pool(self.settings)
        self.storage = KnowledgeStorage(self.settings)
        self.audit = AuditRecorder(self.pool,'M06-test')
        self.index = RagIndex(self.settings)
        self.rag = RagService(self.pool,self.settings,self.index,self.storage,self.audit)
        self.indexer = KnowledgeIndexer(self.pool,self.settings,self.index,self.audit)
        self.processor = KnowledgeProcessor(self.pool,self.settings,self.storage,self.audit,self.indexer)
        self.fixture = Path('/tests/fixtures/m05')

    async def asyncTearDown(self):
        """无参数关闭测试实例，清理本独立库记载的原件与前缀集合；不改动主库、真实历史或其他集合。"""
        for row in await self.admin.fetch('SELECT object_key FROM app_knowledge.applications'):
            self.storage.client.remove_object(self.settings.app_minio_bucket,row['object_key'])
        for name in self.index.client.list_collections():
            if name.startswith(self.name+'_'):
                self.index.client.drop_collection(name)
        await self.index.close()
        self.client.__exit__(None,None,None)
        self.override.stop()
        await self.pool.close()
        await self.admin.close()
        if not self.name.startswith('m06_verify_'):
            raise RuntimeError('拒绝清理非 M06 测试库')
        await self.control.execute(f'DROP DATABASE {identifier(self.name)} WITH (FORCE)')
        await self.control.close()

    async def context(self,session):
        """接收实际登录 Cookie，从 PG 查会话编号再读取当前可信身份，返回检索上下文。"""
        row = await self.admin.fetchrow('SELECT user_id,session_id FROM app_system.auth_sessions WHERE token_hash=$1',digest(session['token']))
        return await live_context(self.pool,row['user_id'],row['session_id'])

    async def indexed(self,text=TRAVEL,acl=None,member=False,filename='travel.txt'):
        """接收虚构正文、ACL 和是否成员申请，实际上传/审核/处理到可检索，返回原件与正式文档。"""
        admin = self.login_as('admin')
        session = self.login_as('zhangsan') if member else admin
        entry = self.submitted(session,[(filename,text.encode())],acl)[0]
        await self.drain()
        if member:
            self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.chunks'),0)
            response = self.review(admin,entry)
            self.assertEqual(response.status_code,200,response.text)
            await self.drain()
        row = await self.admin.fetchrow('SELECT a.*,d.document_id FROM app_knowledge.applications a JOIN app_knowledge.documents d USING(application_id) WHERE application_id=$1',entry['application_id'])
        self.assertEqual(row['status'],'retrievable',row['error_json'])
        return dict(row)

    async def search(self,session,query='企业差旅报销住宿每天上限是多少？'):
        """接收真实登录用户和检索问题，用真实双路/重排服务查询，返回来源，不注入固定召回结果。"""
        return await self.rag.search(query,await self.context(session),new_id())

    async def test_real_approval_dense_bm25_rrf_rerank_and_exact_identifiers(self):
        """成员实际获批准后才切片；真实两路、RRF 与独立重排均有证据，编号与日期结构过滤命中。"""
        entry = await self.indexed(member=True)
        await self.indexed(SECURITY,filename='security.txt')
        session = self.login_as('zhangsan')
        result = await self.search(session)
        self.assertTrue(result['citations'])
        self.assertIn('650',result['citations'][0]['content'])
        evidence = await self.admin.fetchval('SELECT evidence_json FROM app_runtime.retrievals WHERE retrieval_id=$1',result['retrieval_id'])
        self.assertTrue(all(evidence[key] for key in ('dense','bm25','rrf','rerank')))
        self.assertEqual(evidence['rrf_k'],60)
        self.assertGreater(evidence['rerank'][0]['semantic_score'],self.settings.rag_min_score)
        filtered = await self.search(session,'ZX-2026-A17 2026-10-01 差旅申请需要谁批准？')
        self.assertTrue(filtered['citations'])
        self.assertTrue(all(row['document_id']==entry['document_id'] for row in filtered['citations']))
        for statement in ("UPDATE app_runtime.retrievals SET query='修改历史'",'DELETE FROM app_runtime.retrievals','TRUNCATE app_runtime.retrievals'):
            with self.assertRaises(Exception):
                await self.admin.execute(statement)

    async def test_department_grade_admin_and_low_creator(self):
        """高职级材料的低职级创建者不能召回；其他部门不能召回，当前同部门高职级和管理员可以。"""
        entry = await self.indexed(acl={'allowed_department_ids':['D2'],'minimum_grade':8},member=True)
        for username in ('zhangsan','lisi'):
            result = await self.search(self.login_as(username))
            self.assertEqual(result['citations'],[])
            evidence = await self.admin.fetchval('SELECT evidence_json FROM app_runtime.retrievals WHERE retrieval_id=$1',result['retrieval_id'])
            self.assertEqual(evidence['dense'],[])
            self.assertEqual(evidence['bm25'],[])
        for username in ('zhaoliu','admin'):
            result = await self.search(self.login_as(username))
            self.assertEqual(result['citations'][0]['document_id'],entry['document_id'])

    async def test_zero_permission_and_spoof_rejected_before_any_model(self):
        """无资格请求在任何小模型或召回前拒绝；模型参数或 HTTP 字段不能伪造管理员权限。"""
        await self.indexed()
        session,me = self.new_user()
        with patch.object(self.index,'ensure',side_effect=AssertionError('未授权不得初始化召回')):
            with self.assertRaises(Exception) as caught:
                await self.search(session)
            self.assertEqual(caught.exception.status_code,403)
        response = self.call(session,'POST','/tools/search_knowledge',{'question':'差旅报销','role':'admin'})
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.call(session,'POST','/tools/search_knowledge',{'question':'差旅报销'}).status_code,403)

    async def test_delete_blocks_stale_index_and_preserves_archives(self):
        """产品删除立即挡住召回，清理完成后实体确实移除；人工残留旧向量仍不能进入候选或模型。"""
        entry = await self.indexed()
        session = self.login_as('admin')
        result = await self.search(session)
        chunk = dict(await self.admin.fetchrow('SELECT * FROM app_knowledge.chunks WHERE document_id=$1',entry['document_id']))
        vector = (await self.index.embed([chunk['content']]))['values']
        self.assertEqual(self.call(session,'POST',f"/knowledge/applications/{entry['application_id']}/delete").status_code,200)
        self.assertEqual((await self.search(session))['citations'],[])
        await self.drain()
        remaining = self.index.client.query(self.index.collection,filter='document_id == '+json.dumps(entry['document_id']),output_fields=['chunk_id'])
        self.assertEqual(remaining,[])
        await self.index.upsert([chunk],vector)
        stale = await self.search(session)
        evidence = await self.admin.fetchval('SELECT evidence_json FROM app_runtime.retrievals WHERE retrieval_id=$1',stale['retrieval_id'])
        self.assertEqual(stale['citations'],[])
        self.assertEqual(evidence['dense'],[])
        self.assertEqual(evidence['bm25'],[])
        self.assertEqual(self.call(session,'GET','/knowledge/sources/'+chunk['chunk_id']).status_code,404)
        await self.storage.read_verified(entry)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_knowledge.parses'),1)

    async def test_revocation_and_original_change_are_checked_again(self):
        """旧 Cookie 撤权后立即拒绝；原件被替换时不得复用批准或已有切片，不能因缓存继续回答。"""
        entry = await self.indexed()
        session = self.login_as('zhangsan')
        self.assertTrue((await self.search(session))['citations'])
        await self.admin.execute("UPDATE app_system.users SET knowledge_enabled=false,permission_version=permission_version+1 WHERE user_id='U2'")
        with self.assertRaises(Exception) as caught:
            await self.search(session)
        self.assertEqual(caught.exception.status_code,403)
        await self.storage.put(entry['object_key'],b'changed original')
        with self.assertRaises(Exception) as caught:
            await self.search(self.login_as('admin'))
        self.assertEqual(caught.exception.detail['code'],'SOURCE_CHANGED')

    async def test_index_failure_retry_is_real_and_success_not_faked(self):
        """明确注入索引写失败，状态不可检索；显式重试后使用真实小模型和 Milvus 恢复双路索引。"""
        session = self.login_as('admin')
        entry = self.submitted(session,[('travel.txt',TRAVEL.encode())])[0]
        with patch.object(self.index,'upsert',side_effect=RuntimeError('明确索引故障注入')):
            await self.drain()
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_knowledge.documents'),'failed')
        self.assertEqual((await self.search(session))['citations'],[])
        response = self.call(session,'POST',f"/knowledge/applications/{entry['application_id']}/retry")
        self.assertEqual(response.status_code,200,response.text)
        await self.drain()
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_knowledge.documents'),'retrievable')
        self.assertIn('650',(await self.search(session))['citations'][0]['content'])

    async def test_no_evidence_and_embedding_space_mismatch(self):
        """无关查询不凑五条证据；模型空间变更明确拒绝，不能用另一空间的查询向量查旧索引。"""
        await self.indexed()
        session = self.login_as('admin')
        result = await self.search(session,'火星地下海洋中独角兽的食谱是什么？')
        self.assertEqual(result['citations'],[])
        self.index.info['space']='different-space'
        with self.assertRaises(Exception) as caught:
            await self.index.embed(['差旅'])
        self.assertEqual(caught.exception.detail['code'],'EMBEDDING_SPACE_CHANGED')

    async def test_delete_failure_remains_blocked_and_explicit_retry_cleans_index(self):
        """索引删除故障保留删除中与失败任务且不能召回；显式重试清理真实实体，不恢复为可检索。"""
        entry=await self.indexed()
        session=self.login_as('admin')
        self.assertEqual(self.call(session,'POST',f"/knowledge/applications/{entry['application_id']}/delete").status_code,200)
        with patch.object(self.index,'delete_document',side_effect=RuntimeError('明确删除索引故障')):
            await self.drain()
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_knowledge.applications'),'deleting')
        self.assertEqual((await self.search(session))['citations'],[])
        self.assertEqual(await self.admin.fetchval("SELECT status FROM app_knowledge.jobs WHERE job_type='delete'"),'failed')
        response=self.call(session,'POST',f"/knowledge/applications/{entry['application_id']}/retry")
        self.assertEqual(response.status_code,200,response.text)
        await self.drain()
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_knowledge.applications'),'invalidated')
        self.assertEqual(self.index.client.query(self.index.collection,filter='document_id == '+json.dumps(entry['document_id']),output_fields=['chunk_id']),[])

    async def test_current_acl_blocks_source_preview_cross_user(self):
        """合法他人可定位正式来源但不能管理申请；提高文档 ACL 后旧引用入口及原件预览重新拒绝。"""
        entry = await self.indexed()
        session = self.login_as('zhangsan')
        result = await self.search(session)
        source = self.call(session,'GET','/knowledge/sources/'+result['citations'][0]['chunk_id'])
        self.assertEqual(source.status_code,200,source.text)
        self.assertIn('650',source.json()['content'])
        self.assertEqual(self.call(session,'GET',f"/knowledge/applications/{entry['application_id']}").status_code,404)
        self.assertEqual(self.call(session,'GET',f"/knowledge/documents/{entry['document_id']}/preview").status_code,200)
        await self.admin.execute("UPDATE app_knowledge.applications SET minimum_grade=9 WHERE application_id=$1",entry['application_id'])
        self.assertEqual((await self.search(session))['citations'],[])
        self.assertEqual(self.call(session,'GET','/knowledge/sources/'+result['citations'][0]['chunk_id']).status_code,409)


class Algorithms(unittest.TestCase):
    """确定性融合与切片契约，验证实际数学与位置，不替代真实检索验收。"""
    def test_rrf_rank_formula(self):
        """验证排名从一开始，重复切片只出现一次，分数是倒数排名之和而非两路原分数相加。"""
        rows = rrf([{'chunk_id':'C1','rank':1},{'chunk_id':'C2','rank':2}], [{'chunk_id':'C2','rank':1}],60,20)
        self.assertEqual(rows[0]['chunk_id'],'C2')
        self.assertAlmostEqual(rows[0]['rrf_score'],1/62+1/61)

    def test_chinese_pdf_chunks_pages_and_identifiers(self):
        """验证中文切片保留全部真实页码及编号日期；递归合并可以跨页，但不能丢失跨页定位。"""
        rows = split_content({'format':'pdf','pages':[{'page':1,'text':'甲'*700},{'page':2,'text':TRAVEL}]},360,50)
        self.assertEqual(rows[0]['page_number'],1)
        self.assertIn(2,rows[-1]['page_numbers'])
        self.assertEqual(len(rows[0]['content']),360)
        self.assertEqual(identifiers(TRAVEL),['2026-10-01','zx-2026-a17'])


if __name__=='__main__':
    unittest.main()

"""对已独立恢复的真实档案验收：原件/产物、混合检索、缓存重建及一次真实模型主审核链路。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import SecretStr
from app.audit import AuditRecorder
from app.auth import digest
from app.chat_service import ChatProcessor
from app.config import Settings
from app.database import create_app_pool
from app.db_admin import BootstrapSettings,connect_admin
from app.ids import new_id
from app.knowledge_storage import KnowledgeStorage
from app.long_term_memory import LongTermMemory,MemoryArguments
from app.main import app
from app.memory_index import MemoryIndex
from app.model_gateway import ModelGateway
from app.rag_index import RagIndex
from app.rag_service import RagService,live_context


async def verify(root):
    """接收备份/恢复配置目录，只操作指定独立目标；正常路径使用真实存储、CPU与主/审核模型。"""
    root=Path(root)
    base=BootstrapSettings();environment=json.loads((root/'restore.compose.json').read_text())['services']['api']['environment']
    database=environment['POSTGRES_DB']
    if database==base.postgres_db or not database.startswith('m12_restore_'):raise ValueError('验收仅允许m12_restore_隔离库')
    settings=Settings().model_copy(update={'postgres_db':database,'postgres_user':base.app_db_user,'postgres_password':base.app_db_password,
        'app_minio_bucket':environment['APP_MINIO_BUCKET'],'app_minio_access_key':environment['APP_MINIO_ACCESS_KEY'],
        'app_minio_secret_key':SecretStr(environment['APP_MINIO_SECRET_KEY']),
        'rag_collection_prefix':environment['RAG_COLLECTION_PREFIX'],'memory_collection_prefix':environment['MEMORY_COLLECTION_PREFIX'],
        'sandbox_enabled':False,'model_output_tokens':2048,'model_read_timeout_seconds':90,'run_timeout_seconds':240})
    conn=await connect_admin(base.model_copy(update={'postgres_db':database}));pool=await create_app_pool(settings)
    index=RagIndex(settings);private=MemoryIndex(settings);storage=KnowledgeStorage(settings)
    audit=AuditRecorder(pool,'M12-restore-test',tuple(value.get_secret_value() for value in (settings.primary_api_key,settings.fallback_api_key,settings.review_api_key)))
    memory=LongTermMemory(pool,settings,private,audit);gateway=ModelGateway(settings)
    evidence=[]
    try:
        with patch('app.main.get_settings',return_value=settings),TestClient(app) as api:
            def login(username):
                """接收种子账号，以新会话登录恢复库；返回Cookie/CSRF请求头，不输出凭证。"""
                api.cookies.clear();password=base.initial_admin_password if username=='admin' else base.initial_demo_password
                response=api.post('/api/auth/login',json={'username':username,'password':password.get_secret_value()});assert response.status_code==200,response.text
                return {'Cookie':'; '.join(f'{key}={value}' for key,value in api.cookies.items()),'X-CSRF-Token':api.cookies.get('zhixing_csrf')}
            async def context(headers):
                """接收真实登录头，从恢复库取得可信会话及当前权限，不伪造用户身份。"""
                token=headers['Cookie'].split('zhixing_session=')[1].split(';')[0]
                row=await conn.fetchrow('SELECT session_id,user_id FROM app_system.auth_sessions WHERE token_hash=$1',digest(token))
                return await live_context(pool,row['user_id'],row['session_id'])
            admin=login('admin')
            ready=api.get('/api/health/ready',headers=admin);assert ready.status_code==200,ready.text
            # 原件SHA不仅核对备份目录，还通过真实产品下载接口核对；预览读取已保存的不可变解析。
            doc=await conn.fetchrow("SELECT a.*,d.document_id FROM app_knowledge.applications a JOIN app_knowledge.documents d USING(application_id) WHERE a.status='retrievable' AND a.parse_status='completed' ORDER BY a.created_at LIMIT 1")
            assert doc is not None,'恢复验收需要一份实际可检索知识原件'
            response=api.get('/api/knowledge/documents/'+doc['document_id']+'/download',headers=admin);assert response.status_code==200,response.text[:200]
            assert hashlib.sha256(response.content).hexdigest()==doc['original_sha256']
            preview=api.get('/api/knowledge/documents/'+doc['document_id']+'/preview',headers=admin);assert preview.status_code==200,preview.text[:200]
            await index.ensure();rag=RagService(pool,settings,index,storage,audit)
            sample=await conn.fetchval("SELECT content FROM app_knowledge.chunks WHERE document_id=$1 AND status='active' ORDER BY position LIMIT 1",doc['document_id'])
            result=await rag.search(sample[:160],await context(admin),new_id())
            assert any(row['document_id']==doc['document_id'] for row in result['citations']),'恢复后的真实混合检索未找到原知识'
            evidence.append({'knowledge_download_sha':True,'preview':True,'hybrid_retrieval':True})
            artifact=await conn.fetchrow("""SELECT a.*,u.username FROM app_runtime.sandbox_artifacts a JOIN app_runtime.sandbox_results r USING(result_id)
                JOIN app_system.users u ON u.user_id=r.user_id WHERE u.username IN ('admin','zhangsan','lisi','zhaoliu') ORDER BY r.created_at LIMIT 1""")
            assert artifact is not None,'恢复验收需要一份真实沙箱历史产物'
            owner=login(artifact['username'])
            response=api.get('/api/sandbox/artifacts/'+artifact['artifact_id'],headers=owner);assert response.status_code==200,response.text[:200]
            assert hashlib.sha256(response.content).hexdigest()==artifact['sha256']
            evidence.append({'sandbox_artifact_download_sha':True,'media_type':response.headers['content-type']})
            personal=await conn.fetchrow("""SELECT t.*,u.username FROM app_memory.topics t JOIN app_memory.state s ON s.snapshot_id=t.snapshot_id
                JOIN app_system.users u ON u.user_id=t.user_id WHERE u.username IN ('admin','zhangsan','lisi','zhaoliu') ORDER BY t.created_at LIMIT 1""")
            assert personal is not None,'恢复验收需要一份真实项目记忆'
            owner=login(personal['username']);overview=await memory.overview(personal['user_id'])
            assert overview['snapshot_id']==personal['snapshot_id']
            output=await memory.search(MemoryArguments(query=personal['title'],topic_id=personal['topic_key']),await context(owner),new_id(),None)
            assert output['citations'],'私人混合检索恢复失败'
            evidence.append({'redis_overview_rebuilt':True,'private_hybrid_retrieval':True,'owner_filter':True})
            # 少量真实主模型/独立审核调用，验证恢复后新任务能领取并完成，而非只验证静态元数据。
            # 此处新账号没有历史/附件/偏好和知识、SQL资格，外部模型只能收到虚构计算题与固定协议。
            api.cookies.clear()
            response=api.post('/api/auth/register',json={'username':'m12_test_'+new_id().replace('-','').replace('_','').lower(),
                'display_name':'M12虚构验收账号','password':'M12-synthetic-test-only-2026'})
            assert response.status_code==201,response.text
            member={'Cookie':'; '.join(f'{key}={value}' for key,value in api.cookies.items()),'X-CSRF-Token':api.cookies.get('zhixing_csrf')}
            me=api.get('/api/auth/me',headers=member).json()
            assert not me.get('knowledge_enabled') and not me.get('business_query_enabled')
            assert (await memory.overview(me['user_id']))['snapshot_id'] is None
            response=api.post('/api/conversations',json={},headers=member);assert response.status_code==201,response.text
            conversation=response.json()['conversation_id']
            queued=api.post(f'/api/conversations/{conversation}/messages',json={'content':'计算13加29，只回复结果。','request_key':new_id()},headers=member);assert queued.status_code==202,queued.text
            processor=ChatProcessor(pool,settings,gateway,audit,worker_id='M12-restored-worker',rag=rag,long_memory=memory)
            run=await processor.claim();assert run and run['run_id']==queued.json()['run_id']
            await processor.process(run)
            state=await conn.fetchrow('SELECT status,state_json FROM app_runtime.runs WHERE run_id=$1',run['run_id']);assert state['status']=='completed',state['state_json']
            reply=await conn.fetchval("SELECT content FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",run['run_id']);assert '42' in reply
            roles=await conn.fetch('SELECT DISTINCT model_role FROM app_runtime.model_attempts WHERE trace_id=$1',run['trace_id'])
            assert {'primary','review'}<={row['model_role'] for row in roles}
            admin=login('admin');detail=api.get('/api/admin/audit/traces/'+run['trace_id'],headers=admin);assert detail.status_code==200,detail.text
            export=api.get('/api/admin/audit/traces/'+run['trace_id']+'/export',headers=admin);assert export.status_code==200 and export.json()['complete']
            evidence.append({'real_new_task':True,'trace_id':run['trace_id'],'main_and_review_roles':sorted(row['model_role'] for row in roles),'audit_export':True})
        (root/'verification.json').write_text(json.dumps({'passed':True,'database':database,'checks':evidence},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'restored_acceptance':'passed','checks':evidence},ensure_ascii=False))
    finally:
        await gateway.close();await memory.close();await private.close();await index.close();await pool.close();await conn.close()


if __name__=='__main__':asyncio.run(verify(os.environ.get('M12_BACKUP_DIR','/backup')))

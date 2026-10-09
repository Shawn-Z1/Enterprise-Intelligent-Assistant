import asyncio
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.audit import AuditRecorder, Usage
from app.auth import CSRF_COOKIE, SESSION_COOKIE
from app.chat_service import ChatProcessor
from app.config import Settings
from app.context import ContextAssembler
from app.database import create_app_pool
from app.db_admin import BootstrapSettings, connect_admin, grant_runtime_access, identifier, migrate, seed
from app.ids import new_id
from app.main import app
from app.model_gateway import GatewayError, ModelGateway, ModelSlot, usage_from_response
from app.timezones import beijing_now


def packets(content='协议测试回答',usage=True):
    """接收协议测试文本和用量开关，返回确定性 SSE 字节；仅用于故障和协议隔离测试。"""
    rows = [{'model':'fixture-model','choices':[{'delta':{'content':content},'finish_reason':None}]},
            {'model':'fixture-model','choices':[{'delta':{},'finish_reason':'stop'}]}]
    if usage:
        # 两份相同的累计用量包验证不能相加；真实厂商正路径另在 test_m04_real.py 验收。
        rows += [{'choices':[],'usage':{'prompt_tokens':5,'completion_tokens':2,'total_tokens':7}}]*2
    return ''.join('data: '+json.dumps(row,ensure_ascii=False)+'\n\n' for row in rows).encode()+b'data: [DONE]\n\n'


class SlowStream(httpx.AsyncByteStream):
    """可取消流式连接的确定性故障夹具，不用于实际模型成功路径验收。"""

    def __init__(self):
        """无参数记录连接关闭状态，供取消测试检查真实客户端的关闭行为。"""
        self.closed = False

    async def __aiter__(self):
        """无参数缓慢产出合法增量，以便中途取消；返回异步字节流。"""
        for _ in range(100):
            yield b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
            await asyncio.sleep(0.05)

    async def aclose(self):
        """无参数标记客户端已关闭连接，无返回值。"""
        self.closed = True


class ChatTests(unittest.IsolatedAsyncioTestCase):
    """真实隔离 PG 验证路由、任务、SSE 与审计；HTTP 替身仅控制故障和协议边界。"""

    async def asyncSetUp(self):
        """无参数创建本测试专属库并装载演示身份，同时建立实际 API 和后台执行连接。"""
        base = BootstrapSettings()
        self.control = await connect_admin(base)
        self.name = 'm04_verify_'+new_id().replace('-','').replace('_','').lower()[:12]
        await self.control.execute(f'CREATE DATABASE {identifier(self.name)}')
        self.bootstrap = base.model_copy(update={'postgres_db':self.name})
        self.admin = await connect_admin(self.bootstrap)
        await migrate(self.admin,self.bootstrap)
        await seed(self.admin,self.bootstrap)
        self.settings = Settings(postgres_host=base.postgres_host,postgres_db=self.name,postgres_user=base.app_db_user,
            postgres_password=base.app_db_password,business_db_user=base.business_db_user,business_db_password=base.business_db_password,
            app_minio_access_key='unused-in-chat-test',app_minio_secret_key=SecretStr('unused-in-chat-test'),
            primary_base_url='https://primary.test/v1',primary_api_key=SecretStr('fixture-primary-key'),primary_model='fixture-primary',
            fallback_base_url='https://fallback.test/v1',fallback_api_key=SecretStr('fixture-fallback-key'),fallback_model='fixture-fallback',
            model_retry_backoff_seconds=0)
        self.override = patch('app.main.get_settings',return_value=self.settings)
        self.override.start()
        self.api = TestClient(app)
        self.api.__enter__()
        self.pool = await create_app_pool(self.settings)
        self.audit = AuditRecorder(self.pool,'M04-test',('fixture-primary-key','fixture-fallback-key'))
        self.clients,self.gateways,self.calls = [],[],[]

    async def asyncTearDown(self):
        """无参数关闭本测试客户端与连接，删除明确前缀的独立库；不清理主库或审计历史。"""
        self.api.__exit__(None,None,None)
        self.override.stop()
        for gateway in self.gateways:
            await gateway.close()
        for client in self.clients:
            await client.aclose()
        await self.pool.close()
        await self.admin.close()
        if not self.name.startswith('m04_verify_'):
            raise RuntimeError('拒绝删除非测试数据库')
        await self.control.execute(f'DROP DATABASE {identifier(self.name)} WITH (FORCE)')
        await self.control.close()

    def login(self,username='zhangsan') -> dict:
        """接收演示用户名，实际登录并返回本测试 Cookie 与 CSRF 头，不输出凭证。"""
        self.api.cookies.clear()
        password = self.bootstrap.initial_admin_password if username=='admin' else self.bootstrap.initial_demo_password
        response = self.api.post('/api/auth/login',json={'username':username,'password':password.get_secret_value()})
        self.assertEqual(response.status_code,200,response.text)
        token,csrf = self.api.cookies.get(SESSION_COOKIE),self.api.cookies.get(CSRF_COOKIE)
        return {'Cookie':f'{SESSION_COOKIE}={token}; {CSRF_COOKIE}={csrf}','X-CSRF-Token':csrf}

    def queue(self,headers,content='隔离测试文字',conversation=None,key=None) -> dict:
        """接收指定用户和输入，调用真实 API 创建或使用本人会话，返回待执行任务。"""
        if conversation is None:
            response = self.api.post('/api/conversations',json={},headers=headers)
            self.assertEqual(response.status_code,201,response.text)
            conversation = response.json()['conversation_id']
        response = self.api.post(f'/api/conversations/{conversation}/messages',json={'content':content,'request_key':key or new_id()},headers=headers)
        self.assertEqual(response.status_code,202,response.text)
        return response.json()

    def processor(self,handler,settings=None) -> ChatProcessor:
        """接收故障处理器及可选配置，建立可记录请求的无重试 HTTP 客户端和真实后台执行器。"""
        async def recorded(request):
            """接收实际客户端发出的 HTTP 请求，记录次数与 JSON，然后由故障夹具返回响应。"""
            self.calls.append({'host':request.url.host,'body':json.loads(request.content)})
            result = handler(request)
            return await result if asyncio.iscoroutine(result) else result
        client = httpx.AsyncClient(transport=httpx.MockTransport(recorded))
        self.clients.append(client)
        gateway = ModelGateway(settings or self.settings,client)
        self.gateways.append(gateway)
        return ChatProcessor(self.pool,settings or self.settings,gateway,self.audit)

    async def process_one(self,processor):
        """接收执行器，真实领取一条任务并运行到终态，返回主库中的最终执行状态。"""
        run = await processor.claim()
        self.assertIsNotNone(run)
        await processor.process(run)
        return await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',run['run_id'])

    async def test_six_attempts_fail_terminal_and_audit_redacts(self):
        """主备均 503 时实际 HTTP 总计 3+3 次，最终失败，保留六次尝试且凭证不进入审计。"""
        run = self.queue(self.login())
        processor = self.processor(lambda request:httpx.Response(503,json={'error':'fixture-primary-key'}))
        final = await self.process_one(processor)
        self.assertEqual(final['status'],'failed')
        self.assertEqual([call['host'] for call in self.calls],['primary.test']*3+['fallback.test']*3)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_runtime.model_attempts'),6)
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_runtime.messages WHERE role='assistant'"),0)
        rows = await self.admin.fetch('SELECT raw_json,usage_source,total_tokens FROM app_audit.events WHERE run_id=$1',run['run_id'])
        self.assertNotIn('fixture-primary-key',json.dumps([dict(row) for row in rows],default=str))
        self.assertTrue(all(row['total_tokens'] is None for row in rows))

    async def test_credentials_and_parameters_do_not_retry(self):
        """401 与 400 各只调用一次；直接降级，不无意义消耗三次请求。"""
        self.queue(self.login())
        processor = self.processor(lambda request:httpx.Response(401 if request.url.host=='primary.test' else 400,json={'error':'denied'}))
        final = await self.process_one(processor)
        self.assertEqual(final['status'],'failed')
        self.assertEqual(len(self.calls),2)
        self.assertEqual(final['state_json']['error']['code'],'MODEL_PARAMETER_ERROR')

    async def test_partial_failure_resets_and_usage_counted_once(self):
        """响应中断后重试会发出清空临时回答事件，原始片段留审计，最终用量只计一次。"""
        headers = self.login()
        run = self.queue(headers)
        def handler(request):
            """接收请求，第一次故意中断，第二次返回完整协议，用于验证重试显示和用量去重。"""
            if len(self.calls)==1:
                return httpx.Response(200,content=b'data: {"choices":[{"delta":{"content":"discarded"},"finish_reason":null}]}\n\n')
            return httpx.Response(200,content=packets('仅保存完整回答'))
        final = await self.process_one(self.processor(handler))
        self.assertEqual(final['status'],'completed')
        self.assertEqual(await self.admin.fetchval("SELECT content FROM app_runtime.messages WHERE role='assistant'"),'仅保存完整回答')
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_runtime.run_events WHERE event_type='assistant.reset'"),2)
        self.assertEqual(await self.admin.fetchval("SELECT sum(total_tokens) FROM app_audit.events WHERE usage_source='reported'"),7)
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_audit.events WHERE usage_source='reported'"),1)
        self.assertIn('discarded',await self.admin.fetchval("SELECT response_json::text FROM app_runtime.model_attempts WHERE status='failed'"))
        replay = self.api.get(f"/api/runs/{run['run_id']}/events",headers=headers)
        self.assertEqual(replay.status_code,200)
        values = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith('data: ') and 'sequence' in line]
        self.assertEqual([value['sequence'] for value in values],list(range(1,len(values)+1)))
        self.assertTrue(all(value['timestamp'].endswith('+08:00') for value in values))
        last = self.api.get(f"/api/runs/{run['run_id']}/events",headers={**headers,'Last-Event-ID':str(values[-1]['sequence'])})
        self.assertNotIn('event: run.event',last.text)
        self.assertEqual(len(self.calls),2)

    async def test_idempotency_busy_and_owner_isolation(self):
        """重复发送同键不重复入队，改文本及并发发送拒绝；普通用户和管理员都不能读他人运行。"""
        owner = self.login()
        key = new_id()
        run = self.queue(owner,key=key)
        again = self.queue(owner,conversation=run['conversation_id'],key=key)
        self.assertEqual(again['run_id'],run['run_id'])
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_runtime.messages'),1)
        path = f"/api/conversations/{run['conversation_id']}/messages"
        self.assertEqual(self.api.post(path,json={'content':'changed','request_key':key},headers=owner).status_code,409)
        self.assertEqual(self.api.post(path,json={'content':'new','request_key':new_id()},headers=owner).status_code,409)
        for username in ('lisi','admin'):
            headers = self.login(username)
            for endpoint in (f"/api/runs/{run['run_id']}",f"/api/runs/{run['run_id']}/events",f"/api/conversations/{run['conversation_id']}"):
                self.assertEqual(self.api.get(endpoint,headers=headers).status_code,404)
            self.assertEqual(self.api.post(f"/api/runs/{run['run_id']}/cancel",headers=headers).status_code,404)
            self.assertEqual(self.api.patch(f"/api/conversations/{run['conversation_id']}",json={'title':'越权'},headers=headers).status_code,404)
            self.assertEqual(self.api.get('/api/conversations',headers=headers).json(),[])

    async def test_context_multiturn_rename_and_zero_permission_chat(self):
        """未分配业务权限的注册用户仍可文本聊天，完整成功轮次对应实际模型请求，标题持久更新。"""
        self.api.cookies.clear()
        response = self.api.post('/api/auth/register',json={'username':'zero_'+new_id().replace('-','').replace('_','').lower()[:8],'display_name':'零权限验收','password':'M04-isolation-password'})
        self.assertEqual(response.status_code,201,response.text)
        csrf,token = self.api.cookies.get(CSRF_COOKIE),self.api.cookies.get(SESSION_COOKIE)
        headers = {'Cookie':f'{SESSION_COOKIE}={token}; {CSRF_COOKIE}={csrf}','X-CSRF-Token':csrf}
        processor = self.processor(lambda request:httpx.Response(200,content=packets()))
        first = self.queue(headers,'第一轮原始用户输入')
        await self.process_one(processor)
        self.queue(headers,'第二轮原始用户输入',first['conversation_id'])
        await self.process_one(processor)
        history = self.calls[-1]['body']['messages']
        self.assertEqual([(item['role'],item['content']) for item in history[1:]],
            [('user','第一轮原始用户输入'),('assistant','协议测试回答'),('user','第二轮原始用户输入')])
        response = self.api.patch(f"/api/conversations/{first['conversation_id']}",json={'title':'已重命名的会话'},headers=headers)
        self.assertEqual(response.status_code,200)
        detail = self.api.get(f"/api/conversations/{first['conversation_id']}",headers=headers).json()
        self.assertEqual(detail['title'],'已重命名的会话')
        self.assertEqual(len(detail['messages']),4)
        self.assertTrue(all(row['created_at'].endswith('+08:00') for row in detail['messages']))
        self.assertNotIn('reasoning_content',json.dumps(detail,ensure_ascii=False))

    async def test_circuit_open_skips_and_half_open_probes(self):
        """两槽位故障后开路，下一任务不再请求；冷却后半开探测成功并恢复。"""
        headers = self.login()
        healthy = False
        settings = self.settings.model_copy(update={'model_circuit_seconds':0.08})
        def handler(request):
            """接收请求，根据测试开关返回故障或标准协议，控制半开恢复边界。"""
            return httpx.Response(200,content=packets()) if healthy else httpx.Response(503)
        processor = self.processor(handler,settings)
        self.queue(headers)
        await self.process_one(processor)
        self.assertEqual(len(self.calls),6)
        # 冷却窗口在测试中延长，确保第二次执行真实跳过 HTTP 请求。
        for circuit in processor.gateway.circuits.values():
            circuit.opened_until += 10
        self.queue(headers)
        await self.process_one(processor)
        self.assertEqual(len(self.calls),6)
        for circuit in processor.gateway.circuits.values():
            circuit.opened_until -= 10
        await asyncio.sleep(0.1)
        healthy = True
        self.queue(headers)
        final = await self.process_one(processor)
        self.assertEqual(final['status'],'completed')
        self.assertEqual(len(self.calls),7)

    async def test_fallback_rebudgets_smaller_window(self):
        """主窗口可容纳输入、备用窗口不足时，备用不发送必然超窗请求，返回明确错误。"""
        self.queue(self.login(),'长文本'+('x'*2600))
        settings = self.settings.model_copy(update={'primary_context_tokens':12000,'fallback_context_tokens':3000})
        final = await self.process_one(self.processor(lambda request:httpx.Response(503),settings))
        self.assertEqual(len(self.calls),3)
        self.assertEqual(final['state_json']['error']['code'],'CONTEXT_TOO_LARGE')

    async def test_preflight_rejects_oversized_context_without_message(self):
        """输入超过安全预算时 API 返回 413，原消息和后台任务都不产生，避免必然超窗调用。"""
        headers = self.login()
        conversation = self.api.post('/api/conversations',json={},headers=headers).json()['conversation_id']
        app.state.settings = self.settings.model_copy(update={'primary_context_tokens':1024,'model_output_tokens':256})
        response = self.api.post(f'/api/conversations/{conversation}/messages',json={'content':'x'*2000,'request_key':new_id()},headers=headers)
        self.assertEqual(response.status_code,413)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_runtime.messages'),0)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_runtime.runs'),0)

    async def test_cancel_queued_and_running_closes_stream(self):
        """待处理取消不调用模型，运行中取消关闭客户端连接并保存 cancelled 终态，不保存假完整回复。"""
        headers = self.login()
        queued = self.queue(headers)
        self.assertEqual(self.api.post(f"/api/runs/{queued['run_id']}/cancel",headers=headers).status_code,200)
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_runtime.runs WHERE run_id=$1',queued['run_id']),'cancelled')
        stream = SlowStream()
        processor = self.processor(lambda request:httpx.Response(200,stream=stream))
        active = self.queue(headers)
        run = await processor.claim()
        task = asyncio.create_task(processor.process(run))
        for _ in range(100):
            if self.calls:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.api.post(f"/api/runs/{active['run_id']}/cancel",headers=headers).status_code,200)
        await asyncio.wait_for(task,timeout=5)
        self.assertTrue(stream.closed)
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_runtime.runs WHERE run_id=$1',active['run_id']),'cancelled')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_runtime.messages WHERE role='assistant'"),0)

    async def test_audit_failure_blocks_external_request_and_reconciles(self):
        """尝试审计失败时没有 HTTP 模型请求，任务隔离为失败；恢复权限后补录终态审计。"""
        self.queue(self.login())
        processor = self.processor(lambda request:httpx.Response(200,content=packets()))
        run = await processor.claim()
        await self.admin.execute(f'REVOKE INSERT ON app_audit.events FROM {identifier(self.bootstrap.app_db_user)}')
        await processor.process(run)
        self.assertEqual(self.calls,[])
        row = await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',run['run_id'])
        self.assertEqual(row['status'],'failed')
        self.assertTrue(row['state_json']['audit_pending'])
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_runtime.model_attempts'),0)
        await grant_runtime_access(self.admin,self.bootstrap)
        await processor.recover()
        row = await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',run['run_id'])
        self.assertFalse(row['state_json']['audit_pending'])
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_audit.events WHERE event_type='run.failed'"),1)

    async def test_expired_lease_ends_without_reissuing_model(self):
        """worker 丢失租约后任务明确中断，恢复扫描不重复调用可能已发送的模型请求。"""
        self.queue(self.login())
        processor = self.processor(lambda request:httpx.Response(200,content=packets()))
        run = await processor.claim()
        await self.admin.execute("UPDATE app_runtime.runs SET lease_until=now()-interval '1 second' WHERE run_id=$1",run['run_id'])
        await processor.recover()
        self.assertEqual(self.calls,[])
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_runtime.runs WHERE run_id=$1',run['run_id']),'interrupted')

    async def test_beijing_database_connection_and_defaults(self):
        """新数据库时间默认、未标注时区的 SQL 和 Python 编解码均为北京时间，真实时刻保持正确。"""
        self.assertEqual(await self.admin.fetchval('SHOW timezone'),'Asia/Shanghai')
        now = await self.admin.fetchval('SELECT now()')
        self.assertEqual(now.utcoffset(),timedelta(hours=8))
        value = await self.admin.fetchval("SELECT '2026-10-09 12:00:00'::timestamptz")
        self.assertEqual(value.hour,12)
        self.assertEqual(await self.admin.fetchval('SELECT $1::timestamptz=$2::timestamptz',value,value.astimezone(beijing_now().tzinfo)),True)

    async def test_live_sse_delivers_all_persisted_events(self):
        """任务生成期间实际订阅 API，所有持久序号及最后终态均送达，订阅不会产生额外模型调用。"""
        headers = self.login()
        queued = self.queue(headers)
        processor = self.processor(lambda request:httpx.Response(200,content=packets()))
        response_task = asyncio.create_task(asyncio.to_thread(self.api.get,f"/api/runs/{queued['run_id']}/events",headers=headers))
        await asyncio.sleep(0.05)
        await self.process_one(processor)
        response = await asyncio.wait_for(response_task,timeout=10)
        self.assertEqual(response.status_code,200)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ') and 'sequence' in line]
        expected = await self.admin.fetchval('SELECT count(*) FROM app_runtime.run_events WHERE run_id=$1',queued['run_id'])
        self.assertEqual([item['sequence'] for item in events],list(range(1,expected+1)))
        self.assertEqual(events[-1]['type'],'run.completed')
        self.assertEqual(len(self.calls),1)

    async def test_protocol_adapters_and_usage_sources(self):
        """网关隔离供应商思考参数，原消息保留协议；reported、estimated、unavailable 的数值来源可区分。"""
        messages = [{'role':'assistant','content':'回答','_provider':'deepseek','reasoning_content':'公开返回的协议字段'}]
        deepseek = ModelSlot('primary','https://api.deepseek.com','fixture','deepseek-flash').request(messages,100)
        qwen = ModelSlot('primary','https://example.aliyuncs.com/compatible-mode/v1','fixture','qwen-plus').request(messages,100)
        self.assertIn('thinking',deepseek)
        self.assertIn('reasoning_content',deepseek['messages'][0])
        self.assertFalse(qwen['enable_thinking'])
        self.assertNotIn('thinking',qwen)
        self.assertNotIn('reasoning_content',qwen['messages'][0])
        self.assertEqual(usage_from_response({'prompt_tokens':3,'completion_tokens':2,'total_tokens':5},messages,'回答','fixture').source,'reported')
        estimated = usage_from_response(None,messages,'回答','fixture')
        self.assertEqual(estimated.source,'estimated')
        self.assertEqual(estimated.method,'utf8_bytes_upper_bound_v1')
        self.assertIsNone(Usage().total_tokens)


if __name__=='__main__':
    unittest.main()

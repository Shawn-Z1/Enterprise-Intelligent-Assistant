import json
import os
import unittest
from urllib.parse import urlsplit

import httpx

import test_m04 as fixtures
from app.audit import AuditRecorder
from app.chat_service import ChatProcessor
from app.config import Settings
from app.ids import new_id
from app.model_gateway import ModelGateway


class FailPrimaryTransport(httpx.AsyncBaseTransport):
    """主模型注入 503，备用请求经无重试实际网络发送，用于真实降级验收。"""

    def __init__(self,host: str):
        """接收主模型主机，初始化真实传输和主备请求计数，不保存或打印认证头。"""
        self.host,self.primary_calls,self.fallback_calls = host,0,0
        self.real = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self,request):
        """接收客户端请求，限定主槽位故障注入，其他请求真实发送；返回供应商实际响应。"""
        if request.url.host==self.host:
            self.primary_calls += 1
            return httpx.Response(503,json={'error':'验收注入主模型故障'})
        self.fallback_calls += 1
        return await self.real.handle_async_request(request)

    async def aclose(self):
        """无参数关闭实际 HTTP 传输连接，无返回值。"""
        await self.real.aclose()


@unittest.skipUnless(os.environ.get('RUN_REAL_MODELS')=='1','需要显式 RUN_REAL_MODELS=1 和真实主备模型配置')
class RealModelTests(unittest.IsolatedAsyncioTestCase):
    """真实 PG、实际 API 和实际厂商验证，空测试库验收后清理；不以替身成功作为正路径。"""
    asyncTearDown = fixtures.ChatTests.asyncTearDown
    login = fixtures.ChatTests.login
    queue = fixtures.ChatTests.queue
    process_one = fixtures.ChatTests.process_one

    async def asyncSetUp(self):
        """无参数复用隔离库准备，再注入已有真实三参数槽位；未配置或网络失败不标记验收成功。"""
        await fixtures.ChatTests.asyncSetUp(self)
        actual = Settings()
        if not actual.primary_api_key.get_secret_value() or not actual.fallback_api_key.get_secret_value():
            raise RuntimeError('真实模型验收需要主备两个已配置槽位')
        self.settings = self.settings.model_copy(update={name:getattr(actual,name) for name in (
            'primary_base_url','primary_api_key','primary_model','fallback_base_url','fallback_api_key','fallback_model',
            'primary_context_tokens','fallback_context_tokens')})
        self.settings = self.settings.model_copy(update={'model_output_tokens':256,'model_retry_backoff_seconds':0})
        app_secrets = tuple(getattr(self.settings,name).get_secret_value() for name in ('primary_api_key','fallback_api_key'))
        self.audit = AuditRecorder(self.pool,'M04-real',app_secrets)
        fixtures.app.state.settings = self.settings
        fixtures.app.state.audit = AuditRecorder(fixtures.app.state.pool,'M04-real-api',app_secrets)

    def real_processor(self,settings=None,client=None):
        """接收可选槽位替换和故障传输，返回调用实际模型并写完整 PG 审计的执行器。"""
        gateway = ModelGateway(settings or self.settings,client)
        self.gateways.append(gateway)
        if client:
            self.clients.append(client)
        return ChatProcessor(self.pool,settings or self.settings,gateway,self.audit)

    async def assert_real_result(self,run,expected_provider):
        """接收实际执行终态与供应商，核验原始包、正文、reported 用量及数值一致；打印安全证据摘要。"""
        self.assertEqual(run['status'],'completed',run['state_json'].get('error'))
        attempts = await self.admin.fetch('SELECT * FROM app_runtime.model_attempts WHERE trace_id=$1 ORDER BY created_at',run['trace_id'])
        successful = [row for row in attempts if row['status']=='completed']
        self.assertEqual(len(successful),1)
        response = successful[0]['response_json']
        self.assertEqual(successful[0]['provider'],expected_provider)
        self.assertGreater(len(response['chunks']),0)
        self.assertTrue(response['merged']['content'])
        row = await self.admin.fetchrow("SELECT usage_source,input_tokens,output_tokens,total_tokens FROM app_audit.events WHERE run_id=$1 AND event_type='model.response'",run['run_id'])
        self.assertEqual(row['usage_source'],'reported')
        self.assertEqual(row['total_tokens'],response['usage']['total_tokens'])
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_audit.events WHERE run_id=$1 AND usage_source='reported'",run['run_id']),1)
        print(json.dumps({'run_id':run['run_id'],'trace_id':run['trace_id'],'provider':expected_provider,'actual_model':response['actual_model'],
                         'raw_chunks':len(response['chunks']),'usage':dict(row)},ensure_ascii=False))

    async def test_primary_real_multiturn_history_and_ownership(self):
        """实际 DeepSeek 记住随机代号并在第二轮回答，实际输入匹配原消息，其他用户不能读取历史。"""
        headers = self.login()
        tag = '白鹭-'+new_id()
        first_text = '请记住这段会话的代号是 '+tag+'。这条消息只回答“已记住”。'
        first = self.queue(headers,first_text)
        processor = self.real_processor()
        first_result = await self.process_one(processor)
        await self.assert_real_result(first_result,'deepseek')
        second = self.queue(headers,'这段会话的代号是什么？请仅输出代号。',first['conversation_id'])
        second_result = await self.process_one(processor)
        await self.assert_real_result(second_result,'deepseek')
        content = await self.admin.fetchval("SELECT content FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",second['run_id'])
        self.assertIn(tag,content)
        request = await self.admin.fetchval("SELECT request_json FROM app_runtime.model_attempts WHERE trace_id=$1 AND status='completed'",second['trace_id'])
        self.assertEqual(request['messages'][1]['content'],first_text)
        self.assertEqual([item['role'] for item in request['messages']],['system','user','assistant','user'])
        self.assertEqual(self.api.patch(f"/api/conversations/{first['conversation_id']}",json={'title':'真实多轮验收'},headers=headers).status_code,200)
        detail = self.api.get(f"/api/conversations/{first['conversation_id']}",headers=headers).json()
        self.assertEqual(len(detail['messages']),4)
        self.assertEqual(detail['title'],'真实多轮验收')
        replay = self.api.get(f"/api/runs/{second['run_id']}/events",headers=headers)
        self.assertIn('event: run.event',replay.text)
        self.assertIn('assistant.delta',replay.text)
        foreign = self.login('lisi')
        self.assertEqual(self.api.get('/api/conversations',headers=foreign).json(),[])
        self.assertEqual(self.api.get(f"/api/conversations/{first['conversation_id']}",headers=foreign).status_code,404)

    async def test_three_parameter_swap_calls_real_qwen(self):
        """只替换主槽位地址、密钥、模型名，业务与数据库代码不变，实际由千问返回并完整审计。"""
        swapped = self.settings.model_copy(update={'primary_base_url':self.settings.fallback_base_url,
            'primary_api_key':self.settings.fallback_api_key,'primary_model':self.settings.fallback_model})
        self.queue(self.login(),'请只回答：北京时间使用哪个 UTC 偏移？')
        result = await self.process_one(self.real_processor(swapped))
        await self.assert_real_result(result,'qwen')
        request = await self.admin.fetchval("SELECT request_json FROM app_runtime.model_attempts WHERE status='completed'")
        self.assertEqual(request['model'],self.settings.fallback_model)
        self.assertFalse(request['enable_thinking'])
        self.assertNotIn('thinking',request)

    async def test_injected_primary_failure_real_fallback(self):
        """主槽位三次确定性 503 后，实际调用千问一次完成回答；故障替身不提供任何成功文本。"""
        transport = FailPrimaryTransport(urlsplit(self.settings.primary_base_url).hostname)
        client = httpx.AsyncClient(transport=transport,timeout=45,trust_env=False)
        self.queue(self.login(),'用一句简短中文说明为什么工作记录应该写明时区。')
        result = await self.process_one(self.real_processor(client=client))
        await self.assert_real_result(result,'qwen')
        self.assertEqual(transport.primary_calls,3)
        self.assertEqual(transport.fallback_calls,1)
        self.assertEqual(result['state_json']['slot'],'fallback')
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_runtime.model_attempts WHERE status='failed'"),3)


if __name__=='__main__':
    unittest.main()

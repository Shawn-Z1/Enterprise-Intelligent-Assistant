import json
import unittest

import test_m06 as hybrid
from app.audit import AuditRecorder
from app.chat_service import ChatProcessor
from app.config import Settings
from app.ids import new_id
from app.model_gateway import ModelGateway


class RealAgent(unittest.IsolatedAsyncioTestCase):
    """完整门槛使用实际 DeepSeek/千问、真实 ONNX/Milvus、真实 LangGraph，不替换正路径响应。"""
    login_as=hybrid.RagTests.login_as
    call=hybrid.RagTests.call
    new_user=hybrid.RagTests.new_user
    headers=hybrid.RagTests.headers
    upload=hybrid.RagTests.upload
    submitted=hybrid.RagTests.submitted
    review=hybrid.RagTests.review
    drain=hybrid.RagTests.drain
    context=hybrid.RagTests.context
    indexed=hybrid.RagTests.indexed
    search=hybrid.RagTests.search

    async def asyncSetUp(self):
        """无参数创建隔离数据和集合，取得本机真实主备槽位，仅将其注入已隔离的运行配置。"""
        await hybrid.RagTests.asyncSetUp(self)
        actual=Settings()
        self.settings=self.settings.model_copy(update={name:getattr(actual,name) for name in ('primary_base_url','primary_api_key','primary_model',
            'fallback_base_url','fallback_api_key','fallback_model','primary_context_tokens','fallback_context_tokens')})
        self.settings=self.settings.model_copy(update={'model_output_tokens':512,'model_retry_backoff_seconds':0})
        self.gateways=[]
        self.audit=AuditRecorder(self.pool,'M06-real',(actual.primary_api_key.get_secret_value(),actual.fallback_api_key.get_secret_value()))
        self.trace_evidence=[]

    async def asyncTearDown(self):
        """无参数关闭实际厂商连接后清理本独立验收资源；真实调用与门槛摘要仅输出非敏感证据。"""
        for gateway in self.gateways:
            await gateway.close()
        print('真实 Agent 验收证据：'+json.dumps(self.trace_evidence,ensure_ascii=False))
        await hybrid.RagTests.asyncTearDown(self)

    async def chat(self,session,question,settings=None):
        """接收实际登录用户及问题，以生产 API 创建任务并用真实网关和 LangGraph 执行，返回最终回复和任务。"""
        response=self.call(session,'POST','/conversations',{})
        self.assertEqual(response.status_code,201,response.text)
        conversation=response.json()['conversation_id']
        queued=self.call(session,'POST',f'/conversations/{conversation}/messages',{'content':question,'request_key':new_id()})
        self.assertEqual(queued.status_code,202,queued.text)
        gateway=ModelGateway(settings or self.settings)
        self.gateways.append(gateway)
        processor=ChatProcessor(self.pool,settings or self.settings,gateway,self.audit,rag=self.rag)
        run=await processor.claim()
        self.assertIsNotNone(run)
        await processor.process(run)
        state=await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',run['run_id'])
        self.assertEqual(state['status'],'completed',state['state_json'])
        reply=await self.admin.fetchrow("SELECT content,protocol_json FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",run['run_id'])
        tools=await self.admin.fetch('SELECT * FROM app_runtime.tool_calls WHERE run_id=$1',run['run_id'])
        attempts=await self.admin.fetch('SELECT provider,status,response_json,request_json FROM app_runtime.model_attempts WHERE trace_id=$1',run['trace_id'])
        self.trace_evidence.append({'trace_id':run['trace_id'],'run_id':run['run_id'],'providers':list({row['provider'] for row in attempts}),
            'model_attempts':len(attempts),'real_tool_calls':len(tools),'citations':len(reply['protocol_json']['message'].get('citations',[]))})
        return run,reply,tools,attempts

    async def test_primary_autonomous_rag_and_ordinary_direct_answer(self):
        """主模型自主查询企业制度并有依据回答；普通数学问题由模型直接回答，无工具调用。"""
        await self.indexed(member=True)
        session=self.login_as('zhangsan')
        run,reply,tools,attempts=await self.chat(session,'我们公司的差旅住宿每天最多能报销多少钱？')
        self.assertTrue(tools)
        self.assertTrue(any(row['response_json']['merged'].get('tool_calls') for row in attempts))
        self.assertTrue(any(any(message['role']=='tool' for message in row['request_json']['messages']) for row in attempts))
        self.assertIn('650',reply['content'])
        self.assertIn('[1]',reply['content'])
        self.assertTrue(reply['protocol_json']['message']['citations'])
        self.assertTrue(all(row['status']=='completed' for row in tools))
        run,reply,tools,attempts=await self.chat(session,'计算 13 加 29 的结果，用一句中文回答。')
        self.assertIn('42',reply['content'])
        self.assertEqual(len(tools),0)

    async def test_fallback_qwen_actual_tool_loop(self):
        """主槽位配置拒绝后，实际千问也能自主工具调用并继续带引用回答；工具观察不丢失。"""
        await self.indexed()
        settings=self.settings.model_copy(update={'primary_base_url':'invalid'})
        run,reply,tools,attempts=await self.chat(self.login_as('zhangsan'),'本企业正式差旅制度规定住宿报销每天上限多少？',settings)
        self.assertTrue(tools)
        self.assertIn('650',reply['content'])
        self.assertTrue(reply['protocol_json']['message']['citations'])
        self.assertTrue(all(row['provider']=='qwen' for row in attempts))

    async def test_real_model_no_evidence_and_no_permission(self):
        """实际模型面对不存在的制度不得编造；零权限账号请求企业事实得到开通提示，召回前拒绝。"""
        await self.indexed()
        run,reply,tools,attempts=await self.chat(self.login_as('zhangsan'),'本企业制度 ZX-2099-Z99 规定火星基地差旅每晚报销上限多少？')
        self.assertTrue(tools)
        self.assertFalse(reply['protocol_json']['message']['citations'])
        self.assertIn('无法确定',reply['content'])
        session,me=self.new_user()
        run,reply,tools,attempts=await self.chat(session,'我们公司的差旅住宿报销每天上限是多少？')
        self.assertTrue(tools)
        self.assertTrue(all(row['status']=='denied' for row in tools))
        self.assertIn('联系管理员',reply['content'])
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_runtime.retrievals WHERE run_id=$1',run['run_id']),0)


if __name__=='__main__':
    unittest.main()

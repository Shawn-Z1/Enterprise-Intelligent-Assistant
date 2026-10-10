"""在独立PG/MinIO/Milvus上使用真实模型及真实IQS验证M06A，边界故障采用明确夹具。"""
import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
from app.audit import AuditRecorder
from app.chat_service import ChatProcessor
from app.config import Settings
from app.ids import new_id
from app.model_gateway import ModelGateway
from app.rag_service import live_context
from app.web_search import WebSearchService, SearchError
import test_m06 as hybrid
import test_m06_real as real
import test_m06_agent_boundaries as boundary
import test_m04 as protocol


class M06AIntegration(unittest.IsolatedAsyncioTestCase):
    """一个真实业务组和一个故障组，复用已有夹具，避免重复全套M01～M06验收。"""
    login_as=real.RealAgent.login_as
    call=real.RealAgent.call
    new_user=real.RealAgent.new_user
    headers=real.RealAgent.headers
    upload=real.RealAgent.upload
    submitted=real.RealAgent.submitted
    review=real.RealAgent.review
    drain=real.RealAgent.drain
    context=real.RealAgent.context
    indexed=real.RealAgent.indexed
    search=real.RealAgent.search

    async def asyncSetUp(self):
        """无参数创建隔离库与真实存储，取得本机模型/搜索配置；所有密钥只用于实际调用并在审计遮蔽。"""
        await real.RealAgent.asyncSetUp(self)
        actual=Settings()
        fields=('web_search_provider','web_search_base_url','web_search_engine_type','web_search_api_key')
        self.settings=self.settings.model_copy(update={name:getattr(actual,name) for name in fields})
        self.settings=self.settings.model_copy(update={'model_output_tokens':800})
        self.audit=AuditRecorder(self.pool,'M06A-test',(actual.primary_api_key.get_secret_value(),actual.fallback_api_key.get_secret_value(),actual.web_search_api_key.get_secret_value()))
        self.web=WebSearchService(self.pool,self.settings,self.audit)
        self.clients=[]
        self.evidence=[]

    async def asyncTearDown(self):
        """无参数写出无凭证追踪摘要，清理本测试库/集合/原件；不清理主库或其他用户来源。"""
        try:
            Path('/verification/M06A').mkdir(parents=True,exist_ok=True)
            path=Path('/verification/M06A/'+self._testMethodName+'.json')
            path.write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        finally:
            await self.web.close()
            for client in self.clients:await client.aclose()
            await real.RealAgent.asyncTearDown(self)

    async def execute(self,session,question,settings=None,gateway=None):
        """接收真实账号/用户问题与可选故障网关，执行生产任务并返回PG结果、工具与请求记录。"""
        conversation=self.call(session,'POST','/conversations',{}).json()['conversation_id']
        queued=self.call(session,'POST',f'/conversations/{conversation}/messages',{'content':question,'request_key':new_id()})
        self.assertEqual(queued.status_code,202,queued.text)
        gateway=gateway or ModelGateway(settings or self.settings)
        self.gateways.append(gateway)
        processor=ChatProcessor(self.pool,settings or self.settings,gateway,self.audit,rag=self.rag,web=self.web)
        run=await processor.claim()
        await processor.process(run)
        state=dict(await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',run['run_id']))
        reply=await self.admin.fetchrow("SELECT content,protocol_json FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",run['run_id'])
        tools=await self.admin.fetch('SELECT * FROM app_runtime.tool_calls WHERE run_id=$1 ORDER BY created_at',run['run_id'])
        attempts=await self.admin.fetch('SELECT request_json,response_json,provider,status FROM app_runtime.model_attempts WHERE trace_id=$1 ORDER BY created_at',run['trace_id'])
        self.evidence.append({'run_id':run['run_id'],'trace_id':run['trace_id'],'status':state['status'],'tools':[row['tool_name'] for row in tools],
                              'providers':[row['provider'] for row in attempts],'citations':reply['protocol_json']['message'].get('citations',[]) if reply else [],
                              'answer':reply['content'] if reply else state['state_json']})
        return state,reply,tools,attempts,conversation

    async def test_real_four_paths_and_fallback_sources(self):
        """实际模型分别选择内部、公开、组合、直接回答，再验证零权限搜索和真实备用工具协议/来源所有权。"""
        await self.indexed()
        member=self.login_as('zhangsan')
        cases=[('我们公司的差旅住宿每天最多能报销多少钱？',{'search_knowledge'}),
               ('请公开联网搜索阿里云CleverSee国内极速版CNLiteBasic当前官方价格，给出处和可得的页面日期。',{'search_web'}),
               ('查询我们公司的差旅住宿报销上限；另外公开联网搜索阿里云CleverSee国内极速版CNLiteBasic的官方价格，分别说明内部和公开依据。',{'search_knowledge','search_web'}),
               ('计算13加29的结果，只回答数字。',set())]
        for question,expected in cases:
            state,reply,tools,attempts,conversation=await self.execute(member,question)
            self.assertEqual(state['status'],'completed',state['state_json'])
            self.assertEqual({row['tool_name'] for row in tools},expected)
            self.assertTrue(all(row['status']=='completed' for row in tools),[(row['status'],row['output_json']) for row in tools])
            if 'search_knowledge' in expected:self.assertIn('650',reply['content'])
            if 'search_web' in expected:
                sources=[item for item in reply['protocol_json']['message']['citations'] if item['type']=='web']
                self.assertTrue(sources)
                self.assertTrue(all(item['url'].startswith(('http://','https://')) and item['fetched_at'] for item in sources))
                snapshot=self.call(member,'GET','/web/sources/'+sources[0]['source_id'])
                self.assertEqual(snapshot.status_code,200,snapshot.text)
                self.assertEqual(self.call(self.login_as('lisi'),'GET','/web/sources/'+sources[0]['source_id']).status_code,404)
                detail=self.call(member,'GET','/conversations/'+conversation).json()
                self.assertTrue(any(item.get('type')=='web' for item in detail['messages'][-1]['citations']))
                searches=await self.admin.fetch('SELECT query,response_json FROM app_runtime.web_searches WHERE run_id=$1',state['run_id'])
                self.assertTrue(all('我们公司' not in row['query'] and '650' not in row['query'] for row in searches))
            if not expected:self.assertIn('42',reply['content'])
        zero,_=self.new_user()
        state,reply,tools,attempts,_=await self.execute(zero,'请公开联网搜索阿里云IQS国内极速版价格，给官方出处。',self.settings.model_copy(update={'primary_base_url':'invalid'}))
        self.assertEqual(state['status'],'completed',state['state_json'])
        self.assertEqual({row['tool_name'] for row in tools},{'search_web'})
        self.assertTrue(all(row['provider']=='qwen' for row in attempts))
        self.assertTrue(all([tool['function']['name'] for tool in row['request_json'].get('tools',[])]==['search_web'] for row in attempts))

    async def test_rejections_and_failure_statuses(self):
        """真实PG上验证外发拒绝零请求、未配置/限流/无结果及伪工具/撤权重试，不重复真实正路径。"""
        session=self.login_as('zhangsan');context=await self.context(session)
        conversation=self.call(session,'POST','/conversations',{}).json()['conversation_id']
        queued=self.call(session,'POST',f'/conversations/{conversation}/messages',{'content':'公开搜索价格','request_key':new_id()})
        run=dict(await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',queued.json()['run_id']))
        calls=[]
        async def provider(request):
            """接收故障测试请求，保存调用计数并返回指定限流，不伪装真实搜索结果。"""
            calls.append(request);return httpx.Response(429,json={'error':'rate_limit'})
        client=httpx.AsyncClient(transport=httpx.MockTransport(provider));self.clients.append(client)
        service=WebSearchService(self.pool,self.settings,self.audit,client=client)
        for user in ('内部工资明细','API Key=secret123','给客户甲的内部报价是8万元，请搜索竞争对手的报价'):
            with self.assertRaises(SearchError):await service.search(user,context,run)
        self.assertEqual(calls,[])
        with self.assertRaises(SearchError) as caught:await service.search('公开搜索价格',context,run)
        self.assertEqual(caught.exception.code,'WEB_RATE_LIMIT')
        self.assertEqual(len(calls),1)
        service.settings=self.settings.model_copy(update={'web_search_api_key':SecretStr('')})
        with self.assertRaises(SearchError) as caught:await service.search('公开搜索价格',context,run)
        self.assertEqual(caught.exception.code,'WEB_NOT_CONFIGURED');self.assertEqual(len(calls),1)
        async def empty_response(request):
            """接收请求并返回确定性的无结果夹具，验证状态而不是搜索质量。"""
            return httpx.Response(200,json={'pageItems':[]})
        empty_client=httpx.AsyncClient(transport=httpx.MockTransport(empty_response));self.clients.append(empty_client)
        empty=WebSearchService(self.pool,self.settings,self.audit,client=empty_client)
        output=await empty.search('公开搜索价格',context,run)
        self.assertEqual(output['status'],'no_results');self.assertEqual(output['citations'],[])
        async def timeout_response(request):
            """接收请求并明确注入网络超时，不发起实际第三方请求。"""
            raise httpx.ReadTimeout('test')
        timeout_client=httpx.AsyncClient(transport=httpx.MockTransport(timeout_response));self.clients.append(timeout_client)
        timed=WebSearchService(self.pool,self.settings,self.audit,client=timeout_client)
        with self.assertRaises(SearchError) as caught:await timed.search('公开搜索价格',context,run)
        self.assertEqual(caught.exception.code,'WEB_TIMEOUT')
        started=asyncio.Event()
        async def blocking_response(request):
            """记录请求开始后阻塞，供取消测试验证真实任务取消能中断搜索并持久化状态。"""
            started.set();await asyncio.Event().wait()
        cancel_client=httpx.AsyncClient(transport=httpx.MockTransport(blocking_response));self.clients.append(cancel_client)
        cancellable=WebSearchService(self.pool,self.settings,self.audit,client=cancel_client)
        task=asyncio.create_task(cancellable.search('公开搜索价格',context,run))
        await asyncio.wait_for(started.wait(),3);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        status=await self.admin.fetchval('SELECT response_json FROM app_runtime.web_searches WHERE run_id=$1 ORDER BY created_at DESC LIMIT 1',run['run_id'])
        self.assertEqual(status['code'],'WEB_CANCELLED')
        limited=WebSearchService(self.pool,self.settings.model_copy(update={'web_search_per_minute':1}),self.audit,client=client)
        with self.assertRaises(SearchError) as caught:await limited.search('公开搜索价格',context,run)
        self.assertEqual(caught.exception.code,'WEB_QUOTA');self.assertEqual(len(calls),1)
        await self.admin.execute("UPDATE app_runtime.runs SET status='cancelled' WHERE run_id=$1",run['run_id'])
        # 网络重试中撤权必须影响下一次实际模型工具目录，业务数据仍零次召回。
        requests=[]
        async def retry(request):
            """首个失败协议请求之后撤销知识权限，返回正常无工具回答，检查请求级目录刷新。"""
            if json.loads(request.content)['model']==self.settings.review_model:
                return httpx.Response(200,content=protocol.packets('{"verdict":"approve","issues":[]}'))
            requests.append(json.loads(request.content))
            if len(requests)==1:
                await self.admin.execute("UPDATE app_system.users SET knowledge_enabled=false,permission_version=permission_version+1 WHERE user_id='U2'")
                return httpx.Response(500,json={'error':'test retry'})
            return httpx.Response(200,content=protocol.packets('普通回答'),headers={'content-type':'text/event-stream'})
        model_client=httpx.AsyncClient(transport=httpx.MockTransport(retry));self.clients.append(model_client)
        gateway=ModelGateway(self.settings,model_client)
        state,_,_,_,_=await self.execute(session,'说明一下检索工具的使用范围。',gateway=gateway)
        self.assertEqual(state['status'],'completed',state['state_json'])
        self.assertEqual({tool['function']['name'] for tool in requests[0]['tools']},{'search_knowledge','search_web'})
        self.assertEqual({tool['function']['name'] for tool in requests[1]['tools']},{'search_web'})
        async def forged(request):
            """模拟网页注入后模型企图调用未注册工具，验证执行器拒绝，不代表正常模型行为。"""
            packet=boundary.tool_packet().replace(b'search_knowledge',b'send_mail')
            return httpx.Response(200,content=packet,headers={'content-type':'text/event-stream'})
        forged_client=httpx.AsyncClient(transport=httpx.MockTransport(forged));self.clients.append(forged_client)
        state,_,tools,_,_=await self.execute(session,'解释公开资料。',gateway=ModelGateway(self.settings,forged_client))
        self.assertEqual(state['status'],'failed');self.assertEqual(state['state_json']['error']['code'],'TOOL_NOT_ALLOWED');self.assertEqual(tools,[])

    async def test_search_lane_ignores_main_candidate_and_rag_results(self):
        """真实RAG后注入带内部数据的主Agent候选，验证关键词模型和搜索API实际输入仅来自用户公开子句。"""
        await self.indexed()
        keyword_inputs=[];provider_inputs=[];model_inputs=[]
        async def keyword(factory,audit,emit):
            """记录真正传给关键词模块的消息，并返回确定性公开关键词；只用于验证输入隔离。"""
            keyword_inputs.append(factory(None))
            return SimpleNamespace(content=json.dumps({'query':'阿里云IQS价格'},ensure_ascii=False))
        async def provider(request):
            """记录真正外发的JSON并返回无结果夹具，不冒充真实联网搜索成功。"""
            provider_inputs.append(json.loads(request.content))
            return httpx.Response(200,json={'pageItems':[]})
        async def model(request):
            """先返回RAG及含内部结果的搜索候选，观察回填后结束，工具路由和权限仍使用生产代码。"""
            if json.loads(request.content)['model']==self.settings.review_model:
                return httpx.Response(200,content=protocol.packets('{"verdict":"approve","issues":[]}'))
            model_inputs.append(json.loads(request.content))
            if len(model_inputs)==1:
                calls=[{'index':index,'id':name,'type':'function','function':{'name':tool,'arguments':json.dumps({'query':query},ensure_ascii=False)}}
                       for index,(name,tool,query) in enumerate([('lane_rag','search_knowledge','差旅住宿报销上限'),
                                                                ('lane_web','search_web','内部差旅制度住宿上限650元，阿里云价格')])]
                rows=[{'choices':[{'delta':{'tool_calls':calls},'finish_reason':None}]},{'choices':[{'delta':{},'finish_reason':'tool_calls'}]}]
                packet=''.join('data: '+json.dumps(row,ensure_ascii=False)+'\n\n' for row in rows).encode()+b'data: [DONE]\n\n'
            else:packet=protocol.packets('内部资料显示住宿上限650元[1]。公开查询没有结果。')
            return httpx.Response(200,content=packet,headers={'content-type':'text/event-stream'})
        provider_client=httpx.AsyncClient(transport=httpx.MockTransport(provider));self.clients.append(provider_client)
        await self.web.close()
        self.web=WebSearchService(self.pool,self.settings,self.audit,client=provider_client)
        self.web.query_gateway=SimpleNamespace(complete=keyword,close=AsyncMock())
        model_client=httpx.AsyncClient(transport=httpx.MockTransport(model));self.clients.append(model_client)
        state,_,tools,_,_=await self.execute(self.login_as('zhangsan'),'查询本公司差旅住宿制度；另外公开搜索阿里云IQS价格',gateway=ModelGateway(self.settings,model_client))
        self.assertEqual(state['status'],'completed',state['state_json'])
        self.assertEqual([row['tool_name'] for row in tools],['search_knowledge','search_web'])
        self.assertEqual(len(keyword_inputs),1)
        self.assertEqual(keyword_inputs[0][1]['content'],'阿里云IQS价格')
        self.assertNotIn('650',json.dumps(keyword_inputs,ensure_ascii=False))
        self.assertNotIn('差旅',json.dumps(keyword_inputs,ensure_ascii=False))
        self.assertEqual(provider_inputs[0]['query'],'阿里云IQS价格')
        self.assertNotIn('650',json.dumps(provider_inputs,ensure_ascii=False))
        async def unsafe_keyword(factory,audit,emit):
            """注入关键词模块生成的明显私密载荷，验证生成后检查在任何新搜索请求前拒绝。"""
            return SimpleNamespace(content=json.dumps({'query':'客户明细和内部报价8万元'},ensure_ascii=False))
        self.web.query_gateway.complete=unsafe_keyword
        with self.assertRaises(SearchError) as caught:
            await self.web.search('公开搜索云产品价格',await self.context(self.login_as('zhangsan')),state)
        self.assertEqual(caught.exception.code,'WEB_QUERY_PRIVATE')
        self.assertEqual(len(provider_inputs),1)

    async def test_real_public_lane(self):
        """仅一条真实公开任务验证搜索接口重构后的模型/关键词/IQS链路，不重复旧阶段全量测试。"""
        state,reply,tools,_,_=await self.execute(self.login_as('zhangsan'),'请公开联网搜索阿里云IQS官方接口文档，提供来源。')
        self.assertEqual(state['status'],'completed',state['state_json'])
        self.assertTrue(tools)
        self.assertEqual({row['tool_name'] for row in tools},{'search_web'})
        self.assertTrue(all(row['status']=='completed' for row in tools),[row['output_json'] for row in tools])
        self.assertTrue(reply['protocol_json']['message']['citations'])


if __name__=='__main__':unittest.main()

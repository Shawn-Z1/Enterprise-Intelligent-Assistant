"""使用独立PG验证审核路由/发布边界；实际主模型、审核模型及检索另做一个端到端用例。"""
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from pydantic import SecretStr

import test_m04 as protocol
import test_m06_real as real
from app.answer_review import check_execution, ReviewDecision
from app.audit import AuditRecorder
from app.chat_service import ChatProcessor
from app.config import Settings
from app.ids import new_id
from app.model_gateway import ModelGateway
from app.tool_registry import model_observation
from app.web_search import WebSearchService


def review_packet(verdict='approve',basis='本轮没有实际工具记录'):
    """接收审核结论和确定性依据，返回审核协议夹具；不作为真实千问审核成功证据。"""
    issues = [] if verdict=='approve' else [{'aspect':'execution_truth','target':'我搜索过了但没找到',
        'basis':basis,'suggestion':'实际执行当前允许的检索，或明确本轮没有查询'}]
    return protocol.packets(json.dumps({'verdict':verdict,'issues':issues},ensure_ascii=False))


def tool_packet():
    """无参数返回确定性知识工具调用，用于验证审核反馈后的真实执行入口及持久观察。"""
    call = {'index':0,'id':'review_repair_call','type':'function','function':{'name':'search_knowledge','arguments':'{"query":"差旅上限"}'}}
    return ('data: '+json.dumps({'choices':[{'delta':{'tool_calls':[call]},'finish_reason':'tool_calls'}]})+'\n\ndata: [DONE]\n\n').encode()


class ReviewContract(unittest.TestCase):
    """确定性契约覆盖实际状态/引用与审核结论，普通数组不被当成工具引用。"""
    def test_execution_and_decision_contract(self):
        """构造可信工具记录及篡改观察，验证配对/终态/来源检查和严格审核格式；没有模型请求。"""
        call = {'id':'one','type':'function','function':{'name':'search_web','arguments':'{"query":"公开文档"}'}}
        source = {'type':'web','source_id':'s','url':'https://example.com','label':'1','content':'真实片段','title':'来源'}
        output = {'citations':[source],'message':'来源已取得'}
        observation = {'role':'tool','tool_call_id':'one','content':json.dumps(model_observation(output,'search_web'))}
        record = {'provider_call_id':'one','protocol_message':call,'tool_name':'search_web','input_json':{'query':'公开文档'},'status':'completed','output_json':output}
        messages = [{'role':'assistant','tool_calls':[call]},observation]
        self.assertEqual(check_execution(messages,[record],[source],'片段[1]'),[])
        self.assertTrue(check_execution(messages,[{**record,'status':'running'}],[source],'片段[9]'))
        self.assertTrue(check_execution(messages[:-1],[record],[source],'片段[1]'))
        self.assertTrue(check_execution(messages,[record],[{**source,'url':'https://other.com'}],'片段[1]'))
        self.assertTrue(check_execution(messages,[{**record,'input_json':{'query':'不同参数'}}],[source],'片段[1]'))
        self.assertTrue(check_execution(messages,[record],[{**source,'content':'伪造片段'}],'片段[1]'))
        self.assertEqual(check_execution([],[],[],'数组[1]'),[])
        with self.assertRaises(ValueError):ReviewDecision.model_validate({'verdict':'revise','issues':[]})
        with self.assertRaises(ValueError):ReviewDecision.model_validate({'verdict':'approve','issues':[],'answer':'未经审核的替代答案'})
        settings = Settings()
        gateway = ModelGateway(settings.model_copy(update={'fallback_model':'changed-fallback','fallback_api_key':SecretStr('independent-test')}))
        self.assertEqual(gateway.review_slot.model,settings.review_model)
        self.assertEqual(gateway.review_slot.key,settings.review_api_key.get_secret_value())
        asyncio.run(gateway.close())


class ReviewFlow(unittest.IsolatedAsyncioTestCase):
    """真实PG、生产图和网关上注入确定性模型/工具结果，验证边界；不触碰用户原会话。"""
    login = protocol.ChatTests.login
    queue = protocol.ChatTests.queue

    async def asyncSetUp(self):
        """无参数创建测试专属库，配置独立主/备/审槽位，初始化只记录协议的夹具列表。"""
        await protocol.ChatTests.asyncSetUp(self)
        self.settings = self.settings.model_copy(update={'review_base_url':'https://review.test/v1','review_api_key':SecretStr('fixture-review-secret'),
            'review_model':'fixture-review','review_context_tokens':32768,'primary_context_tokens':32768})
        self.audit = AuditRecorder(self.pool,'review-fixture',('fixture-review-secret','fixture-primary-key','fixture-fallback-key'))
        self.counts = {'primary':0,'review':0}
        self.requests = []
        self.outputs = []

    async def asyncTearDown(self):
        """无参数输出无凭证证据并清理本测试库/客户端，不改用户会话及主库。"""
        Path('/verification/answer-review').mkdir(parents=True,exist_ok=True)
        Path('/verification/answer-review/'+self._testMethodName+'.json').write_text(json.dumps(self.outputs,ensure_ascii=False,indent=2),encoding='utf-8')
        await protocol.ChatTests.asyncTearDown(self)

    async def execute(self,handler,question='公司差旅上限？',settings=None):
        """接收协议处理器、问题及可选配置，真实排队执行生产图，返回已持久化回复和可见事件。"""
        self.counts = {'primary':0,'review':0};self.requests=[]
        queued = self.queue(self.login(),question)
        async def transport(request):
            """接收实际网关HTTP请求，分类独立槽位后返回明确夹具；没有网络供应商伪装。"""
            slot = 'review' if request.url.host=='review.test' else 'primary'
            self.counts[slot] += 1
            self.requests.append({'slot':slot,'body':json.loads(request.content)})
            result = handler(slot,self.counts[slot],request,queued)
            return await result if asyncio.iscoroutine(result) else result
        client = httpx.AsyncClient(transport=httpx.MockTransport(transport));self.clients.append(client)
        gateway = ModelGateway(settings or self.settings,client);self.gateways.append(gateway)
        async def search(*args):
            """接收实际执行器查询，返回标注为协议测试的资料，用于记录真实工具调用与观察对应关系。"""
            return {'citations':[{'chunk_id':'fixture-chunk','title':'协议测试资料','content':'协议测试：差旅650元','label':'1'}],'message':'已取得协议测试资料'}
        async def validate(context, ids):
            """接收身份和测试切片编号，返回测试允许集合；真实ACL检索由独立端到端用例验证。"""
            return context,[{'chunk_id':value} for value in ids]
        rag = SimpleNamespace(search=search,validate=validate)
        processor = ChatProcessor(self.pool,settings or self.settings,gateway,self.audit,rag=rag)
        run = await processor.claim();await processor.process(run)
        state = await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',run['run_id'])
        reply = await self.admin.fetchrow("SELECT content,protocol_json FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",run['run_id'])
        events = await self.admin.fetch('SELECT event_type,payload FROM app_runtime.run_events WHERE run_id=$1 ORDER BY sequence',run['run_id'])
        self.outputs.append({'run_id':run['run_id'],'trace_id':run['trace_id'],'status':state['status'],'error':state['state_json'].get('error'),'answer':reply['content'] if reply else None,
            'review':reply['protocol_json']['message'].get('answer_review') if reply else None,'counts':dict(self.counts)})
        return state,reply,events

    async def test_missing_tool_repairs_without_publishing_draft(self):
        """注入零工具却声称搜索的初稿，审核驳回后补执行真实入口；通过前正文与审核JSON零泄露。"""
        def handler(slot,count,request,queued):
            """接收分类请求，模拟具体漏工具问题、返工调用及最终审核通过。"""
            if slot=='review':return httpx.Response(200,content=review_packet('revise' if count==1 else 'approve'))
            return httpx.Response(200,content=protocol.packets('幻觉草稿：我搜索过了但没找到') if count==1 else tool_packet() if count==2 else protocol.packets('协议测试资料说明650元[1]'))
        state,reply,events = await self.execute(handler)
        self.assertEqual(state['status'],'completed')
        self.assertEqual(reply['protocol_json']['message']['answer_review']['status'],'approved')
        self.assertEqual(self.counts,{'primary':3,'review':2})
        reviews = [json.loads(row['body']['messages'][1]['content']) for row in self.requests if row['slot']=='review']
        self.assertEqual(reviews[0]['tool_records'],[])
        self.assertEqual(reviews[1]['tool_records'][0]['status'],'completed')
        self.assertEqual(reviews[1]['tool_records'][0]['provider_call_id'],'review_repair_call')
        self.assertIn('search_knowledge',[tool['function']['name'] for tool in reviews[1]['available_tools']])
        self.assertTrue(all('tools' not in row['body'] for row in self.requests if row['slot']=='review'))
        visible = ''.join(row['payload'].get('text','') for row in events if row['event_type']=='assistant.delta')
        self.assertEqual(visible,reply['content']);self.assertNotIn('幻觉草稿',visible);self.assertNotIn('verdict',visible)
        first_delta = next(index for index,row in enumerate(events) if row['event_type']=='assistant.delta')
        self.assertTrue(any(row['event_type']=='review.completed' for row in events[:first_delta]))
        roles = await self.admin.fetch('SELECT model_role,count(*) AS n FROM app_runtime.model_attempts GROUP BY model_role')
        self.assertEqual({row['model_role']:row['n'] for row in roles},{'primary':3,'review':2})

    async def test_backend_rejects_false_approval(self):
        """审核模型错误通过未提供的引用，后端事实检查仍强制返工；无需依赖第二模型判断成功。"""
        def handler(slot,count,request,queued):
            """接收分类请求，故意让审核两次通过，主模型修正一次不存在的引用。"""
            if slot=='review':return httpx.Response(200,content=review_packet())
            return httpx.Response(200,content=tool_packet() if count==1 else protocol.packets('协议测试650元'+('[999]' if count==2 else '[1]')))
        state,reply,events = await self.execute(handler)
        review = reply['protocol_json']['message']['answer_review']
        self.assertEqual(state['status'],'completed');self.assertEqual(review['status'],'approved')
        self.assertEqual(review['revisions'],1);self.assertTrue(review['rounds'][0]['backend_issues'])
        self.assertNotIn('[999]',''.join(row['payload'].get('text','') for row in events))

    async def test_bounded_review_failures_and_cancellation(self):
        """集中验证二次驳回、证据不足、无效JSON、独立配置/鉴权、瞬时重试与审核期间取消。"""
        cases = [('twice','limited',2,2),('insufficient','limited',1,1),('json','unavailable',1,1),('auth','unavailable',1,1),('retry','approved',1,2),('config','unavailable',1,0)]
        for case,status,primary_count,review_count in cases:
            with self.subTest(case=case):
                def handler(slot,count,request,queued):
                    """接收请求，按当前用例注入一种审核故障或意见，不混入真实供应商成功证据。"""
                    if slot!='review':return httpx.Response(200,content=protocol.packets('未经核验草稿42'))
                    if case=='auth':return httpx.Response(401,json={'error':'fixture-review-secret'})
                    if case=='retry' and count==1:return httpx.Response(503,json={'error':'temporary'})
                    content = review_packet('revise') if case=='twice' else review_packet('insufficient') if case=='insufficient' else protocol.packets('不是JSON') if case=='json' else review_packet()
                    return httpx.Response(200,content=content)
                settings = self.settings.model_copy(update={'review_base_url':''}) if case=='config' else None
                state,reply,events = await self.execute(handler,'计算13加29',settings)
                self.assertEqual(state['status'],'completed')
                self.assertEqual(reply['protocol_json']['message']['answer_review']['status'],status)
                self.assertEqual(self.counts,{'primary':primary_count,'review':review_count})
                if status!='approved':self.assertNotIn('未经核验草稿',reply['content'])
        async def cancelled(slot,count,request,queued):
            """审核响应前在真实PG标记取消，验证缓冲路径检查取消并不发布已收到的草稿。"""
            if slot=='review':
                await self.admin.execute('UPDATE app_runtime.runs SET cancel_requested=true WHERE run_id=$1',queued['run_id'])
                return httpx.Response(200,content=review_packet())
            return httpx.Response(200,content=protocol.packets('取消时未审核草稿'))
        state,reply,events = await self.execute(cancelled,'计算13加29')
        self.assertEqual(state['status'],'cancelled',state['state_json']);self.assertIsNone(reply)
        self.assertFalse(any(row['event_type']=='assistant.delta' for row in events))
        audit = await self.admin.fetch('SELECT raw_json FROM app_audit.events')
        self.assertNotIn('fixture-review-secret',json.dumps([dict(row) for row in audit],default=str))


class RealReview(unittest.IsolatedAsyncioTestCase):
    """一次真实复合检索及一次直接回答，验证生产模型/审核/存储与来源全链路，不重跑全阶段。"""
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
        """无参数初始化独立真实存储和模型，审核配置完全取自独立REVIEW环境字段。"""
        await real.RealAgent.asyncSetUp(self)
        actual=Settings()
        names=('review_base_url','review_api_key','review_model','review_context_tokens','web_search_api_key','web_search_base_url')
        self.settings=self.settings.model_copy(update={name:getattr(actual,name) for name in names}|{'model_output_tokens':1600})
        self.audit=AuditRecorder(self.pool,'review-real',tuple(getattr(actual,name).get_secret_value() for name in ('primary_api_key','fallback_api_key','review_api_key','web_search_api_key')))
        self.web=WebSearchService(self.pool,self.settings,self.audit)
        self.evidence=[]

    async def asyncTearDown(self):
        """无参数记录无凭证摘要并清理本测试资源；用户真实历史不变。"""
        Path('/verification/answer-review').mkdir(parents=True,exist_ok=True)
        Path('/verification/answer-review/real.json').write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        await self.web.close()
        await real.RealAgent.asyncTearDown(self)

    async def test_real_combined_and_direct_review(self):
        """实际DeepSeek调用RAG/IQS并由独立千问核验；普通计算没有工具但仍进行审核。"""
        await self.indexed()
        session=self.login_as('zhangsan')
        for question,expected in [('查询我们公司的差旅住宿报销上限；另外公开联网搜索阿里云IQS官方接口文档。',{'search_knowledge','search_web'}),('计算13加29，只回答结果。',set())]:
            conversation=self.call(session,'POST','/conversations',{}).json()['conversation_id']
            queued=self.call(session,'POST',f'/conversations/{conversation}/messages',{'content':question,'request_key':new_id()})
            self.assertEqual(queued.status_code,202)
            gateway=ModelGateway(self.settings);self.gateways.append(gateway)
            processor=ChatProcessor(self.pool,self.settings,gateway,self.audit,rag=self.rag,web=self.web)
            run=await processor.claim();await processor.process(run)
            state=await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',run['run_id'])
            reply=await self.admin.fetchrow("SELECT content,protocol_json FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",run['run_id'])
            tools=await self.admin.fetch('SELECT tool_name,status,output_json FROM app_runtime.tool_calls WHERE run_id=$1',run['run_id'])
            attempts=await self.admin.fetch('SELECT model_role,model,status FROM app_runtime.model_attempts WHERE trace_id=$1',run['trace_id'])
            self.evidence.append({'run_id':run['run_id'],'trace_id':run['trace_id'],'status':state['status'],'answer':reply['content'] if reply else None,
                'review':reply['protocol_json']['message'].get('answer_review') if reply else None,'tools':[dict(row) for row in tools],'attempts':[dict(row) for row in attempts]})
            self.assertEqual(state['status'],'completed',state['state_json'])
            self.assertEqual(reply['protocol_json']['message']['answer_review']['status'],'approved',self.evidence[-1])
            self.assertEqual({row['tool_name'] for row in tools},expected)
            self.assertTrue(any(row['model_role']=='review' and row['model']==self.settings.review_model and row['status']=='completed' for row in attempts))
            self.assertTrue(any(row['model_role']=='primary' for row in attempts))
            self.assertIn('650' if expected else '42',reply['content'])
            if expected:
                citations=reply['protocol_json']['message']['citations']
                self.assertEqual({row['type'] for row in citations},{'knowledge','web'})
            events=await self.admin.fetch('SELECT event_type,payload FROM app_runtime.run_events WHERE run_id=$1 ORDER BY sequence',run['run_id'])
            first_delta=next(index for index,row in enumerate(events) if row['event_type']=='assistant.delta')
            self.assertTrue(any(row['event_type']=='review.completed' for row in events[:first_delta]))


if __name__=='__main__':
    unittest.main()

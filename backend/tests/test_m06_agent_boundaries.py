import json
import unittest
from unittest.mock import patch

import httpx
from pydantic import SecretStr
import test_m04 as protocol
import test_m06 as hybrid
from app.chat_service import ChatProcessor
from app.ids import new_id
from app.knowledge_service import KnowledgeService
from app.model_gateway import ModelGateway


def tool_packet(query='企业差旅报销住宿上限是多少？',extra=None,call_id='boundary_call'):
    """接收边界测试查询和非法字段，生成明确的协议故障夹具；正常 Agent 门槛另用实际厂商验收。"""
    arguments={'query':query,**(extra or {})}
    rows=[{'choices':[{'delta':{'tool_calls':[{'index':0,'id':call_id,'type':'function','function':{'name':'search_knowledge','arguments':json.dumps(arguments,ensure_ascii=False)}}]},'finish_reason':None}]},
          {'choices':[{'delta':{},'finish_reason':'tool_calls'}]}]
    return ''.join('data: '+json.dumps(row,ensure_ascii=False)+'\n\n' for row in rows).encode()+b'data: [DONE]\n\n'


class AgentBoundaries(unittest.IsolatedAsyncioTestCase):
    """真实存储/检索上的撤权与模型协议失败边界，不将确定性故障夹具当作真实 Agent 成功证据。"""
    login_as=hybrid.RagTests.login_as
    call=hybrid.RagTests.call
    headers=hybrid.RagTests.headers
    upload=hybrid.RagTests.upload
    submitted=hybrid.RagTests.submitted
    review=hybrid.RagTests.review
    drain=hybrid.RagTests.drain
    context=hybrid.RagTests.context
    indexed=hybrid.RagTests.indexed
    search=hybrid.RagTests.search

    async def asyncSetUp(self):
        """无参数创建真实独立库和集合，初始化只记录协议请求的故障客户端列表。"""
        await hybrid.RagTests.asyncSetUp(self)
        self.clients,self.gateways,self.requests=[],[],[]

    async def asyncTearDown(self):
        """无参数关闭本次协议夹具连接，再清理明确的独立数据库/集合及本次原件。"""
        for client in self.clients:await client.aclose()
        for gateway in self.gateways:await gateway.close()
        await hybrid.RagTests.asyncTearDown(self)

    async def execute(self,session,handler,question='公司差旅制度？'):
        """接收真实账号及确定性失败传输，使用生产 LangGraph 与 RAG 执行任务，返回真实 PG 终态。"""
        conversation=self.call(session,'POST','/conversations',{}).json()['conversation_id']
        response=self.call(session,'POST',f'/conversations/{conversation}/messages',{'content':question,'request_key':new_id()})
        self.assertEqual(response.status_code,202,response.text)
        async def record(request):
            """接收网关实际请求，记录协议 JSON 再返回指定故障夹具；不替换 RAG 或数据库。"""
            if request.url.host=='review-boundary.test':
                # 该组只验既有RAG边界，审核正路径/故障由独立test_answer_review验证。
                return httpx.Response(200,content=protocol.packets('{"verdict":"approve","issues":[]}'))
            self.requests.append(json.loads(request.content))
            return handler(request)
        client=httpx.AsyncClient(transport=httpx.MockTransport(record))
        self.clients.append(client)
        settings=self.settings.model_copy(update={'primary_base_url':'https://boundary.test/v1','primary_api_key':SecretStr('boundary-fixture-key'),
            'primary_model':'boundary-fixture','primary_context_tokens':32768,'model_retry_backoff_seconds':0,
            'review_base_url':'https://review-boundary.test/v1','review_api_key':SecretStr('review-boundary-key'),'review_model':'review-boundary','review_context_tokens':32768})
        gateway=ModelGateway(settings,client)
        self.gateways.append(gateway)
        processor=ChatProcessor(self.pool,settings,gateway,self.audit,rag=self.rag)
        run=await processor.claim()
        await processor.process(run)
        return dict(await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',run['run_id']))

    async def test_revoked_during_embedding_never_calls_recall(self):
        """真实向量生成过程中撤权，后续 Milvus 两路调用次数为零，不先召回再隐藏。"""
        await self.indexed()
        original=self.index.embed
        async def revoked(*args,**kwargs):
            """接收实际向量输入，完成真实推理后撤销测试用户权限，返回实际向量响应。"""
            result=await original(*args,**kwargs)
            await self.admin.execute("UPDATE app_system.users SET knowledge_enabled=false,permission_version=permission_version+1 WHERE user_id='U2'")
            return result
        with patch.object(self.index,'embed',side_effect=revoked),patch.object(self.index,'recall',side_effect=AssertionError('撤权后不得召回')) as recall:
            with self.assertRaises(Exception) as caught:await self.search(self.login_as('zhangsan'))
            self.assertEqual(caught.exception.status_code,403)
            self.assertEqual(recall.call_count,0)

    async def test_deleted_after_retrieval_never_enters_next_model(self):
        """真实召回结束后删除来源，工具观察仍保留审计，但下一次模型请求和最终助手消息都不存在。"""
        entry=await self.indexed()
        admin=self.login_as('admin')
        original=self.rag.search
        async def deleted(*args,**kwargs):
            """接收真实检索参数，取得真实结果后仅删除本测试材料，返回结果触发发送前复核。"""
            output=await original(*args,**kwargs)
            context=await self.context(admin)
            await KnowledgeService(self.pool,self.storage,self.audit).change_state(entry['application_id'],'delete',context,new_id())
            return output
        with patch.object(self.rag,'search',side_effect=deleted):
            run=await self.execute(self.login_as('zhangsan'),lambda request:httpx.Response(200,content=tool_packet()))
        self.assertEqual(run['status'],'failed')
        self.assertEqual(run['state_json']['error']['code'],'RAG_EVIDENCE_CHANGED')
        self.assertEqual(len(self.requests),1)
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_runtime.messages WHERE role='assistant'"),0)
        await self.drain()

    async def test_spoofed_tool_permissions_are_not_accepted(self):
        """模型额外传入管理员角色时严格拒绝，检索记录为零，原始真实协议请求保留审计。"""
        await self.indexed()
        run=await self.execute(self.login_as('zhangsan'),lambda request:httpx.Response(200,content=tool_packet(extra={'role':'admin'})))
        self.assertEqual(run['status'],'failed')
        self.assertEqual(run['state_json']['error']['code'],'INVALID_TOOL_ARGUMENTS')
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_runtime.retrievals'),0)

    async def test_empty_evidence_blocks_hallucinated_model_policy(self):
        """明确注入模型无证据编造，真实工具返回无来源时最终用户回答仍不能包含编造的内部金额。"""
        await self.indexed()
        def handler(request):
            """接收实际模型请求，首次调用不存在编号，第二次故意编造，仅用于验证发布守门。"""
            return httpx.Response(200,content=tool_packet('制度 ZX-2099-Z99 规定每天报销多少？') if len(self.requests)==1 else protocol.packets('根据内部制度，每天可以报销 9999999 元[1]。'))
        run=await self.execute(self.login_as('zhangsan'),handler)
        self.assertEqual(run['status'],'completed')
        content=await self.admin.fetchval("SELECT content FROM app_runtime.messages WHERE role='assistant'")
        self.assertIn('无法',content)
        self.assertNotIn('9999999',content)

    async def test_repeated_retrieval_keeps_unique_sources_and_labels(self):
        """真实多次检索同一文档时复用来源编号，完整模型工具配对保留，最终来源卡不重复。"""
        await self.indexed()
        def handler(request):
            """接收实际协议请求，要求两次不同工具调用检索同一知识，再按共同编号生成回答。"""
            if len(self.requests)<=2:return httpx.Response(200,content=tool_packet(call_id='call_'+str(len(self.requests))))
            return httpx.Response(200,content=protocol.packets('虚构制度规定每天650元[1]。'))
        run=await self.execute(self.login_as('zhangsan'),handler)
        self.assertEqual(run['status'],'completed')
        message=await self.admin.fetchval("SELECT protocol_json->'message' FROM app_runtime.messages WHERE role='assistant'")
        self.assertEqual(len(message['citations']),1)
        self.assertEqual(message['citations'][0]['label'],'1')
        self.assertEqual(len(self.requests),3)
        self.assertEqual(sum(item['role']=='tool' for item in self.requests[-1]['messages']),2)

    async def test_ordinary_brackets_are_not_mistaken_for_knowledge_citations(self):
        """普通文本直接回答中的数组/编号不作为企业来源校验，保留一般对话能力且不生成引用卡。"""
        run=await self.execute(self.login_as('zhangsan'),lambda request:httpx.Response(200,content=protocol.packets('数组的第一个元素可以写成 [1]。')))
        self.assertEqual(run['status'],'completed')
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_runtime.tool_calls'),0)
        message=await self.admin.fetchval("SELECT protocol_json->'message' FROM app_runtime.messages WHERE role='assistant'")
        self.assertEqual(message['citations'],[])

    async def test_model_query_rewrite_cannot_remove_original_identifier(self):
        """模型把用户不存在的指定编号改成泛化查询时，服务端仍保留原编号过滤，不用另一制度金额代答。"""
        await self.indexed()
        def handler(request):
            """接收实际请求，故意丢掉原问题的制度编号，再试图用泛化差旅金额回答，检查真实过滤守门。"""
            return httpx.Response(200,content=tool_packet('企业差旅住宿报销上限？') if len(self.requests)==1 else protocol.packets('每天650元[1]。'))
        run=await self.execute(self.login_as('zhangsan'),handler,'指定制度 ZX-2099-Z99 的差旅住宿上限是多少？')
        self.assertEqual(run['status'],'completed')
        message=await self.admin.fetchval("SELECT protocol_json->'message' FROM app_runtime.messages WHERE role='assistant'")
        self.assertEqual(message['citations'],[])
        self.assertIn('无法',message['content'])
        self.assertNotIn('650',message['content'])
        evidence=await self.admin.fetchval('SELECT evidence_json FROM app_runtime.retrievals')
        self.assertIn('zx-2099-z99',evidence['filter'])


if __name__=='__main__':unittest.main()

"""用户反馈定向回归：代码下标引用、个人画像及城市检索的实际链路。"""
import ast
import json
import re
import unittest
from types import SimpleNamespace

import test_m11 as memory_base
from app.answer_review import check_execution
from app.chat_service import ChatProcessor
from app.citations import citation_labels
from app.config import Settings
from app.ids import new_id
from app.memory_worker import Fact,accepted,parse_extraction
from app.web_search import public_location_context,WebSearchService


class FeedbackBoundaries(unittest.TestCase):
    """无模型调用验证三个实际失败边界，不放宽来源/身份校验。"""

    def test_code_indices_and_real_citations(self):
        """合法引用[1]与代码下标[0]/[9]共存，只有叙述中的未提供来源应被拒绝。"""
        answer='按照记忆[1]重写：\n```python\nsnake[0] = snake[1]\nrows[9]\n```\n行内 `snake[8]` 也是代码。'
        self.assertEqual(citation_labels(answer),['1'])
        self.assertEqual(citation_labels(answer+'\n错误来源[7]'),['1','7'])
        call={'id':'memory-call','type':'function','function':{'name':'search_user_memory','arguments':'{"query":"贪吃蛇"}'}}
        source={'type':'memory','chunk_id':'chunk','label':'1','content':'snake_color: 红色'}
        output={'citations':[source],'message':'实际记忆'}
        observation={'role':'tool','tool_call_id':'memory-call','content':json.dumps(output)}
        record={'provider_call_id':'memory-call','protocol_message':call,'tool_name':'search_user_memory','input_json':{'query':'贪吃蛇'},'output_json':output,'status':'completed'}
        self.assertEqual(check_execution([{'role':'assistant','tool_calls':[call]},observation],[record],[source],answer),[])
        self.assertTrue(check_execution([{'role':'assistant','tool_calls':[call]},observation],[record],[source],answer+'\n[7]'))

    def test_open_profile_and_city_projection(self):
        """体重/技能等明确自述可保存，授权字段拒绝；联网仅投影城市，不带画像其他字段和精确住址。"""
        text='我体重200斤，目前住在北京，我的技能是Python。'
        message={'message_id':'fixture','message_order':1}
        facts=[Fact(key='weight',value='200斤',quote='我体重200斤'),Fact(key='skills',value='Python',quote='我的技能是Python')]
        result=accepted(facts,text,message,profile=True)
        self.assertEqual(result['weight']['value'],'200斤');self.assertEqual(result['skills']['value'],'Python')
        parsed=parse_extraction(json.dumps({'profile':[{'key':'weight','value':100,'quote':'我体重200斤'},{'key':'skills','value':['Python','SQL'],'quote':'我的技能是Python'}],'preferences':[],'topics':[]}))
        normalized=accepted(parsed.profile,text,message,profile=True)
        self.assertEqual(normalized['weight']['value'],'200斤')
        self.assertEqual(parsed.profile[1].value,'Python、SQL')
        self.assertFalse(accepted([Fact(key='permission',value='admin',quote='我体重200斤')],text,message,profile=True))
        personal={'profile':{'location':'北京','weight':'200斤','skills':'Python'},'topics':{'content':'PRIVATE_PROJECT_SENTINEL'}}
        query='我的所在地附近有什么好玩的地方吗，给我推荐一些'
        self.assertEqual(public_location_context(query,personal),{'city':'北京'})
        self.assertEqual(public_location_context('我目前在上海，我附近有什么好玩的？',personal),{'city':'上海'})
        self.assertEqual(public_location_context('上海附近有什么好玩的？',personal),{})
        self.assertEqual(public_location_context(query,{'profile':{'location':'北京某路3号楼'}}),{})
        self.assertEqual(public_location_context('查阿里云价格',personal),{})


class FeedbackFlow(unittest.IsolatedAsyncioTestCase):
    """一组真实模型/索引/搜索路径覆盖三项反馈；复用隔离库，不改正式聊天历史。"""
    asyncSetUp=memory_base.MemoryTests.asyncSetUp
    asyncTearDown=memory_base.MemoryTests.asyncTearDown
    login=memory_base.MemoryTests.login
    queue=memory_base.MemoryTests.queue
    process_one=memory_base.MemoryTests.process_one
    context=memory_base.MemoryTests.context
    seed_turn=memory_base.MemoryTests.seed_turn
    drain=memory_base.MemoryTests.drain

    async def test_real_regenerate_and_local_search(self):
        """真实提取作品需求/个人体重技能，跨会话生成带下标代码及北京推荐，核对实际出网词和审核。"""
        headers=self.login()
        await self.seed_turn(headers,'我叫张三，我的技能是Python，我体重200斤，目前住在北京。')
        await self.seed_turn(headers,'帮我写一个贪吃蛇小游戏的python代码。')
        await self.drain()
        await self.seed_turn(headers,'我之前的贪吃蛇小游戏，蛇身颜色要红色。')
        await self.drain()
        overview=await self.memory.overview('U2')
        self.report.append({'overview':overview})
        self.assertIn('200斤',str(overview['profile']));self.assertIn('Python',str(overview['profile']))
        self.assertIn('贪吃蛇',str(overview['topics']))
        processor=ChatProcessor(self.pool,self.settings,self.gateway,self.audit,rag=SimpleNamespace(),long_memory=self.memory)
        queued=self.queue(headers,'先告诉我你记得的我的技能和体重，再按照当时的贪吃蛇要求重新生成一份完整Python代码。不要执行代码。')
        final=await self.process_one(processor)
        self.assertEqual(final['status'],'completed',final['state_json'])
        answer=await self.admin.fetchrow("SELECT content,protocol_json FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",queued['run_id'])
        self.report.append({'regenerated_answer':answer['content'],'review':answer['protocol_json']['message']['answer_review']})
        self.assertEqual(answer['protocol_json']['message']['answer_review']['status'],'approved',answer['content'])
        self.assertIn('200',answer['content']);self.assertIn('Python',answer['content'])
        code=re.search(r'```(?:python)?\s*\n(.*?)```',answer['content'],re.S)
        self.assertIsNotNone(code,answer['content']);ast.parse(code[1])
        self.assertTrue(re.search(r'\[\d+\]',code[1]))
        self.assertTrue(re.search(r'红色|255',answer['content']))
        # 后续公开工具只得到原始问题与本人粗略城市，主模型候选与画像其他内容不参与关键词。
        actual=Settings()
        web_settings=self.settings.model_copy(update={name:getattr(actual,name) for name in ('web_search_api_key','web_search_base_url','web_search_provider','web_search_engine_type')})
        web=WebSearchService(self.pool,web_settings,self.audit)
        try:
            processor.web=web
            local=self.queue(headers,'我的所在地附近有什么好玩的地方吗，给我推荐一些。')
            finished=await self.process_one(processor)
            self.assertEqual(finished['status'],'completed',finished['state_json'])
            local_answer=await self.admin.fetchrow("SELECT content,protocol_json FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",local['run_id'])
            outbound=await self.admin.fetchval('SELECT query FROM app_runtime.web_searches WHERE run_id=$1',local['run_id'])
            self.assertIsNotNone(outbound);self.assertIn('北京',outbound)
            request=await self.admin.fetchval("SELECT raw_json FROM app_audit.events WHERE run_id=$1 AND event_type='web.request'",local['run_id'])
            self.assertEqual(request['public_context'],{'city':'北京'})
            self.assertNotIn('200斤',str(request));self.assertNotIn('Python',str(request))
            self.report.append({'actual_outbound':outbound,'local_answer':local_answer['content'],'review':local_answer['protocol_json']['message']['answer_review'],'web_request':request})
            self.assertEqual(local_answer['protocol_json']['message']['answer_review']['status'],'approved',local_answer['content'])
            self.assertIn('北京',local_answer['content'])
            self.assertNotRegex(local_answer['content'],r'你说得对|审核意见|审查反馈|修正归因|上一轮的归因')
        finally:await web.close()


if __name__=='__main__':unittest.main()

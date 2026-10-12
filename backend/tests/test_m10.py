"""M10定向验收：真实PG/Redis，少量确定性边界与一条真实模型压缩对话，不重复压力矩阵。"""
import asyncio
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic import SecretStr
import test_m04 as base
from app.config import Settings
from app.chat_service import RunWriter
from app.context import ContextAssembler
from app.context_memory import ContextMemory, SUMMARY_INSTRUCTIONS, safe_history
from app.ids import new_id
from app.model_gateway import GatewayError, ModelGateway


class ContextTests(unittest.IsolatedAsyncioTestCase):
    """每项用独立数据库和命名空间，测试结束仅清理本项缓存/库，不读取正式业务附件。"""
    login=base.ChatTests.login
    queue=base.ChatTests.queue
    processor=base.ChatTests.processor
    process_one=base.ChatTests.process_one

    async def asyncSetUp(self):
        """无参数建立真实隔离PG，保留实际模型配置用于唯一真实路径，初始化本项报告和客户端集合。"""
        await base.ChatTests.asyncSetUp(self)
        self.actual=Settings();self.memories=[];self.evidence=[]
        self.settings=self.settings.model_copy(update={'primary_model':'deepseek-flash','fallback_model':'qwen3.8-flash',
            'review_base_url':'https://review.test/v1','review_api_key':SecretStr('fixture-review-key'),'review_model':'qwen3.8-flash',
            'primary_context_tokens':16384,'fallback_context_tokens':8192,'review_context_tokens':32768,
            'model_output_tokens':512,'context_summary_tokens':512,'context_compress_trigger_ratio':0.65})
        self.api.app.state.settings=self.settings

    async def asyncTearDown(self):
        """无参数删除本测试数据库对应Redis键，关闭本项客户端并调用原PG清理；保存安全数值证据。"""
        cleanup=ContextMemory(self.pool,self.settings,None,None)
        try:
            keys=[key async for key in cleanup.client.scan_iter(match=cleanup.namespace+':*')]
            if keys:await cleanup.client.delete(*keys)
        finally:
            await cleanup.close()
            for memory in self.memories:await memory.close()
            target=Path('/verification/M10');target.mkdir(parents=True,exist_ok=True)
            (target/(self._testMethodName+'.json')).write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2),encoding='utf-8')
            await base.ChatTests.asyncTearDown(self)

    async def seed_history(self,headers,turns=8,filler=350):
        """接收登录头、轮数及虚构背景长度，写入已完成轮次后返回会话；仅操作本测试库。"""
        conversation=self.api.post('/api/conversations',headers=headers,json={}).json()['conversation_id']
        for n in range(turns):
            ident=new_id()
            await self.admin.execute("INSERT INTO app_runtime.runs(run_id,trace_id,conversation_id,user_id,request_key,status) VALUES($1,$2,$3,'U2',$4,'completed')",ident,new_id(),conversation,new_id())
            content=('虚构个人计划：负责人李明，截止2026-10-20，预算120元。这个计划不要增加支出。' if n==0 else '继续这个虚构个人计划，原有要求不变。')+'\n'+('只是背景讨论，没有新增决定。\n'*filler)
            await self.admin.execute("INSERT INTO app_runtime.messages(message_id,conversation_id,user_id,role,content,run_id) VALUES($1,$2,'U2','user',$3,$4)",new_id(),conversation,content,ident)
            await self.admin.execute("INSERT INTO app_runtime.messages(message_id,conversation_id,user_id,role,content,run_id,protocol_json) VALUES($1,$2,'U2','assistant',$3,$4,'{}')",new_id(),conversation,'已记录这个个人计划，保持既定要求。',ident)
        return conversation

    def memory(self,processor,run):
        """接收执行器与已领取任务，构造本任务上下文管理器，并登记关闭责任，返回管理器。"""
        memory=ContextMemory(self.pool,processor.settings,processor.gateway,RunWriter(self.pool,self.audit,run))
        self.memories.append(memory);return memory

    async def test_cache_versions_isolation_and_rebuild(self):
        """缓存跨Worker命中、失效重建、连接故障降级和用户/会话隔离，内部派生正文不进入缓存。"""
        headers=self.login();queued=self.queue(headers)
        processor=self.processor(lambda request:httpx.Response(200,content=base.packets()))
        run=await processor.claim();first=self.memory(processor,run);rows=await first.history()
        self.assertEqual(first.last_cache_status,'rebuilt')
        second=self.memory(processor,run)
        with patch('app.context_memory.load_history',side_effect=AssertionError('缓存命中不应重读历史正文')):
            self.assertEqual(await second.history(),rows)
        self.assertEqual(second.last_cache_status,'hit')
        keys=[key async for key in first.client.scan_iter(match=first.namespace+':*')]
        await first.client.delete(*keys)
        self.assertEqual(await second.history(),rows);self.assertEqual(second.last_cache_status,'rebuilt')
        await self.admin.execute("UPDATE app_system.users SET permission_version=permission_version+1 WHERE user_id='U2'")
        await second.history();self.assertEqual(second.last_cache_status,'rebuilt')
        # 模拟仅Redis不可达，不能因此丢对话；不停止正式Redis容器。
        from redis.exceptions import ConnectionError
        with patch.object(second.client,'get',side_effect=ConnectionError()),patch.object(second.client,'set',side_effect=ConnectionError()):
            self.assertEqual(await second.history(),rows)
        secret='PRIVATE_DERIVED_SENTINEL'
        excluded=safe_history([{'run_id':'old','role':'user','content':'此前业务问题'},
            {'run_id':'old','role':'assistant','content':secret,'protocol_json':{'message':{'citations':[{'type':'business'}]}}},
            {'run_id':'new','role':'user','content':'个人计划'}])
        self.assertNotIn(secret,str(excluded));self.assertNotIn('此前业务问题',str(excluded))
        other=self.login('lisi');foreign=self.queue(other)
        other_run=await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',foreign['run_id'])
        foreign_memory=self.memory(processor,dict(other_run))
        self.assertNotEqual(await foreign_memory.history(),rows)
        self.assertTrue(all(row['user_id']==other_run['user_id'] for row in await foreign_memory.history()))
        self.evidence.append({'cache_rebuild':True,'cross_worker_hit':True,'redis_failure_fallback':True,'scope_isolation':True,'run_id':queued['run_id']})

    async def test_protocol_budget_summary_reuse_and_failure(self):
        """保留当前问题/调用ID/思考字段，长观察压缩不改原件；备用预算重算、无半份摘要和撤权重建。"""
        headers=self.login();conversation=await self.seed_history(headers)
        # 在真实历史中加入受保护派生轮次；缓存/摘要必须整轮排除，原数据库记录仍然保留。
        internal=new_id()
        await self.admin.execute("INSERT INTO app_runtime.runs(run_id,trace_id,conversation_id,user_id,request_key,status) VALUES($1,$2,$3,'U2',$4,'completed')",internal,new_id(),conversation,new_id())
        await self.admin.execute("INSERT INTO app_runtime.messages(message_id,conversation_id,user_id,role,content,run_id) VALUES($1,$2,'U2','user','PRIVATE_INTERNAL_QUESTION',$3)",new_id(),conversation,internal)
        await self.admin.execute("INSERT INTO app_runtime.messages(message_id,conversation_id,user_id,role,content,run_id,protocol_json) VALUES($1,$2,'U2','assistant','PRIVATE_DERIVED_SENTINEL',$3,$4)",new_id(),conversation,internal,{'message':{'citations':[{'type':'business'}]}})
        queued=self.queue(headers,'请继续个人计划，当前问题原样保留。',conversation)
        original_count=await self.admin.fetchval('SELECT count(*) FROM app_runtime.messages')
        calls=[];invalid=False
        def handler(request):
            """接收摘要请求，返回固定结构化摘要；开关用于故障，不冒充实际模型成功验收。"""
            nonlocal invalid
            body=json.loads(request.content);calls.append(body)
            value={'facts':['负责人李明，截止2026-10-20，预算120元'],'constraints':['不要增加支出'],
                   'decisions':[],'unfinished':['继续个人计划'],'caveats':['仅表示已有资料，不授权执行动作']}
            return httpx.Response(200,content=base.packets('not-json' if invalid else json.dumps(value,ensure_ascii=False)))
        processor=self.processor(handler);run=await processor.claim();memory=self.memory(processor,run)
        rows=await memory.history();assembler=ContextAssembler(rows,run['run_id'],512)
        call={'id':'paired-call','type':'function','function':{'name':'read_attachment','arguments':'{"attachment_id":"fixture"}'}}
        current=[{'role':'assistant','content':'','tool_calls':[call],'_provider':'deepseek','reasoning_content':'实际供应商返回的必要思考字段'},
            {'role':'tool','tool_call_id':'paired-call','content':json.dumps({'attachment_id':'fixture','coverage':{'start':0,'end':500,'total':10000},'data':'背景数据，'*7000},ensure_ascii=False)}]
        original=copy.deepcopy(current)
        slot=processor.gateway.slots[0]
        result=await memory.fit(slot,assembler.assemble(slot,False),current,[],memory.writer.check_active)
        self.assertEqual(current,original);self.assertEqual(result[-2]['tool_calls'],[call])
        self.assertEqual(result[-2]['reasoning_content'],current[-2]['reasoning_content'])
        self.assertEqual(result[-1]['tool_call_id'],'paired-call');self.assertIn('coverage',result[-1]['content'])
        self.assertIn('当前问题原样保留',str(result));self.assertLessEqual(memory.budget(slot,result,[]),slot.safe_limit)
        count=len(calls)
        rebuilt=self.memory(processor,run)
        keys=[key async for key in memory.client.scan_iter(match=memory.namespace+':*')]
        await memory.client.delete(*keys)
        same=await rebuilt.fit(slot,assembler.assemble(slot,False),current,[],memory.writer.check_active)
        self.assertEqual(same,result);self.assertEqual(len(calls),count)
        fallback=processor.gateway.slots[1]
        adjusted=await rebuilt.fit(fallback,assembler.assemble(fallback,False),current,[],memory.writer.check_active)
        self.assertLessEqual(memory.budget(fallback,adjusted,[]),fallback.safe_limit)
        # 明确验证增量分支：先发布两条旧消息的摘要，再扩大为四条；旧原文编号不重发给摘要模型。
        old=assembler.assemble(slot,False)[1:-1]
        await memory.summarize(slot,{'messages':old[:2]},'history',[item['_message_id'] for item in old[:2]],memory.writer.check_active)
        await memory.summarize(slot,{'messages':old[:4]},'history',[item['_message_id'] for item in old[:4]],memory.writer.check_active)
        self.assertIn('previous_verified_summary',calls[-1]['messages'][1]['content'])
        self.assertNotIn(old[0]['_message_id'],calls[-1]['messages'][1]['content'])
        saved=await self.admin.fetchval('SELECT count(*) FROM app_runtime.context_summaries')
        invalid=True
        with self.assertRaises(GatewayError):
            await memory.summarize(slot,{'messages':[{'role':'user','content':'触发新的摘要失败'}]},'history',[],memory.writer.check_active)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_runtime.context_summaries'),saved)
        invalid=False
        await self.admin.execute("UPDATE app_system.users SET permission_version=permission_version+1,knowledge_enabled=false,business_query_enabled=false WHERE user_id='U2'")
        await rebuilt.fit(slot,assembler.assemble(slot,False),current,[],memory.writer.check_active)
        self.assertGreater(await self.admin.fetchval('SELECT count(*) FROM app_runtime.context_summaries'),saved)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_runtime.messages'),original_count)
        self.assertNotIn('PRIVATE_DERIVED_SENTINEL',str(calls));self.assertNotIn('PRIVATE_INTERNAL_QUESTION',str(calls))
        self.assertTrue(all(body['max_tokens']<=512 for body in calls))
        self.evidence.append({'run_id':queued['run_id'],'summary_requests':len(calls),'safe_limit':slot.safe_limit,
                              'request_budget':memory.budget(slot,result,[]),'fallback_budget':memory.budget(fallback,adjusted,[]),
                              'messages_unchanged':True,'tool_pairs_unchanged':True,'failed_summary_not_published':True})

    async def test_real_small_window_conversation(self):
        """真实主模型分块压缩并回答、独立审核；小窗口触发，核对关键事实及PG原记录不丢失。"""
        headers=self.login();conversation=await self.seed_history(headers,turns=7,filler=180)
        names=('primary_base_url','primary_api_key','primary_model','fallback_base_url','fallback_api_key','fallback_model',
               'review_base_url','review_api_key','review_model')
        settings=self.settings.model_copy(update={name:getattr(self.actual,name) for name in names}|{
            'primary_context_tokens':8192,'fallback_context_tokens':8192,'review_context_tokens':32768,
            'model_output_tokens':1024,'context_summary_tokens':1024,'context_compress_trigger_ratio':0.75})
        # 补录时沿用小窗口虚构历史，触发比例改为正式配置的75%，避免用百万token实测产生不必要消耗。
        question='继续此前虚构个人计划，只告诉我负责人、明确截止日期、预算以及支出约束。不要查库、联网或生成文件。'
        self.queue(headers,question,conversation)
        gateway=ModelGateway(settings);self.gateways.append(gateway)
        from app.chat_service import ChatProcessor
        processor=ChatProcessor(self.pool,settings,gateway,self.audit,rag=SimpleNamespace())
        before=await self.admin.fetchval('SELECT count(*) FROM app_runtime.messages')
        # 只在测试里包装生产压缩入口，记录真实输入/输出视图；业务代码与正式数据不变。
        traces=[];fit=ContextMemory.fit
        async def traced_fit(memory,slot,base_messages,current,tools,guard,**kwargs):
            """接收真实压缩入口参数，分别保存原视图、预算和输出视图；调用原实现，不替代摘要模型。"""
            snapshot={'slot':slot.role,'model':slot.model,'window':slot.window,'safe_limit':slot.safe_limit,
                'trigger':min(int(slot.window*settings.context_compress_trigger_ratio),slot.safe_limit),
                'output_reserved':settings.model_output_tokens,'before_tokens':memory.budget(slot,base_messages+current,tools),
                'base':copy.deepcopy(base_messages),'current':copy.deepcopy(current),'tools':copy.deepcopy(tools)}
            output=await fit(memory,slot,base_messages,current,tools,guard,**kwargs)
            snapshot.update(after=copy.deepcopy(output),after_tokens=memory.budget(slot,output,tools))
            traces.append(snapshot)
            return output
        with patch.object(ContextMemory,'fit',traced_fit):result=await self.process_one(processor)
        self.assertEqual(result['status'],'completed',result['state_json'])
        answer=await self.admin.fetchrow("SELECT content,protocol_json FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",result['run_id'])
        for fact in ('李明','2026','10','20','120'):self.assertIn(fact,answer['content'])
        self.assertEqual(answer['protocol_json']['message']['answer_review']['status'],'approved')
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_runtime.messages'),before+1)
        summaries=await self.admin.fetch('SELECT version,metadata_json,summary_text,covered_message_ids FROM app_runtime.context_summaries ORDER BY version')
        self.assertTrue(summaries)
        trace=next(item for item in traces if item['slot']=='primary')
        self.assertGreater(trace['before_tokens'],trace['trigger'])
        self.assertLessEqual(trace['after_tokens'],trace['safe_limit'])
        request=await self.admin.fetchrow("""SELECT request_json,response_json FROM app_runtime.model_attempts
            WHERE trace_id=$1 AND model_role='primary' AND request_json#>>'{messages,0,content}' LIKE '你是知行企业智能助理%'
            ORDER BY created_at DESC LIMIT 1""",result['trace_id'])
        history=await self.admin.fetch("SELECT role,content,message_id FROM app_runtime.messages WHERE conversation_id=$1 AND run_id<>$2 ORDER BY message_order",conversation,result['run_id'])
        logfile=Path('/verification/M10/context-compression-trace.log')
        def section(title,value):
            """接收阶段标题和数据，以明显分隔线及缩进JSON输出，避免前后上下文混在一起。"""
            text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2,default=str)
            return '\n'+'='*90+'\n'+title+'\n'+'='*90+'\n'+text+'\n'
        previous=Path('/verification/M10/test_real_small_window_conversation.original.json')
        log=section('00 日志来源与测试说明',{'source':'当前版本的真实模型补录，非之前请求的逐字恢复',
            'previous_test':{'window':8192,'trigger_ratio':0.7,'output_reserved':1024,'summary_tokens':1024,'record':str(previous)},
            'note':'之前独立测试库已删除，仅保留结果JSON；本次按75%阈值补录完整过程。全部内容为虚构测试资料，不含正式用户数据或API密钥。'})
        log+=section('01 本次测试参数',{'run_id':result['run_id'],'model':settings.primary_model,'review_model':settings.review_model,
            'window':trace['window'],'trigger_ratio':0.75,'trigger_tokens':trace['trigger'],'safe_ratio':settings.model_context_safety_ratio,
            'safe_limit':trace['safe_limit'],'output_reserved':1024,'summary_tokens':1024,'keep_turns_target':settings.context_keep_turns,
            'history_turns':7,'filler_lines_per_user_message':180,
            'formal_runtime_note':'正式运行仍是100万窗口、75万触发、95万安全线、8192输出预留；小窗口只用于快速验证相同逻辑。'})
        log+=section('02 压缩前的原始历史：逐条完整列出',[dict(row) for row in history])
        log+=section('03 下一次追加的用户问题：原始文本',question)
        log+=section('04 压缩前待发送的完整消息与工具目录',{'messages':trace['base']+trace['current'],'tools':trace['tools']})
        log+=section('05 75%触发判断',{'estimated_input_and_tools':trace['before_tokens']-trace['output_reserved'],
            'output_reserved':trace['output_reserved'],'total_budget_before':trace['before_tokens'],'trigger_tokens':trace['trigger'],
            'comparison':f"{trace['before_tokens']} > {trace['trigger']}",'compression_triggered':True,
            'method':'按当前模型参考分词器计算请求消息和工具定义，再加输出预留；不是只数用户文字。'})
        log+=section('06 实际生成的摘要与覆盖范围',[dict(row) for row in summaries])
        kept=[item['_message_id'] for item in trace['after'] if item.get('_message_id') and any(row['message_id']==item['_message_id'] for row in history)]
        log+=section('07 压缩后的预算与保留范围',{'kept_history_message_ids':kept,'kept_complete_turns':len(kept)//2,
            'keep_turns_target':settings.context_keep_turns,'total_budget_after':trace['after_tokens'],
            'estimated_input_and_tools_after':trace['after_tokens']-trace['output_reserved'],'safe_limit':trace['safe_limit'],
            'within_safe_limit':trace['after_tokens']<=trace['safe_limit'],'original_history_not_deleted':True})
        log+=section('08 压缩后最终实际发送给主模型的完整请求JSON',request['request_json'])
        log+=section('09 供应商返回的实际用量',request['response_json'].get('usage'))
        log+=section('10 最终回答与独立审核结果',{'answer':answer['content'],'review':answer['protocol_json']['message']['answer_review']['status'],
            'original_messages_before':before,'messages_after':before+1,'checks':'负责人、日期、预算正确；审核通过；原历史未减少，只有一条最终回答新增。'})
        logfile.write_text(log,encoding='utf-8')
        print(f"\n压缩分段日志：{logfile}；压缩前预算{trace['before_tokens']}，75%阈值{trace['trigger']}，压缩后预算{trace['after_tokens']}。",flush=True)
        self.evidence.append({'run_id':result['run_id'],'status':result['status'],'review':'approved','answer':answer['content'],
                              'summaries':[dict(row) for row in summaries],'original_messages_unchanged':True})


if __name__=='__main__':unittest.main()

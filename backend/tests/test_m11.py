"""M11定向验收：独立PG/Redis/Milvus、少量真实模型跨会话路径及确定性乱序边界。"""
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import test_m04 as base
from app.audit import AuditRecorder
from app.auth import digest
from app.chat_service import ChatProcessor
from app.config import Settings
from app.context import ContextAssembler
from app.ids import new_id
from app.long_term_memory import LongTermMemory, MemoryArguments, enqueue_memory, directory_view
from app.memory_index import MemoryIndex
from app.memory_worker import MemoryProcessor, Fact, accepted, direct_text
from app.model_gateway import ModelGateway
from app.rag_service import live_context
from app.tool_registry import available_tools
from app.memory_consolidation import PersonalPlan


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    """复用既有隔离库基础，只定义本阶段测试，不重复历史全量矩阵或并发压力。"""
    login=base.ChatTests.login
    queue=base.ChatTests.queue
    process_one=base.ChatTests.process_one

    async def asyncSetUp(self):
        """无参数建立隔离库和唯一私人索引，配置真实主备/审核模型；正式库不被测试领取。"""
        await base.ChatTests.asyncSetUp(self)
        actual=Settings()
        names=('primary_base_url','primary_api_key','primary_model','fallback_base_url','fallback_api_key','fallback_model',
               'review_base_url','review_api_key','review_model','primary_context_tokens','fallback_context_tokens','review_context_tokens')
        self.settings=self.settings.model_copy(update={name:getattr(actual,name) for name in names}|{
            'memory_collection_prefix':self.name+'_memory','model_output_tokens':4096,'model_read_timeout_seconds':90,
            'run_timeout_seconds':300,'memory_job_timeout_seconds':240,'sandbox_enabled':False})
        self.api.app.state.settings=self.settings
        secrets=tuple(value.get_secret_value() for value in (actual.primary_api_key,actual.fallback_api_key,actual.review_api_key,actual.postgres_password,actual.app_minio_secret_key))
        self.audit=AuditRecorder(self.pool,'M11-test',secrets)
        self.index=MemoryIndex(self.settings)
        self.memory=LongTermMemory(self.pool,self.settings,self.index,self.audit)
        self.gateway=ModelGateway(self.settings);self.gateways.append(self.gateway)
        self.background=MemoryProcessor(self.pool,self.settings,self.gateway,self.audit,self.memory,'M11-worker-1')
        self.report=[]

    async def asyncTearDown(self):
        """无参数保存安全验收记录并清理本项集合/缓存/独立库；不删除正式记忆或服务卷。"""
        target=Path('/verification/M11');target.mkdir(parents=True,exist_ok=True)
        (target/(self._testMethodName+'.json')).write_text(json.dumps(self.report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        keys=[key async for key in self.memory.client.scan_iter(match=self.memory.namespace+':*')]
        if keys:await self.memory.client.delete(*keys)
        keys=[key async for key in self.memory.client.scan_iter(match='context:v1:'+self.name+':*')]
        if keys:await self.memory.client.delete(*keys)
        await self.memory.close()
        for name in await asyncio.to_thread(self.index.client.list_collections):
            if name.startswith(self.name+'_memory_'):await asyncio.to_thread(self.index.client.drop_collection,name)
        await self.index.close()
        await base.ChatTests.asyncTearDown(self)

    async def context(self,headers):
        """接收真实登录头，按Cookie摘要读取服务器会话，返回本人可信身份。"""
        token=headers['Cookie'].split('zhixing_session=')[1].split(';')[0]
        session=await self.admin.fetchrow('SELECT user_id,session_id FROM app_system.auth_sessions WHERE token_hash=$1',digest(token))
        return await live_context(self.pool,session['user_id'],session['session_id'])

    async def seed_turn(self,headers,text):
        """接收虚构用户原话，走真实API排队再保存成功轮次，用于确定性乱序边界，不冒充真实模型回答。"""
        queued=self.queue(headers,text)
        run=dict(await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',queued['run_id']))
        await self.admin.execute("UPDATE app_runtime.runs SET status='completed' WHERE run_id=$1",run['run_id'])
        message_id=new_id()
        await self.admin.execute("INSERT INTO app_runtime.messages(message_id,conversation_id,user_id,role,content,run_id) VALUES($1,$2,$3,'assistant','确定性完成夹具',$4)",message_id,run['conversation_id'],run['user_id'],run['run_id'])
        await enqueue_memory(self.admin,run,message_id)
        return dict(await self.admin.fetchrow("SELECT * FROM app_runtime.messages WHERE run_id=$1 AND role='user'",run['run_id']))

    async def drain(self):
        """无参数逐次领取实际后台队列，最多16步；失败直接显示持久原因，不无限重试验收。"""
        for _ in range(16):
            job=await self.background.claim()
            if not job:return
            print(f"[M11] {self.background.worker_id} 领取 {job['job_type']} 作业 {job['job_id']}",flush=True)
            await self.background.process(job)
            row=await self.admin.fetchrow('SELECT status,error_json FROM app_memory.jobs WHERE job_id=$1',job['job_id'])
            self.assertEqual(row['status'],'completed',dict(row))
        self.fail('后台队列未在有界步骤内结束')

    async def test_ordering_no_signal_cache_and_isolation(self):
        """真实双Worker抢任务、乱序纠正/重复/no-signal、缓存故障/目录预算及第三方证据拒绝。"""
        headers=self.login()
        first=await self.seed_turn(headers,'我长期偏好Python。')
        second=await self.seed_turn(headers,'更正我的长期偏好：我现在长期偏好Go。')
        worker2=MemoryProcessor(self.pool,self.settings,self.gateway,self.audit,self.memory,'M11-worker-2')
        jobs=await asyncio.gather(self.background.claim(),worker2.claim())
        self.assertEqual(len({job['job_id'] for job in jobs}),2)
        by_run={job['run_id']:(worker,job) for worker,job in zip((self.background,worker2),jobs)}
        for message in (second,first):
            worker,job=by_run[message['run_id']]
            fact=Fact(key='tech_stack',value='Go' if message==second else 'Python',quote=message['content'])
            delta={'profile':{},'preferences':accepted([fact],message['content'],message),'topics':[]}
            # 本项只验证PG乱序守卫，语义模型效果由独立真实整理用例验证，避免在边界夹具额外调用厂商。
            plan=PersonalPlan(operations=[{'action':'add' if message==second else 'update','category':'preferences',
                'old_key':None if message==second else 'tech_stack','new_key':'tech_stack','value':fact.value,'candidate_keys':['preferences:tech_stack']}])
            with patch.object(worker,'plan_personal',new=AsyncMock(return_value=plan)):
                await worker.aggregate(job,message,delta)
        overview=await self.memory.overview('U2')
        self.assertEqual(overview['preferences']['tech_stack'],'Go')
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_memory.jobs WHERE job_type=\'extract\''),2)
        snapshot=overview['snapshot_id']
        for worker,job in by_run.values():
            existing=await self.admin.fetchrow('SELECT * FROM app_memory.extractions WHERE message_watermark=$1',job['source_watermark'])
            await self.admin.execute("UPDATE app_memory.jobs SET status='running',lease_owner=$1,lease_until=now()+interval '60 seconds' WHERE job_id=$2",worker.worker_id,job['job_id'])
            message=second if job['run_id']==second['run_id'] else first
            await worker.aggregate(job,message,existing['raw_memory'],existing)
        self.assertEqual((await self.memory.overview('U2'))['snapshot_id'],snapshot)
        from redis.exceptions import ConnectionError
        with patch.object(self.memory.client,'get',side_effect=ConnectionError()),patch.object(self.memory.client,'set',side_effect=ConnectionError()):
            self.assertEqual((await self.memory.overview('U2'))['preferences']['tech_stack'],'Go')
        third=await self.seed_turn(headers,'谢谢，这次用英文回复。')
        # 先跳过已经排入的索引作业，只在本隔离库为no-signal领取提取任务。
        await self.admin.execute("UPDATE app_memory.jobs SET available_at=now()+interval '5 minutes' WHERE job_type='index'")
        job=await self.background.claim()
        await self.background.aggregate(job,third,{'profile':{},'preferences':{},'topics':[]})
        self.assertEqual((await self.memory.overview('U2'))['snapshot_id'],snapshot)
        other=self.login('lisi');context=await self.context(other)
        await self.admin.execute("UPDATE app_system.users SET knowledge_enabled=false WHERE user_id=$1",context.user_id)
        context=await self.context(other)
        names={tool['function']['name'] for tool in available_tools(context,False,memory_enabled=True)}
        self.assertIn('search_user_memory',names);self.assertNotIn('search_knowledge',names)
        self.assertFalse((await self.memory.overview(context.user_id))['preferences'])
        quoted='同事说我喜欢Java。\n> 我生日是1月1日\n```\n我偏好Rust\n```\n我长期偏好中文。'
        direct=direct_text(quoted,())
        self.assertNotIn('Java',direct);self.assertNotIn('Rust',direct);self.assertNotIn('1月1日',direct)
        self.assertFalse(accepted([Fact(key='tech_stack',value='Java',quote='我喜欢Java')],direct,first))
        self.assertFalse(accepted([Fact(key='role',value='admin',quote=first['content'])],first['content'],first))
        huge=[{'topic_key':new_id(),'title':'项目'+str(i),'aliases_json':[],'summary':'目录简介'*20} for i in range(200)]
        limited=directory_view(huge,self.settings.model_copy(update={'memory_directory_tokens':256}),self.settings.primary_model)
        self.assertTrue(limited['truncated']);self.assertLess(len(limited['items']),len(huge))
        # 过期租约恢复，旧Worker不得再发布。
        await self.admin.execute("UPDATE app_memory.jobs SET status='running',lease_owner='expired',lease_until=now()-interval '1 second' WHERE job_id=$1",job['job_id'])
        await self.background.recover()
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_memory.jobs WHERE job_id=$1',job['job_id']),'pending')
        with self.assertRaises(asyncio.CancelledError):await self.background.aggregate(job,third,{'profile':{},'preferences':{},'topics':[]})
        self.report.append({'ordering':'newest_source_wins','duplicate':'no_new_version','no_signal':'no_new_version','cache_failure':'PG_fallback','two_workers':'distinct_claims','private_tool_without_enterprise_access':True,'third_party_rejected':True,'directory_bounded':True,'lease_recovery':True})

    async def test_real_cross_conversation_hybrid_memory(self):
        """两账号真实提取、真实私人混合索引、主模型按需调用及独立审核；校验来源/旧版本/管理员隔离。"""
        headers=self.login()
        text='我是一名软件工程师，生日是5月12日。我长期偏好中文回答，先结论后步骤。我的个人项目北辰离线助手的本地部署方案已确定：使用Linux、Docker Compose和PostgreSQL，明确禁止自动联网。请确认这些要求。'
        self.queue(headers,text)
        processor=ChatProcessor(self.pool,self.settings,self.gateway,self.audit,rag=SimpleNamespace(),long_memory=self.memory)
        first=await self.process_one(processor)
        self.assertEqual(first['status'],'completed',first['state_json'])
        await self.drain()
        overview=await self.memory.overview('U2')
        self.assertIn('中文',str(overview['preferences']));self.assertIn('软件',str(overview['profile']))
        self.assertIn('5',str(overview['profile']))
        self.assertGreater(overview['topics']['total'],0)
        lisi=self.login('lisi');lisi_context=await self.context(lisi)
        await self.seed_turn(lisi,'我长期偏好英文回答，使用TypeScript开发。我自己的南桥界面项目明确使用SQLite，保留离线运行。')
        await self.drain()
        other=await self.memory.overview(lisi_context.user_id)
        self.assertIn('英文',str(other['preferences']));self.assertNotIn('北辰',str(other))
        self.assertNotEqual(other['snapshot_id'],overview['snapshot_id'])
        # 无项目任务自动使用本人语言偏好，不强制触发项目检索。
        greeting=self.queue(lisi,'请给我一句简短的问候。')
        greeted=await self.process_one(processor)
        self.assertEqual(greeted['status'],'completed',greeted['state_json'])
        greeting_answer=await self.admin.fetchval("SELECT content FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",greeting['run_id'])
        self.report.append({'user2_overview':other,'user2_greeting':greeting_answer,'greeting_run':greeting['run_id'],
            'greeting_review':await self.admin.fetchval("SELECT protocol_json FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",greeting['run_id']),
            'greeting_attempts':[dict(row) for row in await self.admin.fetch('SELECT model_role,status,response_json FROM app_runtime.model_attempts WHERE trace_id=$1',greeting['trace_id'])]})
        self.assertRegex(greeting_answer,r'[A-Za-z]{3,}')
        self.assertNotRegex(greeting_answer,r'[\u4e00-\u9fff]')
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_runtime.tool_calls WHERE run_id=$1 AND tool_name='search_user_memory'",greeting['run_id']),0)
        headers=self.login()
        await self.admin.execute("UPDATE app_system.users SET knowledge_enabled=false WHERE user_id='U2'")
        question='继续我的北辰离线助手本地部署方案，列出之前已经确定的操作系统、部署方式、数据库和联网限制。不要重新设计。'
        queued=self.queue(headers,question)
        final=await self.process_one(processor)
        self.assertEqual(final['status'],'completed',final['state_json'])
        answer=await self.admin.fetchrow("SELECT content,protocol_json FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",queued['run_id'])
        self.assertEqual(answer['protocol_json']['message']['answer_review']['status'],'approved',answer['content'])
        for word in ('Linux','Docker','PostgreSQL'):self.assertIn(word,answer['content'])
        calls=await self.admin.fetch("SELECT * FROM app_runtime.tool_calls WHERE run_id=$1 AND tool_name='search_user_memory'",queued['run_id'])
        self.assertTrue(calls)
        citations=[citation for call in calls for citation in call['output_json'].get('citations',[])]
        self.assertTrue(citations)
        chunk=citations[0]['chunk_id']
        self.assertEqual(self.api.get('/api/memory/sources/'+chunk,headers=headers).status_code,200)
        self.assertEqual(self.api.get('/api/memory/sources/'+chunk,headers=lisi).status_code,404)
        admin=self.login('admin')
        self.assertEqual(self.api.get('/api/memory/sources/'+chunk,headers=admin).status_code,404)
        retrieval=await self.admin.fetchval("SELECT raw_json FROM app_audit.events WHERE event_type='memory.retrieval' AND run_id=$1",queued['run_id'])
        self.assertTrue(retrieval['dense']);self.assertTrue(retrieval['bm25']);self.assertTrue(retrieval['rrf']);self.assertTrue(retrieval['rerank'])
        self.assertIn('user_id == "U2"',retrieval['filter'])
        self.assertNotIn('南桥',str(retrieval['output']))
        self.assertTrue(await self.memory.validate(await self.context(headers),citations))
        # 主请求自动概要、详情仅在工具结果出现；请求与真实usage永久可查。
        request=await self.admin.fetchval("SELECT request_json FROM app_runtime.model_attempts WHERE trace_id=$1 AND model_role='primary' ORDER BY created_at LIMIT 1",queued['trace_id'])
        self.assertIn('personal_memory',str(request));self.assertIn('北辰',str(request))
        self.assertNotIn('database: PostgreSQL',str(request))
        self.assertTrue(await self.admin.fetchval("SELECT count(*) FROM app_runtime.model_attempts WHERE model_role LIKE 'memory_%'"))
        with patch.object(self.index,'ensure',side_effect=RuntimeError('fixture-index-down')):
            failed=await self.memory.search(MemoryArguments(query='北辰部署'),await self.context(headers),new_id(),None)
        self.assertEqual(failed['status'],'unavailable')
        self.assertIn('中文',str((await self.memory.overview('U2'))['preferences']))
        # 发布新纠正；旧来源即使仍在Milvus/永久PG也不能回传。
        before=overview['snapshot_id']
        await self.seed_turn(headers,'更正北辰离线助手本地部署方案：数据库改为SQLite，其他约束不变。')
        await self.drain()
        updated=await self.memory.overview('U2')
        self.assertNotEqual(updated['snapshot_id'],before)
        self.assertFalse(await self.memory.validate(await self.context(headers),citations))
        self.assertEqual(self.api.get('/api/memory/sources/'+chunk,headers=headers).status_code,404)
        newer=await self.memory.search(MemoryArguments(query='北辰离线助手部署数据库'),await self.context(headers),new_id(),None)
        self.assertIn('SQLite',str(newer['citations']))
        self.assertNotIn('database: PostgreSQL',str(newer['citations']))
        self.report.append({'real_main_run':queued['run_id'],'answer':answer['content'],'review':'approved','overview':overview,
            'hybrid_evidence':retrieval,'index_failure':'overview_still_available','new_snapshot':updated['snapshot_id'],'old_source_blocked':True,'source_owner_only':True})


if __name__=='__main__':unittest.main()

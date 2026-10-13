"""个人记忆语义整理定向验收：计划边界、真实模型合并与真实PG版本冲突。"""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import test_m11 as memory_base
from app.memory_consolidation import PersonalPlan, apply_plan, runtime_fields
from app.memory_worker import Fact, accepted, FORBIDDEN, MemoryProcessor
from app.web_search import public_location_context


def fact(value,order=1):
    """接收虚构值及顺序，返回完整来源夹具；只用于确定性后端约束测试。"""
    return {'value':value,'order':order,'message_id':'message-'+str(order),'quote':value,'observed_at':None,'extraction_id':'extraction-'+str(order)}


def operation(action,key,value=None,old=None,refs=None,merged=None,category='preferences'):
    """接收动作、目标和来源定位，生成测试计划；不模拟语义模型正确性。"""
    return {'action':action,'category':category,'new_key':key,'old_key':old,'value':value,
            'candidate_keys':refs or [category+':'+(key or old)],'merged_keys':merged or []}


class PlanBoundaries(unittest.TestCase):
    """确定性执行检查：允许自由命名但不允许隐式覆盖、伪来源或晚到写回。"""

    def test_rename_merge_and_target_collision(self):
        """已存在目标须明确合并；旧key退出现行字典且来源保留，错误方案不修改输入。"""
        current={'travel_preference':fact('旅游喜欢安静'),'leisure_preference':fact('游戏喜欢策略类',2)}
        original=copy.deepcopy(current)
        delta={'profile':{},'preferences':{'new_interest':fact('旅游喜欢安静、游戏偏好策略类',3)}}
        op=operation('update','leisure_preference','旅游喜欢安静、游戏偏好策略类',old='travel_preference',refs=['preferences:new_interest'])
        with self.assertRaisesRegex(ValueError,'目标冲突'):
            apply_plan({},current,delta,PersonalPlan(operations=[op]),{},FORBIDDEN)
        self.assertEqual(current,original)
        op['merged_keys']=[{'category':'preferences','key':'leisure_preference'}]
        _,result,clocks=apply_plan({},current,delta,PersonalPlan(operations=[op]),{},FORBIDDEN)
        self.assertEqual(set(result),{'leisure_preference'})
        self.assertEqual(len(result['leisure_preference']['sources']),3)
        self.assertEqual(clocks['preferences:travel_preference'],3)

    def test_ignore_clock_old_correction_and_invalid_refs(self):
        """重复确认不改value，较晚完成的旧纠正被水位阻止；遗漏来源或未知旧key拒绝。"""
        existing={'tech_stack':fact('当前Java，之前Python',10)}
        same={'profile':{},'preferences':{'coding_style':fact('仍使用Java',30)}}
        ignore=operation('ignore',None,old='tech_stack',refs=['preferences:coding_style'])
        _,result,clocks=apply_plan({},existing,same,PersonalPlan(operations=[ignore]),{},FORBIDDEN)
        self.assertEqual(result,existing)
        delayed={'profile':{},'preferences':{'tech_stack':fact('改用Rust',20)}}
        update=operation('update','tech_stack','当前Rust',old='tech_stack')
        _,late,_=apply_plan({},result,delayed,PersonalPlan(operations=[update]),clocks,FORBIDDEN)
        self.assertEqual(late,existing)
        with self.assertRaisesRegex(ValueError,'旧key不存在'):
            apply_plan({},existing,delayed,PersonalPlan(operations=[{**update,'old_key':'someone_else'}]),{},FORBIDDEN)
        with self.assertRaisesRegex(ValueError,'候选没有一一处理'):
            apply_plan({},existing,delayed,PersonalPlan(operations=[]),{},FORBIDDEN)

    def test_free_keys_keep_program_projections(self):
        """自由重命名所在地与表达偏好，不从历史叙述猜地点或破坏默认语言。"""
        profile={'location':fact('北京')};prefs={'language':fact('英文')}
        delta={'profile':{'location':fact('上海',2)},'preferences':{'language':fact('中文',2)}}
        plan=PersonalPlan(operations=[operation('update','residence','现住上海，以前在北京',old='location',refs=['profile:location'],category='profile'),
            operation('update','communication_preference','目前中文，以前英文',old='language',refs=['preferences:language'])])
        new_profile,new_prefs,_=apply_plan(profile,prefs,delta,plan,{},FORBIDDEN)
        fields=runtime_fields(new_profile,new_prefs)
        self.assertEqual(fields,{'location':'上海','language':'中文'})
        self.assertEqual(public_location_context('我的所在地附近有什么好玩的？',{'profile':{},'runtime_fields':fields}),{'city':'上海'})


class SemanticMemoryTests(unittest.IsolatedAsyncioTestCase):
    """使用独立PG和少量真实整理模型请求；候选夹具定位新阶段，不重复文件/并发压力测试。"""
    asyncSetUp=memory_base.MemoryTests.asyncSetUp
    asyncTearDown=memory_base.MemoryTests.asyncTearDown
    login=memory_base.MemoryTests.login
    queue=memory_base.MemoryTests.queue
    context=memory_base.MemoryTests.context
    seed_turn=memory_base.MemoryTests.seed_turn

    async def pending(self,headers,text,key,value):
        """接收虚构本人原话与事实，走实际API水位后校验原话，返回作业源及个人候选。"""
        message=await self.seed_turn(headers,text)
        delta={'profile':{},'preferences':accepted([Fact(key=key,value=value,quote=text)],text,message),'topics':[]}
        return message,delta

    async def claim_message(self,message):
        """接收目标原话，将本独立库索引暂时后移，只领取待验证的提取作业。"""
        await self.admin.execute("UPDATE app_memory.jobs SET available_at=now()+interval '1 hour' WHERE job_type='index'")
        job=await self.background.claim()
        self.assertEqual(job['run_id'],message['run_id'])
        return job

    async def test_real_same_meaning_correction_and_ignore(self):
        """真实模型语义合并不同key、表达当前Java/此前Python，重复忽略不生成版本，并可跨账号隔离。"""
        headers=self.login()
        first,delta=await self.pending(headers,'我主要使用Python，以后代码示例优先用Python。','tech_stack','主要Python，代码示例优先Python')
        job=await self.claim_message(first)
        await self.background.aggregate(job,first,delta)
        second,delta=await self.pending(headers,'更正：我现在主要使用Java，后面的代码示例请用Java；之前主要使用Python。','coding_preference','当前主要Java，示例Java；之前Python')
        job=await self.claim_message(second)
        await self.background.aggregate(job,second,delta)
        overview=await self.memory.overview('U2')
        self.report.append({'after_correction':overview})
        self.assertEqual(len(overview['preferences']),1)
        value=next(iter(overview['preferences'].values()))
        self.assertIn('Java',value);self.assertIn('Python',value)
        self.assertTrue('当前' in value or '现在' in value)
        snapshot=overview['snapshot_id']
        # 旧Rust陈述先入库但尚未处理，随后重复确认Java；后端必须阻止旧作业晚到覆盖。
        older,old_delta=await self.pending(headers,'我要改用Rust，后面的示例用Rust。','rust_preference','改用Rust')
        await self.admin.execute("UPDATE app_memory.jobs SET available_at=now()+interval '1 hour' WHERE run_id=$1",older['run_id'])
        repeated,repeat_delta=await self.pending(headers,'我的偏好没变，仍主要使用Java，后面的示例仍用Java。','preferred_stack','仍主要使用Java，示例Java')
        repeat_job=await self.claim_message(repeated)
        await self.background.aggregate(repeat_job,repeated,repeat_delta)
        self.assertEqual((await self.memory.overview('U2'))['snapshot_id'],snapshot)
        await self.admin.execute('UPDATE app_memory.jobs SET available_at=now() WHERE run_id=$1',older['run_id'])
        old_job=await self.claim_message(older)
        stored_key=next(iter(overview['preferences']))
        forced=PersonalPlan(operations=[operation('update',stored_key,'当前Rust',old=stored_key,refs=['preferences:rust_preference'])])
        with patch.object(self.background,'plan_personal',new=AsyncMock(return_value=forced)):
            await self.background.aggregate(old_job,older,old_delta)
        self.assertEqual((await self.memory.overview('U2'))['snapshot_id'],snapshot)
        rows=await self.admin.fetch("SELECT model_role,status FROM app_runtime.model_attempts WHERE model_role LIKE 'memory_consolidate_%'")
        self.assertGreaterEqual(len(rows),3);self.assertTrue(all(row['status']=='completed' for row in rows))
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_memory.consolidations WHERE status='no_change'"),2)
        other=self.login('lisi');context=await self.context(other)
        self.assertFalse((await self.memory.overview(context.user_id))['preferences'])
        self.report.append({'repeat_snapshot_unchanged':True,'late_old_job_blocked':True,'model_roles':[dict(row) for row in rows]})

    async def test_pg_conflict_replans_and_target_collision_keeps_old(self):
        """确定性制造锁外版本竞争，旧计划必须重读；目标冲突整个事务回滚，候选可重试且不丢失。"""
        headers=self.login()
        first,delta=await self.pending(headers,'我喜欢安静旅游。','travel_preference','旅游喜欢安静')
        first_job=await self.claim_message(first)
        plan=PersonalPlan(operations=[operation('add','travel_preference','旅游喜欢安静')])
        with patch.object(self.background,'plan_personal',new=AsyncMock(return_value=plan)):
            await self.background.aggregate(first_job,first,delta)
        pending,pending_delta=await self.pending(headers,'我喜欢篮球。','sports','喜欢篮球')
        job=await self.claim_message(pending)
        competitor,comp_delta=await self.pending(headers,'我喜欢策略游戏。','gaming','喜欢策略游戏')
        other_worker=MemoryProcessor(self.pool,self.settings,self.gateway,self.audit,self.memory,'M11-worker-other')
        comp_job=await other_worker.claim()
        calls=[]
        async def compete(selected,change,current):
            """记录实际读取版本，首轮整理期间由另一Worker提交，第二轮必须看到新gaming条目。"""
            calls.append(current['snapshot_id'])
            if len(calls)==1:
                other_plan=PersonalPlan(operations=[operation('add','gaming','喜欢策略游戏')])
                with patch.object(other_worker,'plan_personal',new=AsyncMock(return_value=other_plan)):
                    await other_worker.aggregate(comp_job,competitor,comp_delta)
            else:self.assertIn('gaming',current['preferences_json'])
            return PersonalPlan(operations=[operation('add','sports','喜欢篮球')])
        with patch.object(self.background,'plan_personal',compete):await self.background.aggregate(job,pending,pending_delta)
        self.assertEqual(len(calls),2);self.assertNotEqual(calls[0],calls[1])
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_memory.consolidations WHERE job_id=$1 AND status='version_conflict'",job['job_id']),1)
        before=await self.memory.overview('U2')
        invalid,invalid_delta=await self.pending(headers,'旅游时我也想打篮球。','travel_extra','旅游也想篮球')
        invalid_job=await self.claim_message(invalid)
        collision=PersonalPlan(operations=[operation('update','gaming','覆盖游戏',old='travel_preference',refs=['preferences:travel_extra'])])
        with patch.object(self.background,'plan_personal',new=AsyncMock(return_value=collision)),self.assertRaisesRegex(ValueError,'目标冲突'):
            await self.background.aggregate(invalid_job,invalid,invalid_delta)
        self.assertEqual((await self.memory.overview('U2'))['snapshot_id'],before['snapshot_id'])
        self.assertTrue(await self.admin.fetchval('SELECT count(*) FROM app_memory.extractions WHERE source_message_ids @> $1::varchar[]',[invalid['message_id']]))
        self.report.append({'pg_conflict_replans':2,'old_plan_not_committed':True,'invalid_target_old_snapshot_preserved':True,'candidate_retained_for_retry':True})

    async def test_real_extract_then_consolidate_pipeline(self):
        """真实新增原话→提取→整理→PG回执，第二次同义重复不增快照；两阶段模型职责与usage可追踪。"""
        headers=self.login()
        snapshots=[]
        for text in ('我长期喜欢在旅行时去人少安静的地方。','我的旅游偏好没有变化，仍然喜欢人少安静的地方。'):
            message=await self.seed_turn(headers,text)
            job=await self.claim_message(message)
            await self.background.process(job)
            status=await self.admin.fetchrow('SELECT status,error_json FROM app_memory.jobs WHERE job_id=$1',job['job_id'])
            self.assertEqual(status['status'],'completed',dict(status))
            overview=await self.memory.overview('U2')
            self.report.append({'overview':overview,'job_id':job['job_id']})
            self.assertEqual(len(overview['preferences']),1)
            self.assertIn('安静',str(overview['preferences']))
            snapshots.append(overview['snapshot_id'])
        self.assertEqual(snapshots[0],snapshots[1])
        for role in ('memory_extract_','memory_consolidate_'):
            self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_runtime.model_attempts WHERE model_role LIKE $1',role+'%'),2)
        usages=await self.admin.fetchval("SELECT count(*) FROM app_audit.events WHERE event_type='model.response' AND usage_source='reported'")
        self.assertEqual(usages,4)
        self.report.append({'pipeline_real_models':True,'repeat_no_version':True,'reported_usage_events':usages})


if __name__=='__main__':unittest.main()

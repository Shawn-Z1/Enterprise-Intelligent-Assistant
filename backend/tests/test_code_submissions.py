"""代码提交共享表目标验收：模拟种子/技术部全表范围与两个真实模型统计任务。"""
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

import test_m07 as m07
from app.audit import AuditRecorder
from app.business_sql import allowed_catalog,validate_sql,bind_values
from app.config import Settings
from app.db_admin import seed
from app.model_gateway import ModelGateway


class CodeRules(unittest.TestCase):
    """共享记录日期查询的静态规则与时区参数，不重复创建集成库。"""
    def test_date_grouping_and_timezone_binding(self):
        """验证DATE_TRUNC允许用于共享视图，时间参数默认北京时间且不覆盖显式UTC。"""
        context=SimpleNamespace(can_query_business=True,is_admin=False,department_name='技术部',grade=1,employee_id='E2')
        sql,tables=validate_sql("SELECT DATE_TRUNC('day',submitted_at)::date AS day,COUNT(*) AS total FROM demo_business.visible_code_submissions GROUP BY day ORDER BY day",[],context)
        self.assertEqual(tables,['visible_code_submissions']);self.assertIn('DATE_TRUNC',sql)
        # 重放用户实际失败的日期写法，包括本人过滤、显式北京时间及完整共享统计。
        for date_expression in ("DATE(submitted_at)","DATE(submitted_at AT TIME ZONE 'Asia/Shanghai')"):
            for personal in (False,True):
                where=" AND author_employee_id=$3" if personal else ""
                params=['2026-09-07','2026-10-01']+(['E2'] if personal else [])
                canonical,_=validate_sql(f"SELECT {date_expression} AS day,COUNT(*) AS total FROM demo_business.visible_code_submissions WHERE submitted_at >= $1 AND submitted_at < $2{where} GROUP BY {date_expression} ORDER BY day",params,context)
                self.assertIn('DATE(',canonical)
        # 日期函数放行不能扩大危险函数或底表权限。
        for forbidden in ("SELECT pg_sleep(1) FROM demo_business.visible_code_submissions","SELECT DATE(submitted_at) FROM demo_business.code_submissions"):
            with self.assertRaises(Exception):validate_sql(forbidden,[],context)
        values=bind_values(['2026-09-07','2026-09-07T00:00:00+00:00'],[SimpleNamespace(name='timestamptz')]*2)
        self.assertEqual(values[0].utcoffset().total_seconds(),8*3600);self.assertEqual(values[1].utcoffset().total_seconds(),0)


class CodeSubmissions(unittest.IsolatedAsyncioTestCase):
    """复用隔离PG和生产确认流程，避免重跑M07全部验收。"""
    asyncSetUp=m07.M07Flow.asyncSetUp
    login=m07.M07Flow.login
    queue=m07.M07Flow.queue
    context=m07.M07Flow.context
    processor=m07.M07Flow.processor
    decide=m07.M07Flow.decide
    finish=m07.M07Flow.finish

    async def asyncTearDown(self):
        """无参数保存无凭证摘要，随后清理仅本次测试资源；不修改主库历史。"""
        Path('/verification/code-submissions').mkdir(parents=True,exist_ok=True)
        Path('/verification/code-submissions/'+self._testMethodName+'.json').write_text(json.dumps(self.evidence,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        await m07.M07Flow.asyncTearDown(self)

    async def query(self,question,headers,processor):
        """接收问题/真实身份/执行器，实际准备并确认查询，返回PG回执及最终回答，确认前零新增fetch。"""
        before=len(self.sent)
        self.queue(headers,question)
        run=await processor.claim();await processor.process(run)
        state=await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',run['run_id'])
        if state['status']!='waiting_confirmation':
            self.evidence.append({'run_id':run['run_id'],'question':question,'stage':state['status'],
                'tools':[dict(row) for row in await self.admin.fetch('SELECT tool_name,status,output_json FROM app_runtime.tool_calls WHERE run_id=$1',run['run_id'])],
                'model_outputs':[row['response_json'].get('merged') for row in await self.admin.fetch('SELECT response_json FROM app_runtime.model_attempts WHERE trace_id=$1 AND status=\'completed\' ORDER BY created_at',run['trace_id'])]})
        self.assertEqual(state['status'],'waiting_confirmation',state['state_json'])
        self.assertEqual(len(self.sent),before)
        action=dict(await self.admin.fetchrow('SELECT * FROM app_runtime.query_approvals WHERE run_id=$1',run['run_id']))
        self.assertIn('作者',action['payload_json']['target']['scope'])
        self.decide(headers,action)
        reply=await self.finish(processor,run)
        output=await self.admin.fetchval('SELECT result_json FROM app_runtime.query_approvals WHERE approval_id=$1',action['approval_id'])
        self.evidence.append({'run_id':run['run_id'],'trace_id':run['trace_id'],'sql':action['payload_json']['payload']['sql'],
            'result':output,'answer':reply['content'],'review':reply['protocol_json']['message']['answer_review']['status']})
        return output,reply

    async def test_shared_table_seed_and_permissions(self):
        """真实种子丰富/幂等，低职级技术员工可读赵六记录，非技术部不可见且不能查询共享表。"""
        stats=dict(await self.admin.fetchrow("SELECT count(*) AS records,count(DISTINCT author_username) AS authors,count(DISTINCT submitted_at::date) AS days,count(DISTINCT repository) AS repositories,count(DISTINCT build_status) AS build_states FROM demo_business.code_submissions"))
        self.assertGreater(stats['records'],150);self.assertEqual(stats['authors'],6);self.assertEqual(stats['days'],18);self.assertEqual(stats['repositories'],4);self.assertEqual(stats['build_states'],3)
        repeated=await seed(self.admin,self.bootstrap)
        self.assertIn('005_code_submissions.sql',repeated['skipped'])
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM demo_business.code_submissions'),stats['records'])
        headers=self.login()
        await self.admin.execute("UPDATE app_system.users SET grade=1,permission_version=permission_version+1 WHERE user_id='U2'")
        tech=await self.context(headers)
        self.assertIn('visible_code_submissions',allowed_catalog(tech))
        self.plan={'sql':'SELECT author_name,COUNT(*) AS total FROM demo_business.visible_code_submissions GROUP BY author_name ORDER BY author_name','params':[],'chart':{'type':'bar','x':'author_name','y':'total','unit':'次'}}
        output,_=await self.query('按人员统计全表代码提交',headers,self.processor())
        expected=[dict(row) for row in await self.admin.fetch('SELECT author_name,COUNT(*) AS total FROM demo_business.code_submissions GROUP BY author_name ORDER BY author_name')]
        self.assertEqual(output['rows'],expected);self.assertEqual(len(output['chart']['points']),6)
        self.plan={'sql':"WITH c AS (SELECT author_employee_id AS employee_id,author_name FROM demo_business.visible_code_submissions) SELECT author_name,COUNT(*) AS total FROM c WHERE employee_id=$1 GROUP BY author_name",'params':['E5'],'chart':{}}
        output,_=await self.query('查询赵六的代码提交',headers,self.processor())
        self.assertEqual(output['rows'][0]['author_name'],'赵六');self.assertGreater(output['rows'][0]['total'],0)
        hr=await self.context(self.login('lisi'))
        self.assertNotIn('visible_code_submissions',allowed_catalog(hr))
        with self.assertRaises(Exception):validate_sql('SELECT * FROM demo_business.visible_code_submissions',[],hr)
        # 原考勤本人边界仍生效，不能因共享代码表放宽敏感员工数据。
        with self.assertRaises(Exception):validate_sql("SELECT * FROM demo_business.visible_attendance_records WHERE employee_id='E5'",[],tech)
        self.evidence.append({'seed_stats':stats,'reseed_skipped':repeated['skipped']})

    async def test_logged_date_query_confirmation_and_execution(self):
        """重放实际日志中的DATE查询，验证确认前不执行、批准后真实PG统计18天207条。"""
        self.plan={'sql':"SELECT DATE(submitted_at) AS 日期,COUNT(*) AS 提交次数 FROM demo_business.visible_code_submissions WHERE submitted_at >= '2026-09-07' AND submitted_at < '2026-10-01' GROUP BY DATE(submitted_at) ORDER BY 日期",'params':[],
                   'chart':{'type':'line','x':'日期','y':'提交次数','title':'技术部每日代码提交次数','unit':'次'}}
        result,_=await self.query('统计2026年9月7日至9月30日技术部每天的代码提交次数，生成折线图。',self.login(),self.processor())
        expected=[dict(row) for row in await self.admin.fetch("SELECT submitted_at::date::text AS label,COUNT(*)::float AS value FROM demo_business.code_submissions GROUP BY submitted_at::date ORDER BY submitted_at::date")]
        self.assertEqual(result['chart']['points'],expected)
        self.assertEqual(len(result['chart']['points']),18)
        self.assertEqual(sum(point['value'] for point in result['chart']['points']),207)

    async def test_actual_model_personal_date_line(self):
        """实际主模型和审核模型解释本人筛选；真实PG按日期结果必须仅张三43条。"""
        actual=Settings()
        names=('primary_base_url','primary_api_key','primary_model','fallback_base_url','fallback_api_key','fallback_model','review_base_url','review_api_key','review_model','review_context_tokens')
        settings=self.settings.model_copy(update={key:getattr(actual,key) for key in names}|{'model_output_tokens':1600})
        self.audit=AuditRecorder(self.pool,'personal-code-real',tuple(getattr(actual,key).get_secret_value() for key in ('primary_api_key','fallback_api_key','review_api_key')))
        result,reply=await self.query('统计2026年9月7日至9月30日我本人的每日代码提交次数，生成折线图。',self.login(),self.processor(settings,ModelGateway(settings)))
        expected=[dict(row) for row in await self.admin.fetch("SELECT submitted_at::date::text AS label,COUNT(*)::float AS value FROM demo_business.code_submissions WHERE author_employee_id='E2' GROUP BY submitted_at::date ORDER BY submitted_at::date")]
        self.assertEqual(result['chart']['points'],expected)
        self.assertEqual(sum(point['value'] for point in result['chart']['points']),43)
        self.assertIn('executed_query',result)
        self.assertIn(43,result['returned_numeric_totals'].values())
        self.assertIn('43',reply['content'])
        self.assertEqual(reply['protocol_json']['message']['answer_review']['status'],'approved')

    async def test_actual_models_author_bar_and_date_line(self):
        """实际主模型/SQL生成/千问审核分别生成全员柱图和按日期折线，点集与直接PG汇总逐一相同。"""
        actual=Settings()
        names=('primary_base_url','primary_api_key','primary_model','fallback_base_url','fallback_api_key','fallback_model','review_base_url','review_api_key','review_model','review_context_tokens')
        settings=self.settings.model_copy(update={key:getattr(actual,key) for key in names}|{'model_output_tokens':1600})
        self.audit=AuditRecorder(self.pool,'code-submissions-real',tuple(getattr(actual,key).get_secret_value() for key in ('primary_api_key','fallback_api_key','review_api_key')))
        headers=self.login()
        cases=[('用query_business_data统计技术部共享代码提交记录表的全部作者在2026-09-07至2026-09-30的提交次数，按次数降序，生成柱状图。不要仅查我本人。','bar',
            "SELECT author_name AS label,COUNT(*)::float AS value FROM demo_business.code_submissions GROUP BY author_name ORDER BY COUNT(*) DESC,author_name"),
            ('用query_business_data统计技术部全体人员2026-09-07至2026-09-30每天代码提交次数，按北京时间日期升序，日期只要年月日，不要按人筛选，生成折线图。','line',
            "SELECT submitted_at::date::text AS label,COUNT(*)::float AS value FROM demo_business.code_submissions GROUP BY submitted_at::date ORDER BY submitted_at::date")]
        for question,kind,reference in cases:
            if os.environ.get('CODE_STATS_ONLY') and os.environ['CODE_STATS_ONLY']!=kind:continue
            processor=self.processor(settings,ModelGateway(settings))
            output,reply=await self.query(question,headers,processor)
            self.assertEqual(reply['protocol_json']['message']['answer_review']['status'],'approved')
            self.assertEqual(output['chart']['type'],kind);self.assertFalse(output['chart']['partial'])
            expected=[dict(row) for row in await self.admin.fetch(reference)]
            if kind=='bar':self.assertEqual({point['label']:point['value'] for point in output['chart']['points']},{point['label']:point['value'] for point in expected})
            else:self.assertEqual(output['chart']['points'],expected)
            self.assertEqual(sum(point['value'] for point in output['chart']['points']),await self.admin.fetchval('SELECT count(*) FROM demo_business.code_submissions'))


if __name__=='__main__':unittest.main()

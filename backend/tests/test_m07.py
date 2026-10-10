"""M07针对性验收：真实PG/生产图验证确认与数据库范围，协议故障夹具不冒充实际模型成功。"""
import asyncio
import json
import unittest
import traceback
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic import SecretStr

import test_m04 as base
from app.audit import AuditRecorder
from app.auth import auth_error
from app.business_sql import validate_sql, allowed_catalog, BusinessSqlService
from app.chat_service import ChatProcessor, RunWriter
from app.config import Settings
from app.database import connect_business
from app.ids import new_id
from app.model_gateway import ModelGateway
from app.rag_service import live_context
from app.sensitive_actions import ActionRequest, HumanSecondaryConfirmationHook
from app.timezones import beijing_now
from app.tool_registry import available_tools
from app.rag_agent import RagAgent


def call_packet():
    """无参数返回明确业务tool_calls夹具，用于恢复/确认边界；实际模型门槛另在RealM07执行。"""
    call={'index':0,'id':'business_call','type':'function','function':{'name':'query_business_data','arguments':'{"query":"查询本人业务数据并展示图表"}'}}
    return ('data: '+json.dumps({'choices':[{'delta':{'tool_calls':[call]},'finish_reason':'tool_calls'}]})+'\n\ndata: [DONE]\n\n').encode()


class SqlRules(unittest.TestCase):
    """固定AST/资格验证，不执行查询，不调用模型。"""
    def test_catalog_ast_and_explicit_overreach(self):
        """核对安全CTE/联表/参数以及写入、底表、系统、危险函数和他人过滤均不能进入确认。"""
        context=SimpleNamespace(can_query_business=True,is_admin=False,department_name='技术部',grade=3,employee_id='E2')
        valid=['SELECT status,COUNT(*) AS total FROM demo_business.visible_attendance_records GROUP BY status',
            'WITH a AS (SELECT employee_id FROM demo_business.visible_employees) SELECT a.employee_id FROM a',
            'SELECT e.name,a.attendance_date FROM demo_business.visible_employees e JOIN demo_business.visible_attendance_records a ON e.employee_id=a.employee_id',
            'SELECT name FROM demo_business.visible_employees WHERE employee_id=$1']
        valid.append('SELECT status,COUNT(*) AS total FROM demo_business.visible_attendance_records WHERE employee_id=$1 AND attendance_date>=$2 AND attendance_date<=$3 GROUP BY status')
        for sql in valid:
            canonical,tables=validate_sql(sql,['E2','2026-09-28','2026-10-02'] if '$3' in sql else ['E2'] if '$1' in sql else [],context)
            self.assertTrue(tables);self.assertTrue(canonical.endswith('LIMIT 201'))
        bad=['DELETE FROM demo_business.visible_employees','SELECT * FROM demo_business.employees',
            'SELECT * FROM app_system.users','SELECT pg_sleep(30) FROM demo_business.visible_employees',
            'SELECT public.count(*) FROM demo_business.visible_employees',
            "SELECT set_config('zhixing.action_token','fake',false) FROM demo_business.visible_employees",
            'WITH x AS (DELETE FROM demo_business.employees RETURNING *) SELECT * FROM x',
            'SELECT name FROM demo_business.visible_employees; SELECT name FROM demo_business.visible_employees',
            'SELECT password_hash FROM demo_business.visible_employees',
            'SELECT * FROM demo_business.visible_approval_records',
            "SELECT * FROM demo_business.visible_employees WHERE employee_id='E3'",
            "SELECT * FROM demo_business.visible_employees WHERE employee_id IN ('E2','E3')",
            'SELECT * FROM demo_business.visible_employees WHERE employee_id=$1']
        for sql in bad:
            with self.subTest(sql=sql),self.assertRaises(Exception):validate_sql(sql,['E3'] if '$1' in sql else [],context)
        denied=SimpleNamespace(can_query_business=False,is_admin=False)
        self.assertNotIn('query_business_data',[row['function']['name'] for row in available_tools(SimpleNamespace(can_query_business=False,can_use_knowledge=False),True)])
        with self.assertRaises(Exception):allowed_catalog(denied)


class M07Flow(unittest.IsolatedAsyncioTestCase):
    """独立PG和生产LangGraph/Hook/SQL执行器；主/审核模型仅作为可控边界夹具。"""
    login=base.ChatTests.login
    queue=base.ChatTests.queue

    async def asyncSetUp(self):
        """无参数创建独立测试库并启用演示账号业务资格，记录SQL实际发出次数及冻结内容。"""
        await base.ChatTests.asyncSetUp(self)
        await self.admin.execute("UPDATE app_system.users SET business_query_enabled=true WHERE user_id IN ('U1','U2','U3','U4')")
        self.settings=self.settings.model_copy(update={'primary_context_tokens':32768,'review_base_url':'https://review.test/v1','review_model':'fixture-review',
            'review_api_key':SecretStr('fixture-review-key'),'review_context_tokens':32768,'model_output_tokens':1200})
        self.plan={'sql':'SELECT status,COUNT(*) AS total FROM demo_business.visible_attendance_records GROUP BY status','params':[],
                   'chart':{'type':'bar','x':'status','y':'total','title':'本人考勤状态','unit':'条'}}
        self.sent=[];self.preparations=0;self.evidence=[]
        original=connect_business
        async def probed(settings):
            """接收业务连接配置，真实连接PG，仅在执行器fetch时记录查询；不替换业务结果。"""
            connection=await original(settings)
            class Probe:
                """代理真实业务连接，查询计数作为未确认零执行及恢复不重跑的证据。"""
                def __getattr__(self,name):
                    """接收成员名，转发真实连接能力。"""
                    return getattr(connection,name)
                async def fetch(self,sql,*params):
                    """接收实际冻结查询及绑定参数，记录后原样执行并返回真实PG数据。"""
                    self_outer.sent.append({'sql':sql,'params':list(params)})
                    return await connection.fetch(sql,*params)
            self_outer=self
            return Probe()
        self.probe=patch('app.business_sql.connect_business',probed);self.probe.start()
        original_run=RagAgent.run
        async def diagnostic(agent):
            """接收测试图实例，将内部异常定位写入本地验收记录，再交还生产错误处理，不改变执行结果。"""
            try:return await original_run(agent)
            except Exception:
                self.evidence.append({'diagnostic':traceback.format_exc()});raise
        self.diagnostic=patch.object(RagAgent,'run',diagnostic);self.diagnostic.start()

    async def asyncTearDown(self):
        """无参数保存无凭证摘要，清理仅本测试库与客户端；不会删除用户历史或业务主库。"""
        Path('/verification/M07').mkdir(parents=True,exist_ok=True)
        Path('/verification/M07/'+self._testMethodName+'.json').write_text(json.dumps({'sql_fetches':self.sent,'evidence':self.evidence},ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        self.probe.stop()
        self.diagnostic.stop()
        await base.ChatTests.asyncTearDown(self)

    async def context(self,headers):
        """接收真实测试登录头，解析服务端会话后从PG读取可信身份。"""
        from app.auth import digest,SESSION_COOKIE
        token=headers['Cookie'].split(SESSION_COOKIE+'=')[1].split(';')[0]
        row=await self.admin.fetchrow('SELECT user_id,session_id FROM app_system.auth_sessions WHERE token_hash=$1',digest(token))
        return await live_context(self.pool,row['user_id'],row['session_id'])

    def processor(self,settings=None,gateway=None):
        """接收可选真实模型配置，默认提供明确协议夹具，返回生产任务执行器。"""
        settings=settings or self.settings
        if gateway is None:
            async def handler(request):
                """分别模拟主模型、SQL准备和审核；业务查询永远使用真实PG执行器。"""
                body=json.loads(request.content)
                if body['model']==settings.review_model:return httpx.Response(200,content=base.packets('{"verdict":"approve","issues":[]}'))
                if body['messages'][0]['content'].startswith('你是只读PostgreSQL'):
                    self.preparations+=1
                    return httpx.Response(200,content=base.packets(json.dumps(self.plan,ensure_ascii=False)))
                if any(row['role']=='tool' for row in body['messages']):
                    observed=json.loads(next(row['content'] for row in reversed(body['messages']) if row['role']=='tool'))
                    text='查询已执行，范围为'+observed.get('scope','')+'，返回'+str(observed.get('row_count',0))+'行[1]。' if observed.get('citations') else observed['message']
                    return httpx.Response(200,content=base.packets(text))
                if '13加29' in body['messages'][-1]['content']:return httpx.Response(200,content=base.packets('42'))
                return httpx.Response(200,content=call_packet())
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler));self.clients.append(client)
            gateway=ModelGateway(settings,client)
        self.gateways.append(gateway)
        # 本组不请求RAG；真实RAG回归在既有目标方法中单独验证。
        rag=SimpleNamespace()
        return ChatProcessor(self.pool,settings,gateway,self.audit,rag=rag)

    async def wait_action(self,headers=None):
        """接收账号头，真实入队执行到工具内部确认边界，返回run、动作和可恢复执行器。"""
        headers=headers or self.login()
        queued=self.queue(headers,'查询本人考勤数据并展示柱状图')
        processor=self.processor();run=await processor.claim();await processor.process(run)
        current=await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',run['run_id'])
        self.assertEqual(current['status'],'waiting_confirmation',current['state_json'])
        self.assertIsNone(current['lease_owner'])
        action=dict(await self.admin.fetchrow('SELECT * FROM app_runtime.query_approvals WHERE run_id=$1',run['run_id']))
        self.evidence.append({'run_id':run['run_id'],'trace_id':run['trace_id'],'action_id':action['approval_id'],'status':current['status']})
        return run,action,processor,headers

    def decide(self,headers,action,decision='approve'):
        """接收本人决定，通过真实带CSRF接口提交，不从测试中直接伪造批准。"""
        response=self.api.post('/api/actions/'+action['approval_id']+'/decision',headers=headers,json={'decision':decision})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    async def finish(self,processor,run):
        """接收新worker执行器和run，真实领取已恢复任务并返回最终数据库状态与回复。"""
        claimed=await processor.claim();self.assertEqual(claimed['run_id'],run['run_id'])
        await processor.process(claimed)
        state=await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',run['run_id'])
        reply=await self.admin.fetchrow("SELECT content,protocol_json FROM app_runtime.messages WHERE run_id=$1 AND role='assistant'",run['run_id'])
        self.evidence.append({'run_id':run['run_id'],'final':state['status'],'error':state['state_json'].get('error')})
        self.assertEqual(state['status'],'completed',state['state_json'])
        return reply

    async def test_wait_approve_reject_isolation_and_exact_payload(self):
        """等待零SQL/释放槽、刷新恢复、本人CSRF和冻结载荷、重复回执一次执行、拒绝零执行。"""
        run,action,_,headers=await self.wait_action()
        self.assertEqual(self.sent,[])
        self.assertGreater(await self.admin.fetchval('SELECT count(*) FROM app_runtime.checkpoints WHERE run_id=$1',run['run_id']),0)
        self.assertEqual(self.api.get('/api/actions/'+action['approval_id'],headers=self.login('lisi')).status_code,404)
        self.assertEqual(self.api.post('/api/actions/'+action['approval_id']+'/decision',headers=headers,json={'decision':'approve','sql':'SELECT 1'}).status_code,422)
        self.assertEqual(self.api.get('/api/actions/'+action['approval_id'],headers=headers).json()['status'],'pending')
        # 当前任务等待时，另一用户仍能完成普通对话，不占据worker槽。
        self.queue(self.login('lisi'),'计算13加29')
        other=self.processor();claimed=await other.claim();await other.process(claimed)
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_runtime.runs WHERE run_id=$1',claimed['run_id']),'completed')
        context=await self.context(headers)
        factory=lambda value:RunWriter(self.pool,self.audit,value)
        await asyncio.gather(self.processor().hook.decide(action['approval_id'],'approve',context,factory),self.processor().hook.decide(action['approval_id'],'approve',context,factory))
        self.decide(headers,action)
        restarted=self.processor();reply=await self.finish(restarted,run)
        self.assertEqual(len(self.sent),1);self.assertEqual(self.sent[0]['sql'],action['payload_json']['payload']['sql'])
        self.assertEqual(self.sent[0]['params'],action['payload_json']['payload']['params'])
        output=await self.admin.fetchval('SELECT result_json FROM app_runtime.query_approvals WHERE approval_id=$1',action['approval_id'])
        self.assertEqual(output['rows'],[{'status':'present','total':5}]);self.assertEqual(output['chart']['points'],[{'label':'present','value':5.0}])
        self.assertEqual(reply['protocol_json']['message']['answer_review']['status'],'approved')
        self.assertEqual(self.preparations,1)
        self.assertEqual(self.api.get('/api/business/results/'+output['result_id'],headers=self.login('lisi')).status_code,404)
        before=len(self.sent)
        run,action,processor,headers=await self.wait_action();self.decide(headers,action,'reject');await self.finish(processor,run)
        self.assertEqual(len(self.sent),before)

    async def test_database_scope_views_and_explicit_denials(self):
        """真实PG视图在漏WHERE/联表/CTE下只读本人，工单/审批按角色过滤，管理员全量且仍逐次确认。"""
        cases=[('zhangsan','SELECT employee_id,name FROM demo_business.visible_employees',1),
            ('zhangsan','WITH a AS (SELECT e.employee_id,t.attendance_id FROM demo_business.visible_employees e JOIN demo_business.visible_attendance_records t ON e.employee_id=t.employee_id) SELECT * FROM a',5),
            ('zhangsan','SELECT ticket_id FROM demo_business.visible_tickets',3),
            ('lisi','SELECT approval_id FROM demo_business.visible_approval_records',3),
            ('wangwu','SELECT approval_id FROM demo_business.visible_approval_records',2),
            ('admin','SELECT employee_id FROM demo_business.visible_employees',5)]
        for username,sql,count in cases:
            self.plan={'sql':sql,'params':[],'chart':{}}
            run,action,processor,headers=await self.wait_action(self.login(username));self.decide(headers,action);await self.finish(processor,run)
            result=await self.admin.fetchval('SELECT result_json FROM app_runtime.query_approvals WHERE approval_id=$1',action['approval_id'])
            self.assertEqual(result['row_count'],count)
            if username=='zhangsan' and 'employee_id' in result['columns']:self.assertTrue(all(row['employee_id']=='E2' for row in result['rows']))
        connection=await connect_business(self.settings)
        try:
            self.assertEqual(await connection.fetch('SELECT * FROM demo_business.visible_employees'),[])
            with self.assertRaises(Exception):await connection.fetch('SELECT * FROM demo_business.employees')
        finally:await connection.close()
        # 只用受限本人视图形成超过200行的笛卡尔样例，验证截断而不导入额外数据。
        self.plan={'sql':'SELECT a.attendance_id FROM demo_business.visible_attendance_records a CROSS JOIN demo_business.visible_attendance_records b CROSS JOIN demo_business.visible_attendance_records c CROSS JOIN demo_business.visible_departments d','params':[],'chart':{}}
        run,action,processor,headers=await self.wait_action();self.decide(headers,action);await self.finish(processor,run)
        output=await self.admin.fetchval('SELECT result_json FROM app_runtime.query_approvals WHERE approval_id=$1',action['approval_id'])
        self.assertEqual(output['row_count'],200);self.assertTrue(output['truncated'])

    async def test_expiry_revoke_cancel_recovery_and_receipts(self):
        """集中过期/撤权/取消和三个中断点：批准未执行可恢复，执行未知不重跑，保存结果只复用。"""
        for mode in ('expired','revoked','cancelled','before_execute','unknown','saved','lease_lost'):
            with self.subTest(mode=mode):
                before=len(self.sent)
                run,action,processor,headers=await self.wait_action()
                if mode=='expired':
                    await self.admin.execute('UPDATE app_runtime.query_approvals SET expires_at=$1 WHERE approval_id=$2',beijing_now()-timedelta(seconds=1),action['approval_id'])
                    await processor.recover();await self.finish(processor,run);self.assertEqual(len(self.sent),before)
                elif mode=='revoked':
                    await self.admin.execute("UPDATE app_system.users SET business_query_enabled=false,permission_version=permission_version+1 WHERE user_id='U2'")
                    self.decide(headers,action);await self.finish(processor,run);self.assertEqual(len(self.sent),before)
                    await self.admin.execute("UPDATE app_system.users SET business_query_enabled=true WHERE user_id='U2'")
                elif mode=='cancelled':
                    response=self.api.post('/api/runs/'+run['run_id']+'/cancel',headers=headers);self.assertEqual(response.status_code,200)
                    self.assertEqual(await self.admin.fetchval('SELECT status FROM app_runtime.runs WHERE run_id=$1',run['run_id']),'cancelled');self.assertEqual(len(self.sent),before)
                else:
                    self.decide(headers,action)
                    claimed=await processor.claim()
                    if mode=='lease_lost':
                        await self.admin.execute("UPDATE app_runtime.runs SET lease_owner='new-test-worker' WHERE run_id=$1",run['run_id'])
                        with self.assertRaises(Exception):await processor.business.execute(action['approval_id'],await self.context(headers),claimed)
                        self.assertEqual(len(self.sent),before)
                    if mode=='unknown':
                        await self.admin.execute("UPDATE app_runtime.query_approvals SET decision='executing',consumed_at=now() WHERE approval_id=$1",action['approval_id'])
                    if mode=='saved':
                        result=await processor.business.execute(action['approval_id'],await self.context(headers),claimed)
                        self.assertTrue(result['result_id']);self.assertEqual(len(self.sent),before+1)
                    await self.admin.execute('UPDATE app_runtime.runs SET lease_until=$1 WHERE run_id=$2',beijing_now()-timedelta(seconds=1),run['run_id'])
                    restarted=self.processor();await restarted.recover();await self.finish(restarted,run)
                    self.assertEqual(len(self.sent),before+(0 if mode=='unknown' else 1))
                    if mode=='unknown':self.assertEqual(await self.admin.fetchval('SELECT decision FROM app_runtime.query_approvals WHERE approval_id=$1',action['approval_id']),'unknown')

    async def test_generic_hook_denial_and_payload_change(self):
        """无外部副作用的消息动作复用同一Hook展示收件人/正文；拒绝/改载荷/未知动作不能执行。"""
        run,sql_action,processor,headers=await self.wait_action();self.decide(headers,sql_action,'reject');await self.finish(processor,run)
        queued=self.queue(headers,'通用消息动作确认契约测试');claimed=await processor.claim()
        call_id=new_id()
        await self.admin.execute("INSERT INTO app_runtime.tool_calls(call_id,run_id,tool_name,input_json,status,idempotency_key,provider_call_id,protocol_message) VALUES($1,$2,'message_fixture','{}','running',$3,'message_fixture','{}')",call_id,claimed['run_id'],new_id())
        context=await self.context(headers)
        request=ActionRequest(action_type='message.send',tool_name='message_fixture',purpose='无外部发送的确认测试',target={'channel':'本地测试','recipients':['recipient@example.invalid']},payload={'text':'这段文本仅用于确认窗口验收，不发送'})
        action=await processor.hook.prepare(request,context,claimed,call_id)
        await RunWriter(self.pool,self.audit,claimed).suspend(action['approval_id'])
        detail=self.api.get('/api/actions/'+action['approval_id'],headers=headers).json()
        self.assertEqual(detail['request']['payload']['text'],request.payload['text'])
        self.decide(headers,action,'reject')
        claimed=await processor.claim()
        async def validator(action,context):
            """验证本地无发送执行器的动作类型，仅用于消费批准边界，不触发任何外部操作。"""
            self.assertEqual(action.action_type,'message.send')
        with self.assertRaises(Exception):await processor.hook.consume(action['approval_id'],context,claimed,validator)
        with self.assertRaises(Exception):processor.hook.policy('unknown.action')
        await RunWriter(self.pool,self.audit,claimed).finish('cancelled',{'code':'TEST_ENDED','message':'本地动作测试结束'})
        run,action,processor,headers=await self.wait_action();self.decide(headers,action)
        await self.admin.execute("UPDATE app_runtime.query_approvals SET payload_json=jsonb_set(payload_json,'{payload,sql}','\"SELECT 1\"'::jsonb) WHERE approval_id=$1",action['approval_id'])
        before=len(self.sent);await self.finish(processor,run);self.assertEqual(len(self.sent),before)


class RealM07(unittest.IsolatedAsyncioTestCase):
    """实际主模型/SQL生成/独立审核+PG查询，所有数据为隔离库的外置虚构种子。"""
    asyncSetUp=M07Flow.asyncSetUp
    asyncTearDown=M07Flow.asyncTearDown
    login=M07Flow.login
    queue=M07Flow.queue
    processor=M07Flow.processor
    decide=M07Flow.decide
    finish=M07Flow.finish
    async def test_actual_model_sql_confirmation_chart(self):
        """实际DeepSeek选择业务工具并生成SQL，确认后真实PG结果由千问核验，柱图点与结果一致。"""
        actual=Settings()
        fields=('primary_base_url','primary_api_key','primary_model','fallback_base_url','fallback_api_key','fallback_model','review_base_url','review_api_key','review_model','review_context_tokens')
        settings=self.settings.model_copy(update={key:getattr(actual,key) for key in fields}|{'model_output_tokens':1500})
        self.audit=AuditRecorder(self.pool,'M07-real',tuple(getattr(actual,key).get_secret_value() for key in ('primary_api_key','fallback_api_key','review_api_key')))
        headers=self.login();self.queue(headers,'请用query_business_data查询本人2026-09-28至2026-10-02的考勤，按status统计条数，展示柱状图，标明本人范围。')
        processor=self.processor(settings,ModelGateway(settings));run=await processor.claim();await processor.process(run)
        state=await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',run['run_id'])
        self.evidence.append({'run_id':run['run_id'],'trace_id':run['trace_id'],'stage':state['status'],
            'tools':[dict(row) for row in await self.admin.fetch('SELECT tool_name,status,output_json FROM app_runtime.tool_calls WHERE run_id=$1',run['run_id'])],
            'model_outputs':[dict(row) for row in await self.admin.fetch('SELECT model_role,model,response_json FROM app_runtime.model_attempts WHERE trace_id=$1 ORDER BY created_at',run['trace_id'])]})
        self.assertEqual(state['status'],'waiting_confirmation',state['state_json']);self.assertEqual(self.sent,[])
        action=dict(await self.admin.fetchrow('SELECT * FROM app_runtime.query_approvals WHERE run_id=$1',run['run_id']))
        self.decide(headers,action);reply=await self.finish(self.processor(settings,ModelGateway(settings)),run)
        result=await self.admin.fetchval('SELECT result_json FROM app_runtime.query_approvals WHERE approval_id=$1',action['approval_id'])
        self.assertEqual(len(result['rows']),1)
        self.assertTrue(result['chart']);self.assertEqual(result['chart']['points'][0]['label'],'present');self.assertEqual(result['chart']['points'][0]['value'],5)
        self.assertIn('本人',reply['content']);self.assertEqual(reply['protocol_json']['message']['answer_review']['status'],'approved')
        self.evidence.append({'run_id':run['run_id'],'trace_id':run['trace_id'],'action_id':action['approval_id'],'result':result,'answer':reply['content'],
            'roles':[dict(row) for row in await self.admin.fetch('SELECT model_role,model,status FROM app_runtime.model_attempts WHERE trace_id=$1',run['trace_id'])]})


if __name__=='__main__':unittest.main()

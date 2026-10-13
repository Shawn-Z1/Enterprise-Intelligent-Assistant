"""M12新增审计接口的定向验收，使用真实隔离PG，不重复历史阶段矩阵。"""
import json
import unittest
import tempfile
from pathlib import Path

import test_m04 as base
from app.audit import Usage
from app.ids import new_id
from app.maintenance import validate,digest,quarantine


class AuditTests(unittest.IsolatedAsyncioTestCase):
    """验证只读权限、过滤、用量来源、分页及受控正文/导出。"""
    asyncSetUp=base.ChatTests.asyncSetUp
    asyncTearDown=base.ChatTests.asyncTearDown
    login=base.ChatTests.login
    queue=base.ChatTests.queue

    async def test_access_steps_usage_and_export(self):
        """新增真实任务及分层审计，管理员可检查、成员/撤权拒绝，原记录不可改且凭证不外泄。"""
        run=self.queue(self.login(),content='M12独立验收')
        trace=await self.admin.fetchval('SELECT trace_id FROM app_runtime.runs WHERE run_id=$1',run['run_id'])
        span=new_id()
        await self.admin.execute("INSERT INTO app_runtime.model_attempts(attempt_id,trace_id,span_id,model_role,provider,model,status,request_json) VALUES($1,$2,$1,'primary','fixture','fixture-model','completed','{}')",span,trace)
        for kind in ('model.chunk','model.response'):
            await self.audit.append(trace,kind,{'usage':{'prompt_tokens_details':{'cached_tokens':12}},'api_key':'unsafe-fixture','auth_session_id':'secret-id','code':'<script>window.m12Executed=true</script>'},
                span_id=span,usage=Usage(source='reported',input_tokens=30,output_tokens=4,total_tokens=34,model='fixture-model'),duration_ms=25)
        unknown=await self.audit.append(trace,'model.failed',{'message':'不可用'},usage=Usage())
        await self.admin.execute("INSERT INTO app_runtime.model_attempts(attempt_id,trace_id,span_id,model_role,provider,model,status,request_json) VALUES($1,$2,$1,'fallback','fixture','fixture-failed','failed','{}')",unknown['span_id'],trace)
        await self.audit.append(trace,'rag.embedding',{},usage=Usage(source='estimated',input_tokens=9,total_tokens=9,method='fixture estimate',model='local-model'))
        headers=self.login('admin')
        listed=self.api.get('/api/admin/audit/traces',params={'trace':trace,'user':'zhangsan'},headers=headers)
        self.assertEqual(listed.status_code,200,listed.text);self.assertEqual(listed.json()['total'],1)
        detail=self.api.get('/api/admin/audit/traces/'+trace,headers=headers)
        self.assertEqual(detail.status_code,200,detail.text)
        self.assertEqual(len(detail.json()['usage']),3)
        unavailable=next(row for row in detail.json()['usage'] if row['usage_source']=='unavailable')
        self.assertIsNone(unavailable['total_tokens'])
        reported=next(row for row in detail.json()['usage'] if row['usage_source']=='reported')
        self.assertEqual(reported['total_tokens'],34);self.assertEqual(reported['cached_tokens'],12)
        events=self.api.get('/api/admin/audit/traces/'+trace+'/events',params={'limit':2},headers=headers)
        self.assertEqual(events.status_code,200,events.text);self.assertTrue(events.json()['has_more'])
        self.assertNotIn('model.chunk',[row['event_type'] for row in events.json()['items']])
        second=self.api.get('/api/admin/audit/traces/'+trace+'/events',params={'after':events.json()['next_after']},headers=headers)
        self.assertTrue(all(row['event_id']>events.json()['next_after'] for row in second.json()['items']))
        export=self.api.get('/api/admin/audit/traces/'+trace+'/export',params={'limit':1},headers=headers)
        self.assertEqual(export.status_code,200,export.text);self.assertFalse(export.json()['complete'])
        self.assertIsNotNone(export.json()['next_after'])
        full=self.api.get('/api/admin/audit/traces/'+trace+'/export',headers=headers)
        self.assertEqual(full.status_code,200,full.text);self.assertTrue(full.json()['complete'])
        self.assertNotIn('unsafe-fixture',full.text);self.assertNotIn('secret-id',full.text)
        self.assertIn('<script>',full.text)
        data=self.api.get('/api/admin/audit/events/'+str(unknown['event_id']),headers=headers).json()
        self.assertEqual(data['data']['message'],'不可用')
        for verb in ('post','delete'):
            self.assertEqual(getattr(self.api,verb)('/api/admin/audit/traces/'+trace,headers=headers).status_code,405)
        with self.assertRaises(Exception):await self.admin.execute("UPDATE app_audit.events SET raw_json='{}' WHERE event_id=$1",unknown['event_id'])
        member=self.login()
        for suffix in ('/traces','/traces/'+trace,'/events/'+str(unknown['event_id']),'/traces/'+trace+'/export'):
            self.assertEqual(self.api.get('/api/admin/audit'+suffix,headers=member).status_code,403)
        revoked=self.login('admin');await self.admin.execute("UPDATE app_system.users SET role='member' WHERE username='admin'")
        self.assertEqual(self.api.get('/api/admin/audit/traces',headers=revoked).status_code,403)
        target=Path('/verification/M12');target.mkdir(parents=True,exist_ok=True)
        (target/'audit-api.json').write_text(json.dumps({'passed':True,'trace_id':trace,'reported_total':34,'estimated_separate':True,'member_denied':True,'revoked_admin_denied':True,'immutable':True,'partial_export_labelled':True}),encoding='utf-8')

    async def test_limits_and_parameter_binding(self):
        """验证无匹配过滤不改变SQL语义、窗口限额/时间校验，以及大事件只按需导出原文。"""
        headers=self.login('admin');trace=new_id()
        row=await self.audit.append(trace,'fixture.large',{'text':'资料'*140000,'message':'password=unsafe-fixture'})
        response=self.api.get('/api/admin/audit/events/'+str(row['event_id']),headers=headers)
        self.assertEqual(response.status_code,200,response.text);self.assertTrue(response.json()['too_large']);self.assertIsNone(response.json()['data'])
        exported=self.api.get('/api/admin/audit/events/'+str(row['event_id'])+'/export',headers=headers)
        self.assertEqual(exported.status_code,200,exported.text[:300]);self.assertEqual(len(exported.json()['events'][0]['raw_json']['text']),280000)
        self.assertNotIn('unsafe-fixture',exported.text)
        bound=self.api.get('/api/admin/audit/traces',params={'tasks_only':False,'user':"' OR true --"},headers=headers)
        self.assertEqual(bound.status_code,200,bound.text);self.assertEqual(bound.json()['total'],0)
        for params in ({'limit':51},{'page':0},{'since':'2026-10-13T00:00:00+08:00','until':'2026-10-12T00:00:00+08:00'}):
            self.assertEqual(self.api.get('/api/admin/audit/traces',params=params,headers=headers).status_code,422)

    async def test_restored_unknown_actions_are_not_replayed(self):
        """真实隔离库验证恢复后旧确认/沙箱/会话均失效，原消息和审计保留；不执行夹具代码。"""
        run=self.queue(self.login());call=new_id();job=new_id();approval=new_id()
        await self.admin.execute("INSERT INTO app_runtime.tool_calls(call_id,run_id,tool_name,input_json,status,idempotency_key) VALUES($1,$2,'query_business_data','{}','waiting_confirmation',$3)",call,run['run_id'],call)
        await self.admin.execute("INSERT INTO app_runtime.query_approvals(approval_id,user_id,run_id,call_id,query_fingerprint,permission_version,decision,expires_at) VALUES($1,'U2',$2,$3,'fixture',1,'executing',now()+interval '30 minutes')",approval,run['run_id'],call)
        await self.admin.execute("""INSERT INTO app_runtime.sandbox_jobs(job_id,run_id,user_id,idempotency_key,operation,status,input_json,node_policy,request_fingerprint,resources_released,expires_at)
            VALUES($1,$2,'U2',$3,'execute','running','{"code":"fixture must never execute"}','{}','fixture',false,now()+interval '5 minutes')""",job,run['run_id'],job)
        before=await self.admin.fetchval('SELECT count(*) FROM app_audit.events')
        self.assertEqual(await quarantine(self.admin,'source-bucket','target-bucket'),1)
        self.assertEqual(await self.admin.fetchval('SELECT status FROM app_runtime.runs WHERE run_id=$1',run['run_id']),'interrupted')
        self.assertEqual(await self.admin.fetchval('SELECT decision FROM app_runtime.query_approvals WHERE approval_id=$1',approval),'invalidated')
        row=await self.admin.fetchrow('SELECT status,cancel_requested,resources_released FROM app_runtime.sandbox_jobs WHERE job_id=$1',job)
        self.assertEqual(dict(row),{'status':'unknown','cancel_requested':True,'resources_released':True})
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_audit.events'),before)
        self.assertGreater(await self.admin.fetchval('SELECT count(*) FROM app_runtime.messages WHERE run_id=$1',run['run_id']),0)
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_system.auth_sessions WHERE revoked_at IS NULL'),0)


class BackupBoundaries(unittest.TestCase):
    """只验证本阶段备份清单损坏边界，不通过人为损坏真实备份来测试。"""
    def test_corrupt_archive_and_unsafe_object_filename(self):
        """在临时目录验证SHA篡改及路径穿越被拒绝；不连接正式库或执行档案。"""
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);archive=root/'database.dump';archive.write_bytes(b'fixture archive')
            manifest={'format':1,'database_sha256':digest(archive),'objects':[]}
            (root/'manifest.json').write_text(json.dumps(manifest))
            self.assertEqual(validate(root)['format'],1)
            archive.write_bytes(b'corrupted fixture')
            with self.assertRaises(ValueError):validate(root)
            manifest['database_sha256']=digest(archive);manifest['objects']=[{'key':'fixture','file':'../elsewhere'}]
            (root/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):validate(root)

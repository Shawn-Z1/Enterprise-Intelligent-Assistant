"""会话审计改版定向测试：多轮归并、原事件定位、长消息与管理员权限。"""
import unittest
import test_m04 as base
from app.audit import Usage
from app.ids import new_id


class ConversationAuditTests(unittest.IsolatedAsyncioTestCase):
    """使用实际隔离PG与API，不执行模型/沙箱，不重复先前阶段压力矩阵。"""
    asyncSetUp=base.ChatTests.asyncSetUp
    asyncTearDown=base.ChatTests.asyncTearDown
    login=base.ChatTests.login
    queue=base.ChatTests.queue

    async def finished(self,headers,question,answer,conversation=None):
        """接收虚构问答和可选同一会话，实际API排队后保存确定性展示夹具，返回run与trace。"""
        queued=self.queue(headers,question,conversation=conversation)
        run=dict(await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',queued['run_id']))
        await self.admin.execute("UPDATE app_runtime.runs SET status='completed' WHERE run_id=$1",run['run_id'])
        await self.admin.execute("INSERT INTO app_runtime.messages(message_id,conversation_id,user_id,role,content,run_id) VALUES($1,$2,$3,'assistant',$4,$5)",new_id(),run['conversation_id'],run['user_id'],answer,run['run_id'])
        return run

    async def test_conversation_grouping_rounds_timeline_and_permissions(self):
        """不同用户会话可分页，同一会话两问答不拆条目；进度、工具和后台事件只归本轮且保留可读事实。"""
        member=self.login()
        first=await self.finished(member,'展示测试：整理会议纪要','## 已整理\n- 演示待办')
        second=await self.finished(member,'展示测试：导出成PDF','已生成演示文件。',first['conversation_id'])
        third=await self.finished(self.login('lisi'),'展示测试：查差旅规定','演示规定，不对应真实政策。')
        await self.audit.append(new_id(),'service.started',{'fixture':True})
        await self.audit.append(first['trace_id'],'tool.request',{'fixture':True,'message':'第一轮工具'},user_id=first['user_id'],run_id=first['run_id'])
        response=await self.audit.append(second['trace_id'],'model.response',{'fixture':True,'slot':'primary','usage':None},
            user_id=second['user_id'],run_id=second['run_id'],usage=Usage(source='estimated',input_tokens=11,output_tokens=2,total_tokens=13,method='展示夹具非真实调用',model='fixture'))
        background=await self.audit.append(second['trace_id'],'memory.no_signal',{'fixture':True,'api_key':'never-show-fixture'},user_id=second['user_id'],run_id=second['run_id'])
        await self.admin.execute("INSERT INTO app_runtime.run_events(run_id,trace_id,sequence,event_type,payload) VALUES($1,$2,2,'context.cache',$3)",second['run_id'],second['trace_id'],{'message':'已读取会话工作上下文','cache_status':'hit'})
        admin=self.login('admin')
        listed=self.api.get('/api/admin/audit/conversations',params={'limit':1},headers=admin)
        self.assertEqual(listed.status_code,200,listed.text);self.assertEqual(listed.json()['total'],2)
        second_page=self.api.get('/api/admin/audit/conversations',params={'limit':1,'page':2},headers=admin).json()
        self.assertNotEqual(listed.json()['items'][0]['conversation_id'],second_page['items'][0]['conversation_id'])
        mine=self.api.get('/api/admin/audit/conversations',params={'user':'zhangsan'},headers=admin).json()['items']
        self.assertEqual(len(mine),1);self.assertEqual(mine[0]['turns_count'],2)
        turns=self.api.get('/api/admin/audit/conversations/'+first['conversation_id']+'/turns',params={'limit':1},headers=admin)
        self.assertEqual(turns.status_code,200,turns.text);self.assertTrue(turns.json()['has_more'])
        first_turn=turns.json()['items'][0];self.assertEqual(first_turn['user_message']['content'],'展示测试：整理会议纪要')
        self.assertIn('已整理',first_turn['assistant_message']['content']);self.assertEqual(first_turn['run_id'],first['run_id'])
        later=self.api.get('/api/admin/audit/conversations/'+first['conversation_id']+'/turns',params={'after':turns.json()['next_after']},headers=admin).json()['items']
        self.assertEqual(len(later),1);self.assertEqual(later[0]['run_id'],second['run_id']);self.assertEqual(later[0]['turn_number'],2)
        detail=self.api.get('/api/admin/audit/runs/'+second['run_id'],headers=admin)
        self.assertEqual(detail.status_code,200,detail.text);self.assertEqual(detail.json()['usage'][0]['total_tokens'],13)
        events=self.api.get('/api/admin/audit/runs/'+second['run_id']+'/events',headers=admin)
        self.assertEqual(events.status_code,200,events.text);items=events.json()['items']
        self.assertTrue(any(row['title']=='读取会话工作上下文' and row['source']=='run_event' for row in items))
        self.assertTrue(any(row['group']=='background' and row['event_id']==background['event_id'] for row in items))
        self.assertFalse(any(row['event_type']=='tool.request' for row in items))
        self.assertTrue(any(row['event_id']==response['event_id'] and '主模型' in row['title'] for row in items))
        progress=next(row for row in items if row['source']=='run_event' and row['event_type']=='context.cache')
        body=self.api.get('/api/admin/audit/run-events/'+str(progress['event_id']),headers=admin).json()
        self.assertEqual(body['data']['cache_status'],'hit')
        export=self.api.get('/api/admin/audit/traces/'+second['trace_id']+'/export',headers=admin)
        self.assertNotIn('never-show-fixture',export.text)
        for suffix in ('/conversations','/conversations/'+first['conversation_id']+'/turns','/runs/'+second['run_id'],'/runs/'+second['run_id']+'/events','/run-events/'+str(progress['event_id'])):
            self.assertEqual(self.api.get('/api/admin/audit'+suffix,headers=member).status_code,403)
        bound=self.api.get('/api/admin/audit/conversations',params={'user':"' OR true --"},headers=admin).json()
        self.assertEqual(bound['total'],0)
        self.assertEqual(self.api.get('/api/admin/audit/conversations',params={'limit':13},headers=admin).status_code,422)
        self.assertEqual(self.api.get('/api/admin/audit/runs/'+third['run_id'],headers=admin).status_code,200)

    async def test_legacy_round_pairing_and_original_long_message(self):
        """旧无run历史按相邻用户消息配对，长预览明确截断且能读全文/导出；正文不执行且凭证遮蔽。"""
        member=self.login();response=self.api.post('/api/conversations',json={},headers=member);conversation=response.json()['conversation_id']
        ids=[]
        for role,content in [('user','甲'*15000),('assistant','旧回复一'),('user','旧问题二'),('assistant','旧回复二')]:
            ident=new_id();ids.append(ident)
            await self.admin.execute('INSERT INTO app_runtime.messages(message_id,conversation_id,user_id,role,content) VALUES($1,$2,$3,$4,$5)',ident,conversation,'U2',role,content)
        admin=self.login('admin')
        turns=self.api.get('/api/admin/audit/conversations/'+conversation+'/turns',headers=admin)
        self.assertEqual(turns.status_code,200,turns.text);rows=turns.json()['items'];self.assertEqual(len(rows),2)
        self.assertTrue(rows[0]['user_message']['truncated']);self.assertIsNone(rows[0]['run_id'])
        self.assertEqual(rows[0]['assistant_message']['content'],'旧回复一');self.assertEqual(rows[1]['assistant_message']['content'],'旧回复二')
        full=self.api.get('/api/admin/audit/messages/'+ids[0],headers=admin).json();self.assertEqual(len(full['content']),15000)
        exported=self.api.get('/api/admin/audit/messages/'+ids[0]+'/export',headers=admin)
        self.assertEqual(exported.status_code,200,exported.text[:200]);self.assertEqual(len(exported.json()['content']),15000)
        self.assertEqual(self.api.get('/api/admin/audit/messages/'+ids[0],headers=member).status_code,403)
        self.assertEqual(self.api.get('/api/admin/audit/messages/'+ids[0]+'/export',headers=member).status_code,403)
        self.assertEqual(self.api.get('/api/admin/audit/conversations',params={'since':'2026-10-14','until':'2026-10-13'},headers=admin).status_code,422)

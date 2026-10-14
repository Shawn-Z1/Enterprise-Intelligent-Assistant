"""管理页面改版定向验收：真实隔离PG/API，不调用云模型、沙箱或生产数据。"""
import json
import unittest

import test_m04 as base
from app.ids import new_id
from app.knowledge_service import fingerprint


class ManagementCatalog(unittest.IsolatedAsyncioTestCase):
    """验证目录分类/分页/归属隔离、失效记录清理及管理员职员筛选。"""
    asyncSetUp=base.ChatTests.asyncSetUp
    asyncTearDown=base.ChatTests.asyncTearDown
    login=base.ChatTests.login

    async def entry(self,owner='U2',status='retrievable',name='目录验收.txt'):
        """接收所有者、状态和名称，在独立库写入元数据夹具；可检索状态同时创建真实文档状态，不伪造工具执行。"""
        row={'application_id':new_id(),'creator_user_id':owner,'title':name,'filename':name,'file_format':'txt','byte_size':20,
             'original_sha256':'a'*64,'object_key':'management-fixture/'+new_id(),'allowed_department_ids':['D2'],
             'minimum_grade':3,'acl_snapshot':{'allowed_department_ids':['D2'],'minimum_grade':3},'source_ids':[]}
        row['fingerprint']=fingerprint(row)
        await self.admin.execute('''INSERT INTO app_knowledge.applications(application_id,creator_user_id,title,filename,file_format,byte_size,
            original_sha256,object_key,allowed_department_ids,minimum_grade,acl_snapshot,source_ids,fingerprint,status,parse_status)
            VALUES($1,$2,$3,$3,'txt',20,$4,$5,ARRAY['D2']::varchar[],3,$6::jsonb,'[]',$7,$8,'completed')''',
            row['application_id'],owner,name,row['original_sha256'],row['object_key'],json.dumps(row['acl_snapshot']),row['fingerprint'],status)
        if status=='retrievable':
            await self.admin.execute('''INSERT INTO app_knowledge.documents(document_id,application_id,creator_user_id,status,acl_snapshot,approved_fingerprint)
                VALUES($1,$2,$3,'retrievable',$4::jsonb,$5)''',new_id(),row['application_id'],owner,json.dumps(row['acl_snapshot']),row['fingerprint'])
        return row['application_id']

    async def test_catalog_filters_pagination_and_staff_roles(self):
        """真实API验证先过滤后分页、成员自己的进度与统计、管理员审核分类及职员筛选无越权。"""
        for index in range(12):
            await self.entry(name=f'目录验收-{index:02}.txt')
        other=await self.entry(owner='U3',name='目录验收-他人.txt')
        for state in ('pending_review','parsing','failed','invalidated','withdrawn','rejected'):
            await self.entry(status=state,name='目录验收-'+state+'.txt')
        member=self.login();admin=self.login('admin')
        own=self.api.get('/api/knowledge/applications/catalog?q=目录验收&limit=10&page=2',headers=member)
        self.assertEqual(own.status_code,200,own.text)
        data=own.json();self.assertEqual((data['total'],len(data['items']),data['page']),(12,2,2))
        self.assertTrue(all(row['creator_user_id']=='U2' and row['status']=='retrievable' for row in data['items']))
        self.assertEqual(data['counts']['progress'],2);self.assertEqual(data['counts']['history'],3);self.assertNotIn('review',data['counts'])
        first=self.api.get('/api/knowledge/applications/catalog?q=目录验收&limit=10',headers=member).json()
        self.assertFalse({row['application_id'] for row in first['items']}&{row['application_id'] for row in data['items']})
        self.assertEqual(self.api.get('/api/knowledge/applications/catalog?scope=all',headers=member).status_code,403)
        self.assertEqual(self.api.get('/api/knowledge/applications/catalog?category=review',headers=member).status_code,403)
        self.assertEqual(self.api.get('/api/knowledge/applications/'+other,headers=member).status_code,404)
        public=self.api.get('/api/knowledge/applications/catalog?scope=all&q=目录验收',headers=admin).json();self.assertEqual(public['total'],13)
        review=self.api.get('/api/knowledge/applications/catalog?scope=all&category=review&q=目录验收',headers=admin).json();self.assertEqual(review['total'],1)
        progress=self.api.get('/api/knowledge/applications/catalog?category=progress&q=目录验收',headers=member).json();self.assertEqual(progress['total'],2)
        self.assertEqual(self.api.get('/api/knowledge/applications/catalog?q=%27%20OR%201=1--',headers=admin).json()['total'],0)
        self.assertEqual(self.api.get('/api/knowledge/applications/catalog?limit=51',headers=admin).status_code,422)
        self.assertEqual(self.api.get('/api/admin/users/catalog',headers=member).status_code,403)
        staff=self.api.get('/api/admin/users/catalog?q=zhangsan&access=business&limit=1',headers=admin)
        self.assertEqual(staff.status_code,200,staff.text);self.assertEqual(staff.json()['total'],1)
        self.assertEqual(staff.json()['items'][0]['user_id'],'U2');self.assertTrue(staff.json()['items'][0]['can_query_business'])
        self.assertNotIn('password_hash',staff.json()['items'][0])
        filtered=self.api.get('/api/admin/users/catalog?q=zhangsan&department_id=D1',headers=admin).json();self.assertEqual(filtered['total'],0)
        assignment={'department_id':'D2','grade':3,'position':'管理目录验收岗位','business_query_enabled':True,
                    'expected_permission_version':staff.json()['items'][0]['permission_version']}
        saved=self.api.patch('/api/admin/users/U2',json=assignment,headers=admin);self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(self.api.patch('/api/admin/users/U2',json=assignment,headers=admin).status_code,409)
        self.assertEqual(self.api.patch('/api/admin/users/U2',json=assignment,headers=member).status_code,403)
        self.assertEqual(self.api.get('/api/admin/users/catalog?q=zhangsan',headers=admin).json()['items'][0]['position'],'管理目录验收岗位')

    async def test_remove_record_keeps_audit_and_rejects_active_or_foreign(self):
        """真实事务验证失效记录清理幂等、后台任务阻止清理、跨用户拒绝，原件元数据和不可变审计均保留。"""
        inactive=await self.entry(status='invalidated',name='清理验收.txt')
        active=await self.entry()
        member=self.login();other=self.login('lisi');admin=self.login('admin')
        path='/api/knowledge/applications/'+inactive+'/remove-record'
        self.assertEqual(self.api.post(path,headers=other).status_code,404)
        self.assertEqual(self.api.post('/api/knowledge/applications/'+active+'/remove-record',headers=member).status_code,409)
        job=new_id()
        await self.admin.execute("INSERT INTO app_knowledge.jobs(job_id,application_id,status,job_type) VALUES($1,$2,'pending','delete')",job,inactive)
        self.assertEqual(self.api.post(path,headers=member).status_code,409)
        await self.admin.execute("UPDATE app_knowledge.jobs SET status='completed' WHERE job_id=$1",job)
        response=self.api.post(path,headers=member);self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.api.post(path,headers=member).status_code,200)
        self.assertEqual(self.api.get('/api/knowledge/applications/'+inactive,headers=member).status_code,404)
        self.assertEqual(self.api.get('/api/knowledge/applications/'+inactive+'/preview',headers=member).status_code,404)
        self.assertNotIn(inactive,[row['application_id'] for row in self.api.get('/api/knowledge/applications?scope=all',headers=admin).json()])
        self.assertEqual(self.api.get('/api/knowledge/applications/catalog?scope=all&category=history&q=清理验收',headers=admin).json()['total'],0)
        retained=await self.admin.fetchrow('SELECT management_deleted_at,object_key,status FROM app_knowledge.applications WHERE application_id=$1',inactive)
        self.assertIsNotNone(retained['management_deleted_at']);self.assertTrue(retained['object_key']);self.assertEqual(retained['status'],'invalidated')
        count=await self.admin.fetchval("SELECT count(*) FROM app_audit.events WHERE event_type='knowledge.record_removed' AND raw_json->>'application_id'=$1",inactive)
        self.assertEqual(count,1)
        foreign=await self.entry(owner='U3',status='invalidated')
        self.assertEqual(self.api.post('/api/knowledge/applications/'+foreign+'/remove-record',headers=admin).status_code,200)

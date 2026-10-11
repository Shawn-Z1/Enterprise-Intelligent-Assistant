"""附件回归仅覆盖超链接、消息归属及跨轮原件复用；实际处理继续在独立Docker客体中执行。"""
import asyncio
import base64
from app.ids import new_id
from app.context import ContextAssembler, load_history
from app.chat_service import RunWriter
from app.model_gateway import ModelSlot
from app.rag_agent import RagAgent
from test_m07b import Runtime, FileFlow


class LinkFiles(Runtime):
    async def test_links_are_inert_strings(self):
        """用真实客体预处理/读取XLSX超链接和CSV URL并计算数值；返回回执证明不执行链接。"""
        generated=await self.run_code("from openpyxl import Workbook\nbook=Workbook();sheet=book.active;sheet.append(['name','amount','link'])\nsheet.append(['Alice',12,'https://example.invalid/private']);sheet['C2'].hyperlink=sheet['C2'].value\nsheet.append(['Bob',18,'https://example.invalid/other']);book.save(OUTPUT_DIR/'links.xlsx')")
        self.assertTrue(generated['ok'],generated)
        files=[self.file(base64.b64decode(generated['artifacts'][0]['data']),'xlsx'),self.file(b'name,amount,link\nAlice,12,https://example.invalid/private\nBob,18,https://example.invalid/other\n','csv')]
        prepared=await self.run_code(files=files,operation='prepare')
        self.assertTrue(prepared['ok'],prepared)
        self.assertTrue(all(row['status']=='ready' for row in prepared['result']['files']),prepared)
        read=await self.run_code(files=[files[0]],operation='read',read={'attachment_id':files[0]['attachment_id']})
        self.assertTrue(read['ok'],read);self.assertEqual(read['result']['rows'][0]['link'],'https://example.invalid/private')
        computed=await self.run_code("assert all(frame['amount'].mean()==15 for frame in tables.values())\nemit_text('URL保留为字符串，两个表平均值均为15')",files)
        self.assertTrue(computed['ok'],computed)


class ConversationFiles(FileFlow):
    async def continue_turn(self, processor, headers, conversation, text):
        """接收既有会话和用户文本，不附带文件编号，驱动实际模型/队列并返回已完成run。"""
        response=self.api.post('/api/conversations/'+conversation+'/messages',headers=headers,json={'content':text,'request_key':new_id()})
        self.assertEqual(response.status_code,202,response.text);ident=response.json()['run_id']
        for _ in range(35):
            run=await processor.claim()
            if run:await processor.process(run)
            state=await self.admin.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1',ident)
            if state['status'] in ('completed','failed','cancelled','interrupted'):break
            job=await self.claim_job()
            if job:await self.complete_job(job)
            await asyncio.sleep(.05)
        self.assertEqual(state['status'],'completed',state['state_json']);return state

    async def test_sent_files_survive_other_turns(self):
        """真实模型隔一轮继续转换旧附件；同时验证未发送草稿不可见、发送幂等、移除和跨用户边界。"""
        _,_,_,headers=await self.flow([('old_meeting.md','# 虚构会议\n林明周五交付测试报告，预算120元。'.encode())],
            '仅依据此文件整理会议纪要，明确负责人和截止日期，不生成文件。')
        file=await self.admin.fetchrow('SELECT * FROM app_runtime.analysis_attachments LIMIT 1');conversation=file['conversation_id']
        detail=self.api.get('/api/conversations/'+conversation,headers=headers).json()
        self.assertEqual(detail['messages'][0]['attachments'][0]['attachment_id'],file['attachment_id'])
        draft=self.api.post('/api/conversations/'+conversation+'/attachments',headers=headers,files=[('files',('not_sent.txt',b'PRIVATE_DRAFT_SENTINEL','text/plain'))]).json()[0]
        processor=self.processor()
        unrelated=await self.continue_turn(processor,headers,conversation,'另一个问题：17加25是多少？直接回答即可。')
        self.assertEqual(unrelated['state_json']['attachment_ids'],[file['attachment_id']]);self.assertEqual(unrelated['state_json']['new_attachment_ids'],[])
        self.assertNotIn(draft['attachment_id'],str(unrelated['state_json']['attachment_catalog']))
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_runtime.sandbox_jobs WHERE run_id=$1 AND operation='prepare'",unrelated['run_id']),0)
        async with self.pool.acquire() as conn:history=await load_history(conn,conversation,file['user_id'])
        slot=ModelSlot('primary',self.settings.primary_base_url,'fixture',self.settings.primary_model,65536)
        prompt=ContextAssembler(history,unrelated['run_id'],1024,unrelated['state_json']['attachment_catalog']).assemble(slot)
        self.assertIn('old_meeting.md',str(prompt));self.assertNotIn('PRIVATE_DRAFT_SENTINEL',str(prompt));self.assertNotIn('not_sent.txt',str(prompt))
        converted=await self.continue_turn(processor,headers,conversation,'把我几轮前发的会议文档原件转换成PDF给我下载，不是把聊天答案转PDF。不需要重新上传。')
        repeated=self.api.post('/api/conversations/'+conversation+'/messages',headers=headers,json={
            'content':'把我几轮前发的会议文档原件转换成PDF给我下载，不是把聊天答案转PDF。不需要重新上传。','request_key':converted['request_key']})
        self.assertEqual(repeated.status_code,202);self.assertEqual(repeated.json()['run_id'],converted['run_id'])
        self.assertEqual(await self.admin.fetchval('SELECT count(*) FROM app_runtime.analysis_attachments WHERE message_id=$1',file['message_id']),1)
        self.assertEqual(await self.admin.fetchval("SELECT count(*) FROM app_runtime.sandbox_jobs WHERE run_id=$1 AND operation='prepare'",converted['run_id']),0)
        count=await self.admin.fetchval("SELECT count(*) FROM app_runtime.sandbox_artifacts a JOIN app_runtime.sandbox_results r USING(result_id) JOIN app_runtime.sandbox_jobs j USING(job_id) WHERE j.run_id=$1 AND a.filename LIKE '%.pdf'",converted['run_id'])
        self.assertGreater(count,0)
        async with self.pool.acquire() as conn:history=await load_history(conn,conversation,file['user_id'])
        agent=RagAgent(processor,processor.rag,RunWriter(self.pool,self.audit,dict(converted)),history)
        await agent.tool_schemas()
        previous=ContextAssembler(agent.assembler.rows,new_id(),1024,converted['state_json']['attachment_catalog']).assemble(slot)
        self.assertTrue(any(row['role']=='user' and '几轮前发的会议文档' in row['content'] for row in previous))
        self.assertTrue(any(row['role']=='assistant' and '旧统计和引用不能冒充本轮执行结果' in row['content'] for row in previous))
        self.assertEqual(self.api.delete('/api/conversations/'+conversation+'/attachments/'+file['attachment_id'],headers=headers).status_code,404)
        self.assertEqual(self.api.delete('/api/conversations/'+conversation+'/attachments/'+draft['attachment_id'],headers=headers).status_code,200)
        self.assertFalse(any(row['attachment_id']==draft['attachment_id'] for row in self.api.get('/api/conversations/'+conversation+'/attachments',headers=headers).json()))
        second=self.api.post('/api/conversations',headers=headers,json={}).json()['conversation_id']
        forbidden=self.api.post('/api/conversations/'+second+'/messages',headers=headers,json={'content':'读取其他会话文件','request_key':new_id(),'attachment_ids':[file['attachment_id']]})
        self.assertEqual(forbidden.status_code,404)
        other=self.login('lisi');self.assertEqual(self.api.get('/api/conversations/'+conversation+'/attachments',headers=other).status_code,404)
        self.evidence.append({'reused_run':converted['run_id'],'pdf_count':count,'new_attachment_ids':converted['state_json']['new_attachment_ids'],'catalog':converted['state_json']['attachment_catalog']})

"""为浏览器安全展示测试追加明确标识的审计夹具，不创建或执行用户业务任务。"""
import asyncio
import json
from app.audit import AuditRecorder,Usage
from app.db_admin import BootstrapSettings,connect_admin
from app.ids import new_id


async def main():
    """无参数追加含脚本字样的文本事件及安全测试元数据；只打印编号，不输出正文或凭证。"""
    conn=await connect_admin(BootstrapSettings());trace=new_id();span=new_id()
    try:
        async with conn.transaction():
            event=await AuditRecorder(None,'M12-browser-fixture').append(trace,'knowledge.m12_display_fixture',
                {'fixture':True,'description':'仅用于验证审计文本展示，不是真实模型调用','code':'<script>window.__m12Executed=true</script>',
                 'long_text':'安全展示样例'*1000,'authorization':'Bearer fixture-secret-never-send'},span_id=span,span_name='浏览器安全展示夹具',
                usage=Usage(source='estimated',input_tokens=9,method='展示夹具，无计费事实',model='fixture-not-real'),connection=conn)
        print(json.dumps({'trace_id':trace,'event_id':event['event_id']}))
    finally:await conn.close()


if __name__=='__main__':asyncio.run(main())

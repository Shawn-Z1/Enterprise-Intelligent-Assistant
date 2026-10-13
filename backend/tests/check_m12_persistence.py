"""对保留卷重建前后事实源与独立恢复副本进行内部指纹比对，不输出用户正文。"""
import asyncio
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from app.db_admin import BootstrapSettings,connect_admin


async def main():
    """读取已验证恢复回执，按备份截止时间比对关键不可变内容及账号权限；仅打印数量/摘要结果。"""
    root=Path(os.environ.get('M12_BACKUP_DIR','/backup'));manifest=json.loads((root/'manifest.json').read_text())
    database=json.loads((root/'restore-receipt.json').read_text())['database'];base=BootstrapSettings()
    if database==base.postgres_db or not database.startswith('m12_restore_'):raise ValueError('禁止使用正式库作为恢复比对目标')
    source=await connect_admin(base);restored=await connect_admin(base.model_copy(update={'postgres_db':database}))
    cutoff=datetime.fromisoformat(manifest['created_at']);checks=[]
    queries={
        'users':"SELECT user_id,username,password_hash,role,status,department_id,grade,employee_id,knowledge_enabled,business_query_enabled,permission_version FROM app_system.users WHERE created_at<$1 ORDER BY user_id",
        'conversations':"SELECT conversation_id,user_id,title FROM app_runtime.conversations WHERE created_at<$1 ORDER BY conversation_id",
        'messages':"SELECT message_id,conversation_id,user_id,role,content,protocol_json FROM app_runtime.messages WHERE created_at<$1 ORDER BY message_id",
        'knowledge':"SELECT application_id,creator_user_id,title,original_sha256,object_key,acl_snapshot FROM app_knowledge.applications WHERE created_at<$1 ORDER BY application_id",
        'memory_snapshots':"SELECT * FROM app_memory.snapshots WHERE created_at<$1 ORDER BY snapshot_id",
        'audit_events':"SELECT * FROM app_audit.events WHERE created_at<$1 ORDER BY event_id"}
    try:
        for label,query in queries.items():
            first=await source.fetch(query,cutoff);second=await restored.fetch(query,cutoff)
            def sha(rows):
                """接收内部行数据，生成稳定JSON指纹，不打印凭证哈希或业务正文。"""
                return hashlib.sha256(json.dumps([dict(row) for row in rows],sort_keys=True,default=str,ensure_ascii=False).encode()).hexdigest()
            passed=len(first)==len(second) and sha(first)==sha(second)
            checks.append({'table':label,'rows':len(first),'equal':passed});assert passed,label+'内容不一致'
        value={'passed':True,'after_application_recreate':True,'checks':checks}
        (root/'persistence-verification.json').write_text(json.dumps(value,ensure_ascii=False,indent=2))
        print(json.dumps(value,ensure_ascii=False))
    finally:await source.close();await restored.close()


if __name__=='__main__':asyncio.run(main())

"""本人实际引用的记忆来源预览；没有记忆管理、修改、删除或跨用户接口。"""
from fastapi import APIRouter, Depends, Request
from app.auth import current_context, auth_error
from app.ids import ShortId
from app.permissions import AuthContext

router=APIRouter(prefix='/api/memory',tags=['个人记忆来源'])


@router.get('/sources/{chunk_id}')
async def memory_source(chunk_id: ShortId,request: Request,context: AuthContext=Depends(current_context)):
    """接收来源编号及当前身份，只返回本人当前版本切片；他人/失效版本统一404，无管理员例外。"""
    async with request.app.state.pool.acquire() as conn:
        row=await conn.fetchrow('''SELECT c.content,c.chunk_id,t.title,t.topic_key,t.source_json,s.created_at
            FROM app_memory.chunks c JOIN app_memory.state st USING(user_id)
            JOIN app_memory.topics t ON t.topic_id=c.topic_id JOIN app_memory.snapshots s ON s.snapshot_id=c.snapshot_id
            WHERE c.chunk_id=$1 AND c.user_id=$2 AND c.snapshot_id=st.snapshot_id AND t.user_id=c.user_id''',chunk_id,context.user_id)
    if not row:raise auth_error(404,'NOT_FOUND','来源不存在或记忆版本已更新')
    await request.app.state.audit.append(request.state.trace_id,'memory.source_read',{'chunk_id':chunk_id},user_id=context.user_id)
    return {'type':'memory','chunk_id':row['chunk_id'],'title':row['title'],'content':row['content'],
            'topic_id':row['topic_key'],'remembered_at':row['created_at'].isoformat(),'source_message_ids':row['source_json'].get('message_ids',[])}

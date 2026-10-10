"""公开来源只展示本人的持久文本快照，不代理网页脚本、图片或跨用户来源。"""
from fastapi import APIRouter, Depends, Request
from app.auth import current_context, auth_error
from app.ids import ShortId
from app.permissions import AuthContext

router = APIRouter(prefix='/api/web',tags=['公开来源'])


@router.get('/sources/{source_id}')
async def web_source(source_id: ShortId, request: Request, context: AuthContext=Depends(current_context)):
    """接收来源编号与当前身份，返回本人会话的公开文本快照；他人/不存在来源均404，不再次抓网页。"""
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow('''SELECT s.snapshot_json FROM app_runtime.web_sources s
            JOIN app_runtime.web_searches q USING(search_id)
            WHERE s.source_id=$1 AND q.user_id=$2''',source_id,context.user_id)
    if row is None:
        raise auth_error(404,'NOT_FOUND','来源不存在或不可访问')
    return row['snapshot_json']

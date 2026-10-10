"""统一敏感动作确认接口；只有本人决定能恢复任务，接口不接收/执行客户端SQL或更改快照。"""
from typing import Literal

from fastapi import APIRouter, Depends, Request

from app.auth import StrictBody, mutation_context, current_context, auth_error
from app.chat_service import RunWriter
from app.ids import ShortId
from app.permissions import AuthContext
from app.sensitive_actions import HumanSecondaryConfirmationHook, public_action
from app.business_sql import allowed_catalog

router=APIRouter(prefix='/api')


class DecisionBody(StrictBody):
    decision: Literal['approve','reject']


@router.get('/actions/{action_id}')
async def action_detail(action_id: ShortId,request: Request,context: AuthContext=Depends(current_context)):
    """接收动作ID和可信身份，返回本人真实等待快照/状态；刷新恢复不假定批准。"""
    hook=HumanSecondaryConfirmationHook(request.app.state.pool,request.app.state.audit)
    return public_action(await hook.owned(action_id,context))


@router.post('/actions/{action_id}/decision')
async def action_decision(action_id: ShortId,body: DecisionBody,request: Request,context: AuthContext=Depends(mutation_context)):
    """接收本人单次决定，经CSRF/所有权/版本复核后恢复运行；不执行敏感动作，不允许更改载荷。"""
    hook=HumanSecondaryConfirmationHook(request.app.state.pool,request.app.state.audit)
    return await hook.decide(action_id,body.decision,context,lambda run:RunWriter(request.app.state.pool,request.app.state.audit,run))


@router.get('/business/results/{result_id}')
async def business_result(result_id: ShortId,request: Request,context: AuthContext=Depends(current_context)):
    """接收结果ID，仅读取本人已有回执；当前业务资格、权限版本及表范围仍须满足，不重新执行SQL。"""
    async with request.app.state.pool.acquire() as conn:
        row=await conn.fetchrow('SELECT q.result_json,a.permission_version,a.payload_json FROM app_runtime.query_results q JOIN app_runtime.tool_calls t USING(call_id) JOIN app_runtime.runs r USING(run_id) JOIN app_runtime.query_approvals a USING(call_id) WHERE q.result_id=$1 AND r.user_id=$2',result_id,context.user_id)
    if row is None:raise auth_error(404,'NOT_FOUND','查询结果不存在或不可访问')
    catalog=allowed_catalog(context)
    if row['permission_version']!=context.permission_version or any(name not in catalog for name in row['payload_json']['target']['tables']):raise auth_error(403,'BUSINESS_EVIDENCE_CHANGED','业务权限已变化，不能继续读取原结果')
    return row['result_json']

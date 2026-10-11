"""敏感操作前的人工二次确认Hook；工具可先准备，真正敏感动作必须持有已消费批准。"""
import hashlib
import json
import secrets
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.auth import auth_error
from app.ids import new_id
from app.timezones import beijing_now


POLICY_VERSION=2
# 策略只由服务端注册，message.send仅保留展示契约，没有生产发送执行器。
ACTION_POLICIES={'knowledge.read':'automatic','public.search':'automatic','business.query':'confirmation','message.send':'confirmation'}


class ActionRequest(BaseModel):
    """准备好的不可变动作快照；purpose解释目的，target/payload决定真实展示与执行内容。"""
    model_config=ConfigDict(extra='forbid',frozen=True)
    action_type: str
    tool_name: str
    purpose: str
    target: dict
    payload: dict


def action_fingerprint(request: ActionRequest) -> str:
    """接收服务器动作快照，对实际类型/目标/载荷求指纹；改目的措辞不能绕过拒绝或改载荷重新确认。"""
    value={'action_type':request.action_type,'tool_name':request.tool_name,'target':request.target,'payload':request.payload}
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def public_action(row: dict) -> dict:
    """接收批准记录，返回本人确认/结果展示字段，不泄露执行能力、身份凭证或框架检查点。"""
    return {'action_id':row['approval_id'],'run_id':row['run_id'],'call_id':row['call_id'],'action_type':row['action_type'],
            'status':row['decision'],'expires_at':row['expires_at'].isoformat(),'request':row['payload_json'],'result':row.get('result_json')}


class HumanSecondaryConfirmationHook:
    """在具体敏感操作边界调用的通用Hook，统一冻结快照/用户决定/执行领取，不与工具开始位置绑定。"""
    def __init__(self,pool,audit):
        """接收应用池和审计，复用现有批准表；未知动作默认拒绝。"""
        self.pool,self.audit=pool,audit

    def policy(self,action_type):
        """接收动作类型，返回服务端固定策略；模型不能声明免确认或注册未知动作。"""
        if action_type not in ACTION_POLICIES:raise auth_error(403,'ACTION_NOT_ALLOWED','敏感动作未注册，不能执行')
        return ACTION_POLICIES[action_type]

    async def prepare(self,request: ActionRequest,context,run,call_id):
        """接收已完成权限/载荷校验的动作与可信身份，冻结本次快照并返回确认记录；不执行动作。"""
        if self.policy(request.action_type)!='confirmation':raise auth_error(400,'ACTION_CONFIRMATION_NOT_REQUIRED','该动作采用自动策略')
        fingerprint=action_fingerprint(request)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current=await conn.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1 AND user_id=$2 FOR UPDATE',run['run_id'],context.user_id)
                if current is None or current['status']!='running' or current['cancel_requested'] or current['lease_owner']!=run.get('lease_owner'):raise auth_error(409,'ACTION_RUN_ENDED','本次任务已停止或执行租约已变化')
                version=await conn.fetchval('SELECT permission_version FROM app_system.users WHERE user_id=$1 AND status=\'active\'',context.user_id)
                if version!=context.permission_version:raise auth_error(403,'ACTION_INVALIDATED','权限已变化，停止准备敏感动作')
                existing=await conn.fetchrow('SELECT * FROM app_runtime.query_approvals WHERE call_id=$1',call_id)
                if existing:
                    if existing['query_fingerprint']!=fingerprint:raise auth_error(409,'ACTION_CHANGED','动作载荷变化，需要新调用和确认')
                    return dict(existing)
                # 用户拒绝后本轮不再自动提出同类敏感动作；新提问才允许重新准备。
                if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM app_runtime.query_approvals WHERE run_id=$1 AND action_type=$2 AND decision='rejected')",run['run_id'],request.action_type):
                    raise auth_error(403,'ACTION_REJECTED','用户已拒绝本轮此类敏感操作，不再重复请求确认')
                row=await conn.fetchrow("INSERT INTO app_runtime.query_approvals(approval_id,user_id,conversation_id,run_id,call_id,action_type,payload_json,query_fingerprint,permission_version,policy_version,expires_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,now()+interval '30 minutes') RETURNING *",
                    new_id(),context.user_id,run['conversation_id'],run['run_id'],call_id,request.action_type,request.model_dump(),fingerprint,context.permission_version,POLICY_VERSION)
                await self.audit.append(run['trace_id'],'action.prepared',{'action':public_action(dict(row)),'fingerprint':fingerprint},user_id=context.user_id,run_id=run['run_id'],connection=conn)
        return dict(row)

    async def owned(self,action_id,context):
        """接收动作ID及当前身份，读取本人快照；猜测他人ID与不存在同样返回404。"""
        async with self.pool.acquire() as conn:
            row=await conn.fetchrow('SELECT * FROM app_runtime.query_approvals WHERE approval_id=$1 AND user_id=$2',action_id,context.user_id)
        if row is None:raise auth_error(404,'NOT_FOUND','待确认动作不存在或不可访问')
        return dict(row)

    async def decide(self,action_id,decision: Literal['approve','reject'],context,writer_factory):
        """接收本人决定及写入器工厂，锁定run/动作后验证冻结快照和权限，幂等入队恢复；不执行敏感动作。"""
        action=await self.owned(action_id,context)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                run=dict(await conn.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1 FOR UPDATE',action['run_id']))
                action=dict(await conn.fetchrow('SELECT * FROM app_runtime.query_approvals WHERE approval_id=$1 FOR UPDATE',action_id))
                if action['decision']!='pending':
                    return public_action(action)
                if run['status']!='waiting_confirmation' or run['cancel_requested']:raise auth_error(409,'ACTION_RUN_ENDED','本次任务已停止或已不在等待确认')
                original=await conn.fetchval("SELECT EXISTS(SELECT 1 FROM app_system.auth_sessions WHERE session_id=$1 AND user_id=$2 AND revoked_at IS NULL AND expires_at>now())",run['state_json']['auth_session_id'],context.user_id)
                valid=original and action['permission_version']==context.permission_version and (action['action_type']!='business.query' or context.can_query_business)
                valid=valid and action['policy_version']==POLICY_VERSION and action_fingerprint(ActionRequest(**action['payload_json']))==action['query_fingerprint']
                status='expired' if action['expires_at']<=beijing_now() else 'invalidated' if not valid else 'approved' if decision=='approve' else 'rejected'
                await conn.execute('UPDATE app_runtime.query_approvals SET decision=$1,updated_at=now() WHERE approval_id=$2',status,action_id)
                await conn.execute("UPDATE app_runtime.runs SET status='pending',state_json=state_json || $1::jsonb,updated_at=now() WHERE run_id=$2",{'resume_action':action_id},run['run_id'])
                await writer_factory(run).append_event(conn,'confirmation.decided',{'message':'本次操作已同意，等待执行' if status=='approved' else '本次操作已拒绝或失效，不会执行','action_id':action_id,'status':status})
                await self.audit.append(run['trace_id'],'action.decided',{'action_id':action_id,'decision':status},user_id=context.user_id,run_id=run['run_id'],connection=conn)
                action['decision']=status
        return public_action(action)

    async def consume(self,action_id,context,run,validator):
        """接收当前身份、任务及具体动作校验器，原子消费批准并生成一次性执行能力；已完成只复用回执。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current=await conn.fetchrow('SELECT * FROM app_runtime.runs WHERE run_id=$1 AND user_id=$2 FOR UPDATE',run['run_id'],context.user_id)
                # 与管理员撤权保持用户→动作锁顺序，避免消费批准和撤权互相等待。
                actor=await conn.fetchrow('SELECT permission_version,status,business_query_enabled FROM app_system.users WHERE user_id=$1 FOR UPDATE',context.user_id)
                row=await conn.fetchrow('SELECT * FROM app_runtime.query_approvals WHERE approval_id=$1 AND user_id=$2 AND run_id=$3 FOR UPDATE',action_id,context.user_id,run['run_id'])
                if row is None:raise auth_error(404,'NOT_FOUND','待确认动作不存在或不可访问')
                row=dict(row)
                request=ActionRequest(**row['payload_json'])
                if self.policy(request.action_type)!='confirmation':raise auth_error(403,'ACTION_NOT_ALLOWED','该动作不可使用确认执行路径')
                if current['status']!='running' or current['cancel_requested'] or current['lease_owner']!=run.get('lease_owner'):raise auth_error(409,'ACTION_RUN_ENDED','任务已停止或执行租约已变化')
                if actor['status']!='active' or actor['permission_version']!=context.permission_version or (request.action_type=='business.query' and not actor['business_query_enabled']):
                    raise auth_error(403,'ACTION_INVALIDATED','当前权限已变化，原确认不能消费')
                await validator(request,context)
                valid=row['permission_version']==context.permission_version and row['policy_version']==POLICY_VERSION and row['query_fingerprint']==action_fingerprint(request)
                if not valid:raise auth_error(403,'ACTION_INVALIDATED','权限、策略或载荷已变化，原确认已失效')
                if row['decision']=='completed':return row,None
                if row['decision'] in ('executing','unknown'):raise auth_error(409,'ACTION_RESULT_UNKNOWN','操作结果未知，不自动重新执行；请重新提问并确认')
                if row['decision']!='approved' or row['expires_at']<=beijing_now():raise auth_error(403,'ACTION_NOT_APPROVED','本次操作未同意、已拒绝或已过期，不执行')
                # 消费批准后生成的随机能力只交给本次执行器，不进入模型、确认窗口或日志。
                token=secrets.token_urlsafe(32)
                await conn.execute("UPDATE app_runtime.query_approvals SET decision='executing',consumed_at=now(),execution_token_hash=$1,updated_at=now() WHERE approval_id=$2",hashlib.sha256(token.encode()).hexdigest(),action_id)
                await self.audit.append(run['trace_id'],'action.consumed',{'action_id':action_id,'fingerprint':row['query_fingerprint']},user_id=context.user_id,run_id=run['run_id'],connection=conn)
                row['decision']='executing'
        return row,token

    async def complete(self,action_id,run,output,status='completed'):
        """接收动作回执，原子持久化最终结果并废弃能力；中断结果未知不得被自动回放执行。"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row=await conn.fetchrow("UPDATE app_runtime.query_approvals SET decision=$1,result_json=$2,execution_token_hash=NULL,updated_at=now() WHERE approval_id=$3 AND run_id=$4 AND decision='executing' RETURNING approval_id",status,output,action_id,run['run_id'])
                if row is None:raise auth_error(409,'ACTION_STATE_CHANGED','动作状态已变化，不能覆盖回执')
                await self.audit.append(run['trace_id'],'action.'+status,{'action_id':action_id,'result':output},user_id=run['user_id'],run_id=run['run_id'],connection=conn)

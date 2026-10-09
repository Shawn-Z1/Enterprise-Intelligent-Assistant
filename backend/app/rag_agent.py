"""LangGraph 的模型→真实 RAG 工具→观察→模型回答循环。"""
import asyncio
import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException

from app.context import ContextAssembler
from app.ids import new_id
from app.model_gateway import GatewayError, estimate_tokens
from app.rag_service import live_context
from app.rag_index import identifiers

# 工具只暴露问题，用户/部门/职级和集合名全部由可信后台上下文决定。
TOOLS = [{'type':'function', 'function':{'name':'search_knowledge', 'description':'查询本企业当前授权的知识文档。内部制度、业务规则或编号的事实必须使用此工具取得证据；普通知识问题可直接回答。',
         'parameters':{'type':'object','properties':{'query':{'type':'string','description':'独立、具体的中文检索问题，可包含精确制度编号或日期'}},'required':['query'],'additionalProperties':False}}}]


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=2000)


class State(TypedDict):
    messages: list[dict]
    turns: int
    result: object


class RagAgent:
    """使用现有主备流式网关构建真实工具循环；原调用和观察写入永久表，界面不展示模型计费。"""
    def __init__(self, processor, rag, writer, rows):
        """接收聊天执行器、真实检索、事件写入器与本人历史，初始化限轮图和本次证据集。"""
        self.processor, self.rag, self.writer = processor, rag, writer
        self.assembler = ContextAssembler(rows, writer.run['run_id'], processor.settings.model_output_tokens)
        self.citations, self.observations, self.protocol = [], [], []
        # 原始用户问题的精确编号/日期属于查询约束，模型改写检索问题不能丢弃这些约束。
        current=next((row['content'] for row in reversed(rows) if row['role']=='user' and row.get('run_id')==writer.run['run_id']),'')
        self.required_identifiers=identifiers(current)
        self.current_state = None
        builder = StateGraph(State)
        builder.add_node('model', self.model)
        builder.add_node('tools', self.tools)
        builder.add_edge(START, 'model')
        builder.add_conditional_edges('model', self.route, ['tools', END])
        builder.add_edge('tools', 'model')
        self.graph = builder.compile()

    async def context(self):
        """无参数从运行创建时保存的登录会话重读当前身份；模型无法指定或覆盖此身份。"""
        return await live_context(self.processor.pool, self.writer.run['user_id'], self.writer.run['state_json']['auth_session_id'])

    def factory(self, slot):
        """接收主或备用模型槽位，组装完整工具调用/观察配对并计入工具 schema 预算，超限明确拒绝。"""
        messages = self.assembler.assemble(slot)+self.current_state['messages']
        if estimate_tokens(slot.request(messages,self.processor.settings.model_output_tokens)['messages'])+estimate_tokens(TOOLS)+self.processor.settings.model_output_tokens>int(slot.window*0.8):
            raise GatewayError('CONTEXT_TOO_LARGE', '检索结果与历史超过当前模型安全窗口，请新建会话或缩短问题')
        return messages

    async def guard(self):
        """无参数在每次实际模型发送前重新核验已取得证据；撤权、删除或旧索引不能继续进入模型。"""
        if not self.citations:
            return
        context, chunks = await self.rag.validate(await self.context(), [row['chunk_id'] for row in self.citations])
        if {row['chunk_id'] for row in chunks}!={row['chunk_id'] for row in self.citations}:
            raise GatewayError('RAG_EVIDENCE_CHANGED', '知识来源或权限已变化，本次回答已停止，请重新提问')

    async def emit(self, kind, data):
        """接收网关事件，保留运行状态但缓冲回答正文，确保工具决定和最终权限复核前不展示过期证据。"""
        if kind in ('assistant.delta','assistant.reset') and self.observations:
            return
        await self.writer.emit(kind, data)

    async def model(self, state: State) -> dict:
        """接收图状态，实际调用文本模型自主选择回答或 tool_calls，返回标准结果及递增轮次。"""
        if state['turns']>=4:
            raise GatewayError('AGENT_STEP_LIMIT', '本次检索步骤超过限制，请缩短问题再试')
        self.current_state = state
        await self.guard()
        result = await self.processor.gateway.complete(self.factory, self.writer.record_model, self.emit, TOOLS, self.guard)
        result.message['_provider'] = result.provider
        return {'messages':state['messages']+[result.message], 'result':result, 'turns':state['turns']+1}

    def route(self, state: State):
        """接收模型真实结果，仅按实际 tool_calls 路由到工具节点；没有调用即结束，不用关键词假装自主决策。"""
        return 'tools' if state['result'].message.get('tool_calls') else END

    async def tools(self, state: State) -> dict:
        """接收真实模型工具调用，严格校验工具和参数，记录完整观察，返回配对 tool 消息供模型继续回答。"""
        calls = state['result'].message['tool_calls']
        if not 1<=len(calls)<=3:
            raise GatewayError('TOOL_CALL_LIMIT', '本次工具调用数量超过限制')
        messages = list(state['messages'])
        for call in calls:
            if not call.get('id') or len(call['id'])>200 or call.get('type')!='function' or call.get('function',{}).get('name')!='search_knowledge':
                raise GatewayError('TOOL_NOT_ALLOWED', '模型请求了未开放的工具，已停止执行')
            try:
                args = Arguments.model_validate_json(call['function']['arguments'])
            except (ValueError,TypeError,KeyError):
                raise GatewayError('INVALID_TOOL_ARGUMENTS','模型工具参数不符合允许的检索格式，已停止执行')
            call_id = new_id()
            async with self.processor.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute('''INSERT INTO app_runtime.tool_calls(call_id,run_id,tool_name,input_json,status,idempotency_key,provider_call_id,protocol_message)
                        VALUES($1,$2,'search_knowledge',$3,'running',$4,$5,$6)''', call_id, self.writer.run['run_id'], args.model_dump(),
                        self.writer.run['run_id']+':'+call['id'], call['id'], call)
                    await self.processor.audit.append(self.writer.run['trace_id'], 'tool.request', {'call_id':call_id,'provider_call':call},
                        user_id=self.writer.run['user_id'], run_id=self.writer.run['run_id'], connection=conn)
            await self.writer.emit('tool.started', {'message':'正在检索当前授权的企业知识'})
            try:
                query=args.query
                missing=[value for value in self.required_identifiers if value not in identifiers(query)]
                if missing:
                    query+='\n精确编号或日期：'+' '.join(missing)
                output = await self.rag.search(query, await self.context(), self.writer.run['trace_id'], self.writer.run['run_id'])
            except HTTPException as error:
                if error.status_code not in (401,403):
                    await self.fail_call(call_id,error)
                    detail=error.detail if isinstance(error.detail,dict) else {'code':'RAG_FAILED','message':'知识检索未完成，请重试'}
                    raise GatewayError(detail['code'],detail['message'])
                output = {'citations':[], 'error':error.detail, 'message':'知识权限不足或登录已失效，请联系管理员开通知识权限；未进行越权检索。'}
            except BaseException as error:
                await self.fail_call(call_id,error)
                raise
            # 本轮证据标号全局递增，多个工具观察不产生相同编号指向不同原件。
            for citation in output['citations']:
                # 多次检索可能命中同一切片，复用原标签，避免一个来源变成多张重复引用卡。
                existing=next((row for row in self.citations if row['chunk_id']==citation['chunk_id']),None)
                citation['label']=existing['label'] if existing else str(len(self.citations)+1)
                if existing is None:
                    self.citations.append(citation)
            self.observations.append(output)
            observation = {'role':'tool', 'tool_call_id':call['id'], 'content':json.dumps(output,ensure_ascii=False)}
            messages.append(observation)
            self.protocol.extend([call, observation])
            async with self.processor.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute('UPDATE app_runtime.tool_calls SET status=$1,output_json=$2 WHERE call_id=$3', 'denied' if output.get('error') else 'completed', output, call_id)
                    await self.processor.audit.append(self.writer.run['trace_id'], 'tool.observation', {'call_id':call_id,'observation':observation},
                        user_id=self.writer.run['user_id'], run_id=self.writer.run['run_id'], connection=conn)
            await self.writer.emit('tool.completed', {'message':output['message']})
        return {'messages':messages}

    async def fail_call(self,call_id,error):
        """接收内部调用编号和异常，持久记录失败或中断及安全原因，不留下伪运行状态或供应商凭证。"""
        async with self.processor.pool.acquire() as conn:
            async with conn.transaction():
                failure={'error_type':type(error).__name__,'message':'检索未完成，未发布企业事实'}
                await conn.execute('UPDATE app_runtime.tool_calls SET status=$1,output_json=$2 WHERE call_id=$3',
                    'interrupted' if isinstance(error,asyncio.CancelledError) else 'failed',failure,call_id)
                await self.processor.audit.append(self.writer.run['trace_id'],'tool.failed',{'call_id':call_id,**failure},
                    user_id=self.writer.run['user_id'],run_id=self.writer.run['run_id'],connection=conn)

    async def run(self):
        """无参数运行真实 LangGraph，结束时复核证据和引用，再返回带来源的最终模型结果。"""
        state = await self.graph.ainvoke({'messages':[], 'turns':0, 'result':None}, {'recursion_limit':12})
        await self.guard()
        result = state['result']
        # 无相关证据时由服务端守住内部事实边界，即便模型试图补写制度也不能作为有依据回答展示。
        if self.observations and not self.citations:
            denied = any(value.get('error') for value in self.observations)
            result.content = '当前未获得知识库权限，请联系管理员分配部门和职级后再查询。' if denied else '未找到足够相关且有权访问的企业知识，无法确定该内部规定。请提供正式文件或请管理员补充知识材料。'
            result.message['content'] = result.content
        import re
        if self.observations and any(number not in {row['label'] for row in self.citations} for number in re.findall(r'\[(\d+)\]', result.content)):
            raise GatewayError('INVALID_CITATION', '回答引用了未提供的来源，已停止发布，请重新提问')
        result.message['citations'] = self.citations
        result.message['agent_protocol'] = self.protocol
        await self.writer.emit('assistant.reset', {'reason':'final_answer'})
        # 普通知识问题仍展示分段文字，RAG 结果先完成最后权限复核再交付，避免旧证据流出。
        for offset in range(0,len(result.content),96):
            await self.writer.emit('assistant.delta', {'text':result.content[offset:offset+96]})
        return result

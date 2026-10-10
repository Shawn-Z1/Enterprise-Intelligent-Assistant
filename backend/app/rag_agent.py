"""LangGraph真实模型→工具→敏感动作确认→观察→审核→发布循环。"""
import asyncio
import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.errors import NodeCancelledError
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException

from app.context import ContextAssembler
from app.answer_review import evaluate, limited_answer
from app.ids import new_id
from app.model_gateway import GatewayError, estimate_tokens, pack_result, unpack_result
from app.graph_checkpoint import PgGraphSaver
from app.rag_service import live_context
from app.rag_index import identifiers
from app.tool_registry import available_tools, model_observation
from app.web_search import SearchError, outbound_query

class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=2000)


class State(TypedDict):
    messages: list[dict]
    turns: int
    result: dict | None
    revisions: int
    reviews: list[dict]
    review_status: str
    citations: list[dict]
    observations: list[dict]
    protocol: list[dict]
    web_output: dict | None
    tool_index: int
    pending_action: str | None
    tools_route: str


class RagAgent:
    """使用现有主备流式网关构建真实工具循环；原调用和观察写入永久表，界面不展示模型计费。"""
    def __init__(self, processor, rag, writer, rows):
        """接收聊天执行器、真实检索、事件写入器与本人历史，初始化限轮图和本次证据集。"""
        self.processor, self.rag, self.writer = processor, rag, writer
        self.assembler = ContextAssembler(rows, writer.run['run_id'], processor.settings.model_output_tokens)
        self.citations, self.observations, self.protocol = [], [], []
        self.current_tools = []
        # 仅当前用户问题可供公开查询投影，内部历史及工具正文不参与外发词生成。
        self.current_input = next((row['content'] for row in reversed(rows) if row['role']=='user' and row.get('run_id')==writer.run['run_id']),'')
        self.web = getattr(processor,'web',None)
        # 同一run的公开任务不重复外发，复用本轮已保存快照；不跨会话/用户缓存。
        self.web_output = None
        # 原始用户问题的精确编号/日期属于查询约束，模型改写检索问题不能丢弃这些约束。
        current=self.current_input
        self.required_identifiers=identifiers(current)
        self.current_state = None
        builder = StateGraph(State)
        builder.add_node('model', self.model)
        builder.add_node('tools', self.tools)
        builder.add_node('confirmation',self.confirmation)
        builder.add_node('review', self.review)
        builder.add_node('publish', self.publish)
        builder.add_edge(START, 'model')
        builder.add_conditional_edges('model', self.route, ['tools', 'review'])
        builder.add_conditional_edges('tools',lambda state:state['tools_route'],['tools','confirmation','model'])
        builder.add_edge('confirmation','tools')
        builder.add_conditional_edges('review', self.review_route, ['model','publish'])
        builder.add_edge('publish', END)
        self.graph = builder.compile(checkpointer=PgGraphSaver(processor.pool,writer.run['run_id']))

    def sync_state(self,state):
        """接收持久图状态，将证据/观察/协议及公开快照还原到当前实例，跨worker恢复不依赖内存。"""
        self.citations,self.observations,self.protocol,self.web_output=state['citations'],state['observations'],state['protocol'],state['web_output']

    async def context(self):
        """无参数从运行创建时保存的登录会话重读当前身份；模型无法指定或覆盖此身份。"""
        try:
            return await live_context(self.processor.pool, self.writer.run['user_id'], self.writer.run['state_json']['auth_session_id'])
        except HTTPException as error:
            detail=error.detail if isinstance(error.detail,dict) else {'code':'UNAUTHENTICATED','message':'登录已失效，请重新登录'}
            raise GatewayError(detail['code'],detail['message']) from None

    def factory(self, slot):
        """接收主或备用模型槽位，组装完整工具调用/观察配对并计入工具 schema 预算，超限明确拒绝。"""
        messages = self.assembler.assemble(slot)+self.current_state['messages']
        if estimate_tokens(slot.request(messages,self.processor.settings.model_output_tokens)['messages'],slot.model)+estimate_tokens(self.current_tools,slot.model)+self.processor.settings.model_output_tokens>int(slot.window*0.8):
            raise GatewayError('CONTEXT_TOO_LARGE', '检索结果与历史超过当前模型安全窗口，请新建会话或缩短问题')
        return messages

    async def guard(self):
        """无参数在每次实际模型发送前重新核验已取得证据；撤权、删除或旧索引不能继续进入模型。"""
        context = await self.context()
        if not self.citations:
            return
        internal = [row for row in self.citations if row.get('type','knowledge')=='knowledge']
        business=[row for row in self.citations if row.get('type')=='business']
        if business and (not context.can_query_business or any(row.get('permission_version')!=context.permission_version for row in business)):
            raise GatewayError('BUSINESS_EVIDENCE_CHANGED','业务权限已变化，本次回答已停止，请重新提问')
        if not internal:
            return
        context, chunks = await self.rag.validate(context, [row['chunk_id'] for row in internal])
        if {row['chunk_id'] for row in chunks}!={row['chunk_id'] for row in internal}:
            raise GatewayError('RAG_EVIDENCE_CHANGED', '知识来源或权限已变化，本次回答已停止，请重新提问')

    async def tool_schemas(self):
        """无参数重读PG身份，过滤模型工具目录并整轮移除旧内部证据派生历史；返回可调用schema。"""
        context = await self.context()
        self.current_tools = available_tools(context,self.web is not None)
        # 历史内部证据轮次整体不复送；原历史/审计保留，内部事实需重新检索。
        excluded = {row.get('run_id') for row in self.assembler.rows if row['role']=='assistant' and
                    any(value.get('type','knowledge') in ('knowledge','business') for value in (row.get('protocol_json') or {}).get('message',{}).get('citations',[]))}
        self.assembler.rows = [row for row in self.assembler.rows if row.get('run_id')==self.writer.run['run_id'] or row.get('run_id') not in excluded]
        return self.current_tools

    async def emit(self, kind, data):
        """接收主模型网关事件，所有草稿先缓冲，状态继续展示；审核通过或受限结束后才发布正文。"""
        if kind in ('assistant.delta','assistant.reset'):
            await self.writer.check_active()
            return
        await self.writer.emit(kind, data)

    async def model(self, state: State) -> dict:
        """接收图状态，实际调用文本模型自主选择回答或 tool_calls，返回标准结果及递增轮次。"""
        if state['turns']>=11:
            raise GatewayError('AGENT_STEP_LIMIT', '本次检索步骤超过限制，请缩短问题再试')
        self.sync_state(state)
        self.current_state = state
        await self.guard()
        result = await self.processor.gateway.complete(self.factory, self.writer.record_model, self.emit, self.tool_schemas, self.guard)
        result.message['_provider'] = result.provider
        return {'messages':state['messages']+[result.message], 'result':pack_result(result), 'turns':state['turns']+1,'tool_index':0}

    def route(self, state: State):
        """接收模型真实结果，有调用则执行工具，无调用进入独立审核，普通直接回答也不能跳过审核。"""
        return 'tools' if state['result']['message'].get('tool_calls') else 'review'

    async def tools(self, state: State) -> dict:
        """接收真实模型工具调用，严格校验工具和参数，记录完整观察，返回配对 tool 消息供模型继续回答。"""
        self.sync_state(state)
        calls = state['result']['message']['tool_calls']
        if not 1<=len(calls)<=3:
            raise GatewayError('TOOL_CALL_LIMIT', '本次工具调用数量超过限制')
        messages = list(state['messages'])
        if len(self.observations)+len(calls)-state['tool_index']>10:
            raise GatewayError('TOOL_CALL_LIMIT','整轮工具调用达到10次上限，请缩小任务')
        for call in calls[state['tool_index']:state['tool_index']+1]:
            name = call.get('function',{}).get('name')
            if not call.get('id') or len(call['id'])>200 or call.get('type')!='function' or name not in ('search_knowledge','search_web','query_business_data'):
                raise GatewayError('TOOL_NOT_ALLOWED', '模型请求了未开放的工具，已停止执行')
            try:
                args = Arguments.model_validate_json(call['function']['arguments'])
            except (ValueError,TypeError,KeyError):
                raise GatewayError('INVALID_TOOL_ARGUMENTS','模型工具参数不符合允许的检索格式，已停止执行')
            call_id = new_id()
            async with self.processor.pool.acquire() as conn:
                async with conn.transaction():
                    existing=await conn.fetchrow('SELECT * FROM app_runtime.tool_calls WHERE idempotency_key=$1',self.writer.run['run_id']+':'+call['id'])
                    if existing:
                        call_id=existing['call_id']
                    else:
                        await conn.execute('''INSERT INTO app_runtime.tool_calls(call_id,run_id,tool_name,input_json,status,idempotency_key,provider_call_id,protocol_message)
                        VALUES($1,$2,$7,$3,'running',$4,$5,$6)''', call_id, self.writer.run['run_id'], args.model_dump(),
                        self.writer.run['run_id']+':'+call['id'], call['id'], call, name)
                    await self.processor.audit.append(self.writer.run['trace_id'], 'tool.request', {'call_id':call_id,'provider_call':call},
                        user_id=self.writer.run['user_id'], run_id=self.writer.run['run_id'], connection=conn)
            await self.writer.emit('tool.started', {'message':'正在准备或恢复业务查询' if name=='query_business_data' else '正在检索当前授权的企业知识' if name=='search_knowledge' else '正在查询公开互联网','tool_name':name})
            try:
                query=args.query
                missing=[value for value in self.required_identifiers if value not in identifiers(query)]
                if missing and name=='search_knowledge':
                    query+='\n精确编号或日期：'+' '.join(missing)
                context = await self.context()
                permitted = {value['function']['name'] for value in available_tools(context,self.web is not None)}
                if name not in permitted:
                    raise HTTPException(403,{'code':'TOOL_PERMISSION_DENIED','message':'当前身份没有该工具的调用资格'})
                if existing and existing['output_json']:
                    output=existing['output_json']
                elif name=='query_business_data':
                    async with self.processor.pool.acquire() as conn:
                        action=await conn.fetchrow('SELECT * FROM app_runtime.query_approvals WHERE call_id=$1',call_id)
                    if action is None:action=await self.processor.business.prepare(query,context,self.writer.run,call_id,self.writer,self.guard)
                    if action['decision']=='pending':
                        return {'pending_action':action['approval_id'],'tools_route':'confirmation'}
                    output=await self.processor.business.execute(action['approval_id'],context,self.writer.run)
                elif name=='search_knowledge':
                    self.processor.hook.policy('knowledge.read')
                    output = await self.rag.search(query,context,self.writer.run['trace_id'],self.writer.run['run_id'])
                    for citation in output['citations']:
                        citation['type']='knowledge'
                else:
                    self.processor.hook.policy('public.search')
                    # 搜索仅接收本次用户输入；已取得的内部证据与模型候选不进入搜索检查或生成。
                    outbound_query(self.current_input,self.processor.audit.secrets)
                    if self.web_output:
                        output={**self.web_output,'reused_in_run':True,'message':'本轮相同公开任务已查询，复用同一快照，没有新搜索或新来源。请根据已观察证据回答，资料缺失则明确说明。'}
                    else:
                        output = await self.web.search(self.current_input,context,self.writer.run)
                        self.web_output=output
            except SearchError as error:
                output = {'citations':[],'error':error.public(),'message':error.message}
            except HTTPException as error:
                if name=='query_business_data':
                    output={'citations':[],'error':error.detail,'message':error.detail.get('message','业务查询未执行') if isinstance(error.detail,dict) else '业务查询未执行'}
                elif error.status_code not in (401,403):
                    await self.fail_call(call_id,error)
                    detail=error.detail if isinstance(error.detail,dict) else {'code':'RAG_FAILED','message':'知识检索未完成，请重试'}
                    raise GatewayError(detail['code'],detail['message'])
                else:
                    output = {'citations':[], 'error':error.detail, 'message':'知识权限不足或登录已失效，请联系管理员开通知识权限；未进行越权检索。'}
            except BaseException as error:
                await self.fail_call(call_id,error)
                raise
            # 本轮证据标号全局递增，多个工具观察不产生相同编号指向不同原件。
            for citation in output['citations']:
                # 多次检索可能命中同一切片，复用原标签，避免一个来源变成多张重复引用卡。
                key = 'result_id' if citation.get('type')=='business' else 'source_id' if citation.get('type')=='web' else 'chunk_id'
                existing=next((row for row in self.citations if (row.get('url')==citation['url'] if key=='source_id' else row.get(key)==citation[key])),None)
                citation['label']=existing['label'] if existing else str(len(self.citations)+1)
                if existing is None:
                    self.citations.append(citation)
            self.observations.append(output)
            # 公开证据给模型的正文按片段限额，完整受限快照另存PG，避免多来源组合撑破上下文。
            observed = model_observation(output,name)
            observation = {'role':'tool', 'tool_call_id':call['id'], 'content':json.dumps(observed,ensure_ascii=False)}
            messages.append(observation)
            self.protocol.extend([call, observation])
            async with self.processor.pool.acquire() as conn:
                async with conn.transaction():
                    # 权限/外发拒绝与服务故障分开保存，失败不伪装成正常完成。
                    code=(output.get('error') or {}).get('code','')
                    denied=code in ('WEB_QUERY_PRIVATE','TOOL_PERMISSION_DENIED','KNOWLEDGE_NOT_AUTHORIZED','BUSINESS_NOT_AUTHORIZED','BUSINESS_SCOPE_DENIED','SQL_NOT_ALLOWED','ACTION_NOT_APPROVED','ACTION_REJECTED','ACTION_INVALIDATED')
                    status='denied' if denied else 'failed' if code else 'completed'
                    await conn.execute('UPDATE app_runtime.tool_calls SET status=$1,output_json=$2 WHERE call_id=$3',status,output,call_id)
                    await self.processor.audit.append(self.writer.run['trace_id'], 'tool.observation', {'call_id':call_id,'observation':observation},
                        user_id=self.writer.run['user_id'], run_id=self.writer.run['run_id'], connection=conn)
            await self.writer.emit('tool.completed', {'message':output['message']})
        index=state['tool_index']+1
        return {'messages':messages,'tool_index':index,'pending_action':None,'tools_route':'tools' if index<len(calls) else 'model',
                'citations':self.citations,'observations':self.observations,'protocol':self.protocol,'web_output':self.web_output}

    async def confirmation(self,state: State):
        """接收已准备动作，在敏感操作边界暂停；恢复值只用于定位，真正批准仍由PG与Hook检查。"""
        interrupt({'action_id':state['pending_action']})
        return {'tools_route':'tools'}

    async def fail_call(self,call_id,error):
        """接收内部调用编号和异常，持久记录失败或中断及安全原因，不留下伪运行状态或供应商凭证。"""
        async with self.processor.pool.acquire() as conn:
            async with conn.transaction():
                failure={'error_type':type(error).__name__,'message':'检索未完成，未发布企业事实'}
                await conn.execute('UPDATE app_runtime.tool_calls SET status=$1,output_json=$2 WHERE call_id=$3',
                    'interrupted' if isinstance(error,asyncio.CancelledError) else 'failed',failure,call_id)
                await self.processor.audit.append(self.writer.run['trace_id'],'tool.failed',{'call_id':call_id,**failure},
                    user_id=self.writer.run['user_id'],run_id=self.writer.run['run_id'],connection=conn)

    async def review(self, state: State) -> dict:
        """接收待发布状态，执行事实与独立语义审核；最多一次返工，异常/二次驳回明确受限交付。"""
        self.sync_state(state)
        await self.guard()
        await self.writer.emit('review.started',{'message':'正在核验本轮执行过程和答案'})
        try:
            decision,issues = await evaluate(self,state)
            record = {'round':len(state['reviews'])+1,**decision.model_dump(),'backend_issues':issues}
        except GatewayError as error:
            # 权限/证据变化属于安全停止，不能降级交付仍含旧正文的资料摘录。
            if error.code in ('RAG_EVIDENCE_CHANGED','BUSINESS_EVIDENCE_CHANGED','UNAUTHENTICATED','FORBIDDEN','EXECUTION_RECORD_INVALID'):
                raise
            record = {'round':len(state['reviews'])+1,'verdict':'unavailable','error':error.public()}
        await self.processor.audit.append(self.writer.run['trace_id'],'answer.review',record,
            user_id=self.writer.run['user_id'],run_id=self.writer.run['run_id'],conversation_id=self.writer.run['conversation_id'])
        reviews = state['reviews']+[record]
        if record['verdict']=='approve':
            await self.writer.emit('review.completed',{'message':'答案核验通过'})
            return {'reviews':reviews,'review_status':'approved'}
        if record['verdict']=='revise' and state['revisions']==0 and state['turns']<11:
            await self.writer.emit('review.revision',{'message':'发现需要修正的问题，正在补充核验；最多返工一次'})
            # 反馈是结构化数据；主模型保留已有工具状态，不能重跑副作用或绕过权限/外发边界。
            feedback = {'role':'system','content':'服务器发布前审核未通过。请参考以下问题修正答案，必要时继续调用当前允许工具。不要仅声称已修正；工具动作必须实际执行。不得绕过安全拒绝、重复已完成的副作用，或把内部证据用于公开搜索。若无合理路径请明确限制。审核问题数据：'+json.dumps(record.get('issues',[]),ensure_ascii=False)}
            return {'messages':state['messages']+[feedback],'reviews':reviews,'revisions':1,'review_status':'revise'}
        status = 'unavailable' if record['verdict']=='unavailable' else 'limited'
        await self.writer.emit('review.limited',{'message':'未形成通过核验的完整答案，将说明本轮资料与限制'})
        return {'reviews':reviews,'review_status':status}

    def review_route(self, state: State):
        """接收审核状态，只有首次明确返工才能回模型，其他结论均进入受控发布。"""
        return 'model' if state['review_status']=='revise' else 'publish'

    async def publish(self, state: State) -> dict:
        """接收最终审核状态，复验权限后发布通过答案或服务端资料摘录；不展示未审核草稿/审核JSON。"""
        self.sync_state(state)
        await self.guard()
        result = unpack_result(state['result'])
        if state['review_status']!='approved':
            result.content = limited_answer(self.citations,state['review_status'])
        # 无相关证据时由服务端守住内部事实边界，即便模型试图补写制度也不能作为有依据回答展示。
        elif self.observations and not self.citations:
            # 没有资料不等于无权限；真实错误逐项说明，不把服务故障误报为全库无内容。
            result.content = '本轮检索没有取得可引用资料，无法确定依赖这些资料的结论。'
            errors = [value['message'] for value in self.observations if value.get('error')]
            if errors:
                result.content += ' '+'；'.join(dict.fromkeys(errors))
            else:
                result.content += '请缩小检索主题或补充正式资料。'
            result.message['content'] = result.content
        import re
        if state['review_status']=='approved' and self.observations and any(number not in {row['label'] for row in self.citations} for number in re.findall(r'\[(\d+)\]', result.content)):
            raise GatewayError('INVALID_CITATION', '回答引用了未提供的来源，已停止发布，请重新提问')
        result.message['citations'] = self.citations
        result.message['agent_protocol'] = self.protocol
        result.message['answer_review'] = {'status':state['review_status'],'revisions':state['revisions'],'rounds':state['reviews']}
        result.message['content'] = result.content
        await self.writer.emit('assistant.reset', {'reason':'final_answer'})
        # 普通知识问题仍展示分段文字，RAG 结果先完成最后权限复核再交付，避免旧证据流出。
        for offset in range(0,len(result.content),96):
            await self.writer.emit('assistant.delta', {'text':result.content[offset:offset+96]})
        return {'result':pack_result(result)}

    async def run(self):
        """无参数运行模型/工具/审核/发布图，返回最终结果；整体轮次、工具次数、超时仍采用原运行预算。"""
        try:
            initial={'messages':[], 'turns':0, 'result':None,'revisions':0,'reviews':[],'review_status':'',
                     'citations':[],'observations':[],'protocol':[],'web_output':None,'tool_index':0,'pending_action':None,'tools_route':'model'}
            config={'configurable':{'thread_id':self.writer.run['run_id']},'recursion_limit':64}
            resume=self.writer.run['state_json'].get('resume_action')
            state = await self.graph.ainvoke(Command(resume=resume) if resume else initial,config)
            if state.get('__interrupt__'):
                await self.writer.suspend(state['pending_action'])
                return None
        except NodeCancelledError as error:
            # LangGraph将节点主动取消包装为普通异常，恢复取消语义交给任务处理器区分本人停止/进程中断。
            raise asyncio.CancelledError() from error
        return unpack_result(state['result'])

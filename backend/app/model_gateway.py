import asyncio
import copy
import json
from dataclasses import dataclass, field
from time import monotonic
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from app.audit import Usage
from app.config import Settings
from app.ids import new_id
from app.token_counting import count_value, count_request

AuditCallback = Callable[[str, dict, Usage | None], Awaitable[None]]
EventCallback = Callable[[str, dict], Awaitable[None]]


class GatewayError(Exception):
    """模型调用安全错误；内部响应只进入遮蔽审计，界面只取得中文说明和错误码。"""

    def __init__(self, code: str, message: str, retryable: bool = False, *, status: int | None = None, raw=None):
        """接收分类、中文说明、重试资格和可选内部错误，初始化异常，不包含请求凭证。"""
        super().__init__(message)
        self.code, self.message, self.retryable, self.status, self.raw = code, message, retryable, status, raw

    def public(self) -> dict:
        """无参数，返回前端安全错误结构，排除供应商原文、地址和密钥。"""
        return {"code":self.code,"message":self.message}


@dataclass(frozen=True)
class ModelSlot:
    """文本槽位只需地址、密钥、模型名；协议适配及保守窗口由网关管理。"""
    role: str
    url: str
    key: str = field(repr=False)
    model: str = ""
    context_tokens: int = 0
    # 安全比例由运行配置传入；所有请求阶段共享同一预算边界。
    context_safety_ratio: float = 0.95

    @property
    def provider(self) -> str:
        """无参数，根据服务域名与型号识别协议适配器；返回 DeepSeek、千问或通用兼容协议。"""
        host = (urlsplit(self.url).hostname or "").lower()
        if host.endswith("deepseek.com"):
            return "deepseek"
        if host.endswith("aliyuncs.com") or self.model.lower().startswith("qwen"):
            return "qwen"
        return "compatible"

    @property
    def window(self) -> int:
        """无参数，返回显式窗口或已核对型号的窗口；未知型号保留保守值，避免按厂商一概扩大。"""
        # 2026-10-12核对官方型号说明；代理服务如有更小限制，应显式配置槽位窗口。
        known_windows = {'deepseek-flash':1_000_000,'qwen3.7-flash':1_000_000,
                         'qwen3.7-flash-2026-07-15':1_000_000,'qwen3.8-flash':1_000_000}
        return self.context_tokens or known_windows.get(self.model.lower(),32768 if self.provider in ("deepseek","qwen") else 8192)

    @property
    def safe_limit(self) -> int:
        """无参数，返回输入与输出合计的安全上限，供历史、工具循环及审核统一使用。"""
        return int(self.window*self.context_safety_ratio)

    def endpoint(self) -> str:
        """无参数，验证配置并返回 Chat Completions 地址；非法槽位在发送 HTTP 前明确失败。"""
        parsed = urlsplit(self.url)
        if parsed.scheme not in ("http","https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or not self.key or not self.model:
            raise GatewayError("MODEL_CONFIGURATION","模型配置不完整或地址格式不正确")
        base = self.url.rstrip("/")
        return base if base.endswith("/chat/completions") else base+"/chat/completions"

    def request(self, messages: list[dict], output_tokens: int) -> dict:
        """接收标准历史和输出预算，返回本槽位请求 JSON；供应商思考字段不会跨厂商直接转发。"""
        converted = []
        for message in messages:
            value = {key:copy.deepcopy(message[key]) for key in ("role","content","tool_calls","tool_call_id","name") if key in message}
            if self.provider == "deepseek" and message.get("_provider") == "deepseek" and "reasoning_content" in message:
                value["reasoning_content"] = message["reasoning_content"]
            converted.append(value)
        body = {"model":self.model,"messages":converted,"stream":True,"max_tokens":output_tokens}
        if self.provider in ("deepseek","qwen"):
            body["stream_options"] = {"include_usage":True}
        if self.provider == "deepseek":
            body["thinking"] = {"type":"disabled"}
        elif self.provider == "qwen":
            body["enable_thinking"] = False
        return body


@dataclass
class ModelResult:
    """合并后的标准回复及公开协议字段，实际原始包另存永久模型审计。"""
    content: str
    message: dict
    usage: Usage
    model: str
    provider: str
    slot: str
    attempt_id: str
    finish_reason: str


@dataclass
class Circuit:
    """单 worker 的初始熔断状态，重启后重新探测，不作为业务任务的唯一持久状态。"""
    failures: int = 0
    opened_until: float = 0
    probing: bool = False


def pack_result(result: ModelResult) -> dict:
    """接收网关结果，转换为检查点可保存的普通字段，usage保留实际来源，不序列化自定义执行对象。"""
    return {**vars(result),'usage':result.usage.model_dump(mode='json')}


def unpack_result(value: dict) -> ModelResult:
    """接收可信检查点中的模型结果字典，还原网关结果供审核/发布，不接受浏览器提供的状态。"""
    return ModelResult(**{**value,'usage':Usage(**value['usage'])})


def estimate_tokens(value, model: str='') -> int:
    """接收消息/文本与型号，返回模型参考tokenizer预算；未知型号用字节上界，不表示供应商计费用量。"""
    return count_value(value,model).count


def usage_from_response(raw: dict | None, messages: list[dict], content: str, model: str, tools=None, response_message=None) -> Usage:
    """接收最后一份实际用量、输入和输出，返回 reported 或明确方法的 estimated，不累加流式用量包。"""
    raw = raw or {}
    values = {name:raw.get(key) for name,key in (("input_tokens","prompt_tokens"),("output_tokens","completion_tokens"),("total_tokens","total_tokens"))}
    if any(value is not None for value in values.values()) and all(value is None or (type(value) is int and value>=0) for value in values.values()):
        return Usage(source="reported",**values,model=model)
    counted=count_request(messages,tools,model)
    output_tokens=estimate_tokens(response_message or content,model)
    input_tokens=counted.count
    return Usage(source="estimated",input_tokens=input_tokens,output_tokens=output_tokens,total_tokens=input_tokens+output_tokens,
                 method=counted.method if counted.tokenizer=='unknown_model' else counted.method+':'+counted.tokenizer,model=model)


def classify_status(status: int, raw) -> GatewayError:
    """接收 HTTP 状态及内部响应，返回可审计错误；凭证、参数错误不进行无意义重试。"""
    if status in (401,403):
        return GatewayError("MODEL_AUTHENTICATION","模型凭证被服务商拒绝",status=status,raw=raw)
    if status==429:
        return GatewayError("MODEL_RATE_LIMIT","模型服务繁忙或额度受限",True,status=status,raw=raw)
    if status>=500 or status in (408,425):
        return GatewayError("MODEL_SERVICE_ERROR","模型服务暂时不可用",True,status=status,raw=raw)
    return GatewayError("MODEL_PARAMETER_ERROR","模型请求参数或型号被服务商拒绝",status=status,raw=raw)


class ModelGateway:
    """统一管理适配、3+3 尝试和熔断；HTTP 客户端重试为零，不与业务层叠加。"""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        """接收配置和可选测试 HTTP 客户端，建立主备槽位及独立熔断状态，不发送请求。"""
        self.settings = settings
        self.slots = [ModelSlot("primary",settings.primary_base_url,settings.primary_api_key.get_secret_value(),settings.primary_model,settings.primary_context_tokens,settings.model_context_safety_ratio),
                      ModelSlot("fallback",settings.fallback_base_url,settings.fallback_api_key.get_secret_value(),settings.fallback_model,settings.fallback_context_tokens,settings.model_context_safety_ratio)]
        # 审核使用独立槽位和熔断状态；即使URL/密钥相同也不与备用槽位耦合。
        self.review_slot = ModelSlot("review",settings.review_base_url,settings.review_api_key.get_secret_value(),settings.review_model,settings.review_context_tokens,settings.model_context_safety_ratio)
        self.owns_client = client is None
        self.client = client or httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0),trust_env=False,
            timeout=httpx.Timeout(settings.model_read_timeout_seconds,connect=10),follow_redirects=False)
        self.circuits = {slot.role:Circuit() for slot in [*self.slots,self.review_slot]}

    async def close(self):
        """无参数关闭网关自建连接池；外部测试客户端由调用者管理，无返回值。"""
        if self.owns_client:
            await self.client.aclose()

    async def complete(self, factory: Callable[[ModelSlot],list[dict]], audit: AuditCallback, emit: EventCallback, tools: list[dict] | None=None, guard=None) -> ModelResult:
        """接收逐槽位上下文工厂和审计/事件回调，流式调用主备；成功返回合并回复，完全失败抛安全错误。"""
        last_error = GatewayError("MODEL_UNAVAILABLE","主模型与备用模型均未能完成回答")
        for slot in self.slots:
            circuit = self.circuits[slot.role]
            if slot.role == "fallback":
                await emit("model.fallback",{"message":"主模型未完成回答，正在切换备用模型","reason":last_error.code})
                await audit("model.fallback",{"reason":last_error.code},None)
            if circuit.opened_until>monotonic() or circuit.probing:
                last_error = GatewayError("MODEL_CIRCUIT_OPEN","模型暂时熔断，正在等待恢复")
                await emit("model.circuit_open",{"slot":slot.role,"message":"模型暂时不可用，已跳过调用"})
                await audit("model.circuit_skipped",{"slot":slot.role},None)
                continue
            half_open = circuit.opened_until>0
            circuit.probing = half_open
            try:
                messages = factory(slot)
                slot.endpoint()
            except GatewayError as error:
                circuit.probing = False
                last_error = error
                await audit("model.configuration_or_budget_rejected",{"slot":slot.role,**error.public()},None)
                continue
            try:
                for number in range(1,4):
                    attempt_id = new_id()
                    await emit("assistant.reset",{"reason":"new_attempt"})
                    await emit("model.started",{"slot":slot.role,"model":slot.model,"attempt":number,"message":"正在生成回答"})
                    try:
                        if guard:
                            await guard()
                        # 每次实际请求重建工具目录与消息，重试/备用不会沿用撤权前的schema。
                        actual_tools = await tools() if callable(tools) else tools
                        messages = factory(slot)
                        result = await self._attempt(slot,messages,number,attempt_id,audit,emit,actual_tools)
                        circuit.failures,circuit.opened_until = 0,0
                        return result
                    except GatewayError as error:
                        last_error = error
                        circuit.failures += 1
                        if circuit.failures>=self.settings.model_circuit_failures:
                            circuit.opened_until = monotonic()+self.settings.model_circuit_seconds
                        await emit("model.attempt_failed",{"slot":slot.role,"attempt":number,**error.public()})
                        if not error.retryable or half_open or number==3:
                            break
                        delay = self.settings.model_retry_backoff_seconds*2**(number-1)
                        await emit("model.retry",{"slot":slot.role,"next_attempt":number+1,"delay_seconds":delay,"message":"本次调用失败，正在重试"})
                        await audit("model.retry",{"slot":slot.role,"next_attempt":number+1,"reason":error.code},None)
                        await asyncio.sleep(delay)
            finally:
                circuit.probing = False
        raise last_error

    async def review(self, factory, audit: AuditCallback, emit: EventCallback, guard=None) -> ModelResult:
        """接收审核上下文工厂、审计/事件回调及权限守卫，仅调用独立审核槽位；瞬时故障最多两次，不降级自审。"""
        slot, circuit = self.review_slot, self.circuits['review']
        slot.endpoint()
        if circuit.opened_until>monotonic() or circuit.probing:
            raise GatewayError('REVIEW_UNAVAILABLE','答案审核模型暂时不可用')
        half_open = circuit.opened_until>0
        circuit.probing = half_open
        try:
            for number in range(1,3):
                if guard:
                    await guard()
                messages = factory(slot)
                try:
                    result = await self._attempt(slot,messages,number,new_id(),audit,emit)
                    circuit.failures,circuit.opened_until = 0,0
                    return result
                except GatewayError as error:
                    circuit.failures += 1
                    if circuit.failures>=self.settings.model_circuit_failures:
                        circuit.opened_until = monotonic()+self.settings.model_circuit_seconds
                    if not error.retryable or half_open or number==2:
                        raise
                    await audit('review.retry',{'attempt':number,'reason':error.code},None)
                    await asyncio.sleep(self.settings.model_retry_backoff_seconds)
        finally:
            circuit.probing = False

    async def _attempt(self, slot: ModelSlot, messages: list[dict], number: int, attempt_id: str, audit: AuditCallback, emit: EventCallback, tools: list[dict] | None=None) -> ModelResult:
        """接收槽位、上下文和尝试标识，执行一次实际 HTTP 流；完整审计后返回标准结果，途中失败保留原始包。"""
        body = slot.request(messages,self.settings.model_output_tokens)
        if tools:
            body['tools'] = tools
            body['tool_choice'] = 'auto'
        metadata = {"attempt_id":attempt_id,"slot":slot.role,"provider":slot.provider,"model":slot.model,"attempt":number}
        # 网关统一复核实际消息和工具预算，独立公开关键词等调用也不会绕过窗口检查。
        counted=count_request(body['messages'],body.get('tools'),slot.model)
        budget={'input_tokens':counted.count,'output_reserved':self.settings.model_output_tokens,'window':slot.window,'safe_limit':slot.safe_limit,
                'method':counted.method,'tokenizer':counted.tokenizer,'source':'estimated'}
        if counted.count+self.settings.model_output_tokens>budget['safe_limit']:
            await audit('model.budget_rejected',{**metadata,'budget':budget},None)
            raise GatewayError('CONTEXT_TOO_LARGE','本次输入与预留输出超过模型安全窗口，请缩小任务')
        await audit("model.request",{**metadata,"request":body,'budget':budget},None)
        chunks, content, reasoning, tools = [],"","",{}
        raw_usage, finish, actual_model, completed = None,None,slot.model,False
        started = monotonic()
        try:
            async with self.client.stream("POST",slot.endpoint(),json=body,headers={"Authorization":"Bearer "+slot.key,"Accept":"text/event-stream"}) as response:
                if response.status_code!=200:
                    payload = (await response.aread())[:16384].decode("utf-8",errors="replace")
                    raise classify_status(response.status_code,payload)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        completed = True
                        break
                    if not data:
                        continue
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        raise GatewayError("MODEL_PROTOCOL_ERROR","模型返回了无法识别的流式数据",True,raw=data[:1000])
                    if not isinstance(chunk,dict):
                        raise GatewayError("MODEL_PROTOCOL_ERROR","模型响应格式不符合要求",True)
                    chunks.append(chunk)
                    await audit("model.chunk",{**metadata,"chunk":chunk},None)
                    if chunk.get("error"):
                        raise GatewayError("MODEL_STREAM_ERROR","模型在生成过程中返回错误",True,raw=chunk["error"])
                    actual_model = chunk.get("model") or actual_model
                    if chunk.get("usage"):
                        raw_usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content") or ""
                        if not isinstance(text,str):
                            raise GatewayError("MODEL_PROTOCOL_ERROR","模型返回的文本格式不正确",True)
                        if text:
                            content += text
                            await emit("assistant.delta",{"text":text})
                        reasoning += delta.get("reasoning_content") or ""
                        for tool in delta.get("tool_calls") or []:
                            index = tool.get("index",0)
                            merged = tools.setdefault(index,{"id":"","type":"function","function":{"name":"","arguments":""}})
                            merged["id"] += tool.get("id") or ""
                            for name in ("name","arguments"):
                                merged["function"][name] += (tool.get("function") or {}).get(name) or ""
                        finish = choice.get("finish_reason") or finish
                    if len(content.encode())+len(reasoning.encode())>2_000_000 or len(chunks)>20000:
                        raise GatewayError("MODEL_RESPONSE_TOO_LARGE","模型输出超过本次处理限制")
            if not completed or not finish or (not content and not tools):
                raise GatewayError("MODEL_STREAM_INCOMPLETE","模型响应中断或没有完整结束",True)
            merged = {"role":"assistant","content":content}
            if reasoning:
                merged["reasoning_content"] = reasoning
            if tools:
                merged["tool_calls"] = [tools[key] for key in sorted(tools)]
            usage = usage_from_response(raw_usage,body["messages"],content,actual_model,body.get('tools'),merged)
            await audit("model.response",{**metadata,"actual_model":actual_model,"chunks":chunks,"merged":merged,
                "finish_reason":finish,"usage":raw_usage,"duration_ms":(monotonic()-started)*1000},usage)
            return ModelResult(content,merged,usage,actual_model,slot.provider,slot.role,attempt_id,finish)
        except (httpx.TimeoutException,httpx.TransportError) as error:
            classified = GatewayError("MODEL_TIMEOUT" if isinstance(error,httpx.TimeoutException) else "MODEL_CONNECTION_ERROR",
                "模型连接超时" if isinstance(error,httpx.TimeoutException) else "无法连接模型服务",True,raw={"error_type":type(error).__name__})
        except GatewayError as error:
            classified = error
        except asyncio.CancelledError:
            await audit("model.interrupted",{**metadata,"chunks":chunks,"partial_content":content,"reason":"cancelled_or_shutdown"},None)
            raise
        await audit("model.failed",{**metadata,**classified.public(),"retryable":classified.retryable,"http_status":classified.status,
            "response":classified.raw,"chunks":chunks,"partial_content":content,"duration_ms":(monotonic()-started)*1000},None)
        raise classified

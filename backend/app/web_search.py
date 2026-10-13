"""阿里云公开搜索、最小查询外发及绑定已校验地址的安全网页读取。"""
import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from app.ids import new_id
from app.model_gateway import GatewayError, ModelGateway
from app.rag_service import live_context
from app.timezones import beijing_now


class SearchError(GatewayError):
    """公开工具错误可回填模型，敏感内部异常不能成为前端或查询内容。"""


def public_url(url: str) -> str:
    """接收候选网页地址，验证协议/凭证/端口与字面地址；返回原公开URL，非法目标直接拒绝。"""
    try:
        parsed = urlsplit(url)
        if (len(url)>4096 or parsed.scheme not in ('http','https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.port not in (None,80,443)
                or re.search(r'[\x00-\x20\\]',url)):
            raise ValueError()
        host = parsed.hostname.lower().rstrip('.')
        if '.' not in host and ':' not in host or host.endswith(('.localhost','.local','.internal')) or host=='localhost':
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and (not address.is_global or address.is_multicast or address.is_reserved or getattr(address,'ipv4_mapped',None) and not address.ipv4_mapped.is_global):
            raise ValueError()
    except (ValueError,TypeError):
        raise SearchError('WEB_TARGET_BLOCKED','网页目标不是允许的公开HTTP/HTTPS地址') from None
    return url


async def resolve_public(host: str, port: int) -> list[str]:
    """接收主机及端口，限时解析全部地址并拒绝混合私网DNS；返回可用于实际连接的公开IP列表。"""
    try:
        rows = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host,port,type=socket.SOCK_STREAM),4)
    except (OSError,TimeoutError):
        raise SearchError('WEB_DNS_FAILED','网页域名解析失败') from None
    addresses = list(dict.fromkeys(row[4][0] for row in rows))
    if not addresses or any(not ipaddress.ip_address(value).is_global or ipaddress.ip_address(value).is_multicast or ipaddress.ip_address(value).is_reserved for value in addresses):
        raise SearchError('WEB_TARGET_BLOCKED','网页域名指向内网或非公开地址')
    return addresses


class PublicNetwork(httpcore.AsyncNetworkBackend):
    """在连接时解析并固定已验证IP，TLS仍使用原域名验证，避免校验后再次DNS解析。"""
    def __init__(self):
        """无参数创建底层网络后端及连接证据；不会读取环境代理或应用凭证。"""
        self.backend = httpcore.AnyIOBackend()
        self.connections = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        """接收HTTP层域名和连接策略，仅连接已验证的公开IP；返回网络流并记录实际连接地址。"""
        addresses = await resolve_public(host,port)
        stream = await self.backend.connect_tcp(addresses[0],port,timeout=timeout,local_address=None,socket_options=socket_options)
        self.connections.append({'host':host,'ip':addresses[0],'port':port})
        return stream

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        """接收Unix路径但始终拒绝，公开网页读取不允许访问宿主socket。"""
        raise SearchError('WEB_TARGET_BLOCKED','公开网页读取不能访问本地socket')

    async def sleep(self, seconds):
        """接收网络层等待秒数，无副作用地异步等待，无返回值。"""
        await asyncio.sleep(seconds)


class TextExtractor(HTMLParser):
    """只提取HTML可见文本，脚本、样式、表单与导航不进入模型，也不执行任何页面内容。"""
    def __init__(self):
        """无参数初始化文本片段与跳过层级；HTML实体转义为普通文字。"""
        super().__init__(convert_charrefs=True)
        self.parts, self.ignored = [], 0

    def handle_starttag(self, tag, attrs):
        """接收HTML标签及属性，记录不可见节点和换行；不访问任何URL或属性脚本。"""
        if tag in ('script','style','head','noscript','svg','form','nav'):
            self.ignored += 1
        if tag in ('p','div','br','li','h1','h2','h3','tr') and not self.ignored:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        """接收结束标签并恢复文本提取层级，无返回值。"""
        if tag in ('script','style','head','noscript','svg','form','nav'):
            self.ignored = max(0,self.ignored-1)

    def handle_data(self, data):
        """接收正文片段，仅保留可见普通文本，无返回值。"""
        if not self.ignored:
            self.parts.append(data)


class PublicPageReader:
    """逐跳校验URL并限制总时间、原始字节和文本长度的公开页面读取器。"""
    def __init__(self, settings):
        """接收服务端限额配置，保存边界；不创建共享Cookie或浏览器会话。"""
        self.settings = settings

    async def read(self, url):
        """接收公开URL，限时获取纯文本快照，返回最终URL/连接/指纹/截断范围；失败不冒充已读正文。"""
        network = PublicNetwork()
        async with asyncio.timeout(self.settings.web_search_timeout_seconds):
            async with httpcore.AsyncConnectionPool(network_backend=network,retries=0) as pool:
                for _ in range(5):
                    public_url(url)
                    async with pool.stream('GET',url,headers={'User-Agent':'Zhixing-PublicReader/0.6','Accept':'text/html,text/plain','Accept-Encoding':'identity'},
                                           extensions={'timeout':{'connect':4,'read':6,'write':4,'pool':4}}) as response:
                        headers = {key.lower():value for key,value in response.headers}
                        if response.status in (301,302,303,307,308):
                            location = headers.get(b'location',b'').decode('latin1')
                            if not location:
                                raise SearchError('WEB_PAGE_FAILED','网页重定向缺少目标')
                            url = public_url(urljoin(url,location))
                            continue
                        if response.status!=200:
                            raise SearchError('WEB_PAGE_FAILED','网页未能公开读取')
                        media = headers.get(b'content-type',b'').decode('latin1').lower()
                        if not media.startswith(('text/html','text/plain','application/xhtml+xml')) or headers.get(b'content-encoding',b'identity') not in (b'identity',b''):
                            raise SearchError('WEB_PAGE_TYPE','只支持公开HTML/文本，不读取压缩或可执行下载')
                        data = bytearray()
                        async for block in response.aiter_stream():
                            data.extend(block)
                            if len(data)>self.settings.web_search_response_bytes:
                                raise SearchError('WEB_RESPONSE_LIMIT','网页响应超过大小限制')
                    encoding = re.search(r'charset=([\w-]+)',media)
                    try:
                        body = bytes(data).decode(encoding[1] if encoding else 'utf-8',errors='replace')
                    except LookupError:
                        body = bytes(data).decode('utf-8',errors='replace')
                    if 'html' in media:
                        parser = TextExtractor()
                        parser.feed(body)
                        body = ''.join(parser.parts)
                    text = re.sub(r'[ \t]+',' ',body).strip()
                    return {'url':url,'content':text[:self.settings.web_search_text_chars],'content_sha256':hashlib.sha256(text.encode()).hexdigest(),
                            'truncated':len(text)>self.settings.web_search_text_chars,'received_bytes':len(data),'connections':network.connections}
        raise SearchError('WEB_REDIRECT_LIMIT','网页重定向次数超过限制')


def check_public_input(text: str, secrets: tuple[str,...]=()) -> None:
    """接收用户文本或生成关键词及已知凭证，轻量拒绝凭证/明显私密载荷；正常无返回，不读取RAG或业务数据。"""
    # 只检查输入本身，不能宣称识别所有语义秘密，更不使用内部正文做比对。
    secret_pattern = r'(?:sk-[\w-]{8,}|(?:api[_ -]?key|密码|密钥|身份证|手机号|保密资料)\s*[:：=]|[\w.+-]+@[\w.-]+\.[a-z]{2,})'
    private_payload = r'(?:内部报价|报价明细|客户明细|客户资料|员工明细|工资明细|保密资料)'
    if any(secret and len(secret)>=6 and secret in text for secret in secrets) or re.search(secret_pattern+'|'+private_payload,text,re.I):
        raise SearchError('WEB_QUERY_PRIVATE','搜索内容可能包含私密数据，请另给可公开的关键词')


def outbound_query(current_input: str, secrets: tuple[str,...]=()) -> str:
    """接收本次用户输入及已知凭证，只提取公开任务子句；返回查询主题，不接收主Agent候选或任何RAG结果。"""
    check_public_input(current_input,secrets)
    private_pattern = r'(?:我司|本企业|本公司|我们公司|内部|保密|上传|附件|数据库|工单|考勤|员工明细|工资明细)'
    clauses = [re.sub(r'[,，](?:分别说明|给出|给出处|给官方|提供出处|返回出处|列出来源|请给|并给).*','',value.strip(' ，,'))
               for value in re.split(r'[；;\n。]|(?:另外|以及|同时|并且)',current_input) if value.strip(' ，,')]
    public_clauses = [value for value in clauses if not re.search(private_pattern,value)]
    if len(public_clauses)!=len(clauses):
        public_clauses = [value for value in public_clauses if re.search(r'公开|联网|互联网|网上|官网',value)]
    query = '；'.join(public_clauses).strip()
    if not query or len(query)>240:
        raise SearchError('WEB_QUERY_PRIVATE','无法确定安全的公开检索主题，请另给可公开关键词')
    query = re.sub(r'^.*?(?:搜索|查询|查找)\s*','',query,count=1)
    if not query.strip():
        raise SearchError('WEB_QUERY_PRIVATE','请提供明确的公开检索主题')
    return query


def public_location_context(current_input,personal):
    """接收原始当前问题和服务器本人概要，仅在本地指代时返回粗略城市；不外发画像/项目或精确住址。"""
    if not re.search(r'我的所在地|我的位置|我所在|我住的|我附近|我这(?:里|边)|这里|当地|^附近|(?:推荐|找|查)[一下些]*附近',current_input):return {}
    explicit=re.search(r'我(?:现在|目前)?(?:住在|在|位于)\s*([\u4e00-\u9fffA-Za-z ·-]{2,24}?)(?=[，。；！？\s]|附近|$)',current_input)
    location=explicit[1] if explicit else personal.get('runtime_fields',{}).get('location') or personal.get('profile',{}).get('location')
    if not isinstance(location,str) or not re.fullmatch(r'[\u4e00-\u9fffA-Za-z ·-]{2,24}',location):return {}
    if re.search(r'街|路|号|栋|室|小区|公司|办公室|密码|密钥',location):return {}
    check_public_input(location)
    return {'city':location}


class WebSearchService:
    """真实IQS查询及本人来源快照，采用固定提供商、不启用缓存，错误返回实际状态。"""
    def __init__(self, pool, settings, audit, client=None, reader=None):
        """接收数据库、配置、审计及可选边界测试依赖；建立无代理、无重试、无Cookie的搜索客户端。"""
        self.pool, self.settings, self.audit = pool,settings,audit
        self.client = client or httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=settings.web_search_timeout_seconds)
        self.owns_client = client is None
        self.reader = reader or PublicPageReader(settings)
        self.query_gateway = ModelGateway(settings) if client is None else None

    async def close(self):
        """无参数关闭本服务拥有的HTTP客户端，不关闭测试注入客户端。"""
        if self.owns_client:
            await self.client.aclose()
        if self.query_gateway:
            await self.query_gateway.close()

    async def public_keywords(self, task, run, public_context=None):
        """接收已投影的公开任务及可信运行，用独立无历史/无工具上下文提炼关键词；返回受限查询。"""
        city=(public_context or {}).get('city')
        if city:check_public_input(city,self.audit.secrets)
        if self.query_gateway is None:
            return (city+' '+task) if city else task
        messages=[{'role':'system','content':'你是公开搜索关键词提炼器。输入是用户本次公开任务和服务器解析的可选粗略城市，不含内部工具或其他个人信息。遇到所在地/附近等指代时必须用city替换，最终关键词保留city原词，不保留未解析的附近，不猜其他城市。提炼一条简短自然关键词，保留产品/型号和时效，去除礼貌与回答格式要求。只返回JSON：{"query":"关键词"}。不回答、不输出其他内容、不调用工具。'},
                  {'role':'user','content':json.dumps({'public_task':task,'city':city},ensure_ascii=False) if city else task}]
        async def record(kind,data,usage):
            """接收实际查询提炼协议和用量，将其作为当前run子步骤永久审计，不伪造主回答。"""
            await self.audit.append(run['trace_id'],'web.query_'+kind,data,user_id=run['user_id'],run_id=run['run_id'],usage=usage,
                                    span_id=data.get('attempt_id'),parent_span_id=run['run_id'],span_name='web.query_model')
        async def silent(kind,data):
            """接收网关事件但不展示关键词生成流，实际协议由record记录，无返回值。"""
            return None
        result=await self.query_gateway.complete(lambda slot:messages,record,silent)
        try:
            value=json.loads(result.content.strip().removeprefix('```json').removesuffix('```').strip())['query']
            if not isinstance(value,str) or not value.strip() or len(value)>150:
                raise ValueError()
        except (ValueError,KeyError,TypeError):
            raise SearchError('WEB_QUERY_FORMAT','公开关键词生成未完成，请提供更明确的公开主题') from None
        check_public_input(value,self.audit.secrets)
        # 城市为服务端最小投影；模型改写不能丢失它，候选不含城市时后端补回。
        if city and city not in value:value=city+' '+value
        return value

    async def search(self, current_input, context, run, public_context=None):
        """接收用户本次输入与可信身份/任务，轻量校验并独立生成公开关键词，返回来源；不接收RAG、SQL或主Agent候选。"""
        context = await live_context(self.pool,context.user_id,context.session_id)
        public_task = outbound_query(current_input,self.audit.secrets)
        settings = self.settings
        if settings.web_search_provider!='aliyun_iqs' or settings.web_search_engine_type!='CNLiteBasic' or settings.web_search_base_url.rstrip('/')!='https://cloud-iqs.aliyuncs.com' or not settings.web_search_api_key.get_secret_value():
            raise SearchError('WEB_NOT_CONFIGURED','阿里云LiteBasic搜索尚未正确配置')
        query = await self.public_keywords(public_task,run,public_context)
        context = await live_context(self.pool,context.user_id,context.session_id)
        search_id = new_id()
        # 用户级配额领取与持久请求记录在同一事务内，避免并发绕过限制。
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1))','web:'+context.user_id)
                count = await conn.fetchval("SELECT count(*) FROM app_runtime.web_searches WHERE user_id=$1 AND created_at>now()-interval '1 minute'",context.user_id)
                if count>=settings.web_search_per_minute:
                    raise SearchError('WEB_QUOTA','公开搜索调用过于频繁，请稍后再试')
                await conn.execute('INSERT INTO app_runtime.web_searches(search_id,user_id,conversation_id,run_id,query,status) VALUES($1,$2,$3,$4,$5,\'running\')',
                                   search_id,context.user_id,run['conversation_id'],run['run_id'],query)
                body = {'query':query,'engineType':'CNLiteBasic','contents':{'mainText':True,'summary':False,'markdownText':False,'rerankScore':True},
                        'advancedParams':{'numResults':settings.web_search_max_results}}
                await self.audit.append(run['trace_id'],'web.request',{'search_id':search_id,'public_task':public_task,'public_context':public_context or {},'outbound_query':query,'query_origin':'current_public_task_with_optional_own_city','request':body},
                                        user_id=context.user_id,run_id=run['run_id'],connection=conn)
        try:
            async with asyncio.timeout(settings.web_search_timeout_seconds):
                async with self.client.stream('POST',settings.web_search_base_url.rstrip('/')+'/search/unified',json=body,
                                              headers={'Authorization':'Bearer '+settings.web_search_api_key.get_secret_value()}) as response:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data)>settings.web_search_response_bytes:
                            raise SearchError('WEB_RESPONSE_LIMIT','搜索响应超过大小限制')
                    if response.status_code!=200:
                        code = 'WEB_RATE_LIMIT' if response.status_code==429 else 'WEB_PROVIDER_FAILED'
                        raise SearchError(code,'搜索提供商限流或拒绝本次请求',status=response.status_code)
            raw = json.loads(data)
            if not isinstance(raw,dict) or not isinstance(raw.get('pageItems'),list):
                raise SearchError('WEB_PROTOCOL','搜索服务未返回有效网页结果')
            await self.audit.append(run['trace_id'],'web.provider_response',{'search_id':search_id,'response':raw},user_id=context.user_id,run_id=run['run_id'])
            sources = []
            for item in raw['pageItems'][:settings.web_search_max_results]:
                try:
                    url = public_url(item['link'])
                except (SearchError,KeyError,TypeError):
                    continue
                # 提供商正文不代表本服务连接过来源；DNS与实际连接只由安全读取器校验。
                source = {'type':'web','source_id':new_id(),'title':str(item.get('title') or urlsplit(url).hostname)[:250],
                          'url':url,'website':str(item.get('hostname') or urlsplit(url).hostname)[:150],
                          'published_at':item.get('publishedTime') or None,'fetched_at':beijing_now().isoformat(),
                          'content':str(item.get('mainText') or item.get('snippet') or '')[:settings.web_search_text_chars],
                          'content_kind':'provider_excerpt' if item.get('mainText') else 'search_snippet','truncated':True,'page_read':False}
                if len(sources)<settings.web_search_max_pages:
                    try:
                        page = await self.reader.read(url)
                        source.update(page,content_kind='page_text',page_read=True)
                    except (SearchError,OSError,TimeoutError,httpcore.NetworkError,httpcore.TimeoutException,httpcore.ProtocolError) as error:
                        source['page_error'] = error.code if isinstance(error,SearchError) else 'WEB_PAGE_UNAVAILABLE'
                source.setdefault('content_sha256',hashlib.sha256(source['content'].encode()).hexdigest())
                async with self.pool.acquire() as conn:
                    await conn.execute('INSERT INTO app_runtime.web_sources(source_id,search_id,snapshot_json) VALUES($1,$2,$3)',source['source_id'],search_id,source)
                sources.append(source)
            await live_context(self.pool,context.user_id,context.session_id)
            output = {'search_id':search_id,'query':query,'citations':sources,'status':'completed' if sources else 'no_results','cost_source':'reported' if raw.get('costCredits') else 'unavailable',
                      'cost_credits':raw.get('costCredits'),'message':'已取得公开来源；片段不等于完整网页，日期未知时保持空值' if sources else '公开搜索没有可用结果，不能虚构网页来源'}
            output['query_policy']='实际外发搜索词以本回执query为准，主模型候选不是实际搜索词；输入仅本次公开任务及可选本人粗略城市。重复调用复用本轮结果，来源不匹配时不能凭候选归因供应商。'
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute('UPDATE app_runtime.web_searches SET status=$1,response_json=$2 WHERE search_id=$3',output['status'],output,search_id)
                    await self.audit.append(run['trace_id'],'web.completed',output,user_id=context.user_id,run_id=run['run_id'],connection=conn)
            return output
        except BaseException as error:
            if isinstance(error,asyncio.CancelledError):
                public = {'code':'WEB_CANCELLED','message':'公开搜索已取消'}
            elif isinstance(error,SearchError):
                public = error.public()
            else:
                public = {'code':'WEB_TIMEOUT' if isinstance(error,(TimeoutError,httpx.TimeoutException)) else 'WEB_UNAVAILABLE','message':'公开搜索超时或暂不可用'}
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute('UPDATE app_runtime.web_searches SET status=\'failed\',response_json=$1 WHERE search_id=$2',public,search_id)
                    await self.audit.append(run['trace_id'],'web.failed',{'search_id':search_id,**public},user_id=context.user_id,run_id=run['run_id'],connection=conn)
            if isinstance(error,asyncio.CancelledError):
                raise
            raise SearchError(public['code'],public['message']) from None

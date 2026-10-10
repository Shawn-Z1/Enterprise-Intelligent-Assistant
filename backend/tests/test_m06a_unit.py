"""M06A仅覆盖出网边界与协议关键故障，不将测试替身当作搜索成功证据。"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from app.config import Settings
from app.tool_registry import available_tools
from app.web_search import SearchError, public_url, resolve_public, outbound_query, check_public_input, PublicNetwork, PublicPageReader, TextExtractor


class WebBoundaries(unittest.IsolatedAsyncioTestCase):
    """公开URL、固定连接IP、查询来源和网页限额的确定性验证。"""
    def test_url_and_query_boundaries(self):
        """无参数验证私网/非HTTP/凭证目标及私密搜索拒绝，并保留复合任务的公开子句。"""
        for url in ('http://127.0.0.1','http://[::1]','http://169.254.169.254','http://postgres','http://10.1.2.3','file:///etc/passwd','https://user:pass@example.com','http://[::ffff:127.0.0.1]','http://224.0.0.1','http://[ff02::1]'):
            with self.subTest(url=url),self.assertRaises(SearchError):public_url(url)
        self.assertEqual(public_url('https://www.gov.cn/'),'https://www.gov.cn/')
        self.assertEqual(outbound_query('先查本公司报销制度；另外公开搜索阿里云IQS最新价格'),'阿里云IQS最新价格')
        for user in ('内部工资明细','搜索公开新闻 API Key=secret123','我们给客户甲的内部报价是8万元，查一下竞争对手价格。'):
            with self.subTest(user=user),self.assertRaises(SearchError):outbound_query(user)
        with self.assertRaises(SearchError):check_public_input('API Key=generated-secret')

    async def test_dns_pin_and_mixed_private(self):
        """无参数验证混合DNS拒绝，实际网络后端只接收校验后的固定IP，不重新解析原域名。"""
        loop=asyncio.get_running_loop()
        with patch.object(loop,'getaddrinfo',new=AsyncMock(return_value=[(2,1,6,'',('8.8.8.8',443)),(2,1,6,'',('127.0.0.1',443))])):
            with self.assertRaises(SearchError):await resolve_public('test.example',443)
        network=PublicNetwork()
        backend=AsyncMock()
        network.backend=backend
        with patch('app.web_search.resolve_public',new=AsyncMock(return_value=['8.8.8.8'])):
            await network.connect_tcp('test.example',443)
        self.assertEqual(backend.connect_tcp.call_args.args,('8.8.8.8',443))
        self.assertEqual(network.connections[0]['ip'],'8.8.8.8')

    def test_tools_and_script_text(self):
        """无参数验证无权限目录不含知识工具，网页脚本与表单内容不被当作正文。"""
        names=lambda context:[tool['function']['name'] for tool in available_tools(context,True)]
        self.assertEqual(names(SimpleNamespace(can_use_knowledge=False)),['search_web'])
        self.assertEqual(names(SimpleNamespace(can_use_knowledge=True)),['search_knowledge','search_web'])
        parser=TextExtractor();parser.feed('<script>send_mail()</script><style>secret</style><p>公开事实</p>')
        self.assertIn('公开事实',''.join(parser.parts));self.assertNotIn('send_mail',''.join(parser.parts))

    async def test_actual_http_redirect_and_response_limit(self):
        """无参数启动本地可控HTTP服务，验证真实重定向复检和超大响应；仅测试网络映射允许连接本测试端口。"""
        async def handler(reader,writer):
            """接收测试连接，按请求路径输出内网重定向或超大正文，关闭测试流。"""
            request=await reader.read(4096)
            if b'/redirect' in request:
                writer.write(b'HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\nContent-Length: 0\r\n\r\n')
            else:
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2048\r\n\r\n'+b'x'*2048)
            await writer.drain();writer.close();await writer.wait_closed()
        server=await asyncio.start_server(handler,'127.0.0.1',0)
        port=server.sockets[0].getsockname()[1]
        test_port=port
        async def fixture_connect(network,host,port,**kwargs):
            """将测试公开主机明确映射到专属夹具端口，不修改生产校验或其他目标。"""
            return await network.backend.connect_tcp('127.0.0.1',test_port,**kwargs)
        reader=PublicPageReader(SimpleNamespace(web_search_timeout_seconds=3,web_search_response_bytes=1024,web_search_text_chars=500))
        try:
            with patch.object(PublicNetwork,'connect_tcp',fixture_connect):
                for path,code in (('redirect','WEB_TARGET_BLOCKED'),('large','WEB_RESPONSE_LIMIT')):
                    with self.assertRaises(SearchError) as caught:await reader.read('http://public.example/'+path)
                    self.assertEqual(caught.exception.code,code)
        finally:
            server.close();await server.wait_closed()


if __name__=='__main__':unittest.main()

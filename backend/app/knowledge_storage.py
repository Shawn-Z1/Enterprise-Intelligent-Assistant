import asyncio
import base64
import hashlib
import json
import sys
from io import BytesIO

import urllib3
from minio import Minio

from app.auth import auth_error


class KnowledgeStorage:
    """只使用应用 Bucket 保存原件；正文读取前由服务鉴权，解析放在有界子进程中。"""

    def __init__(self,settings):
        """接收运行配置，创建有界 MinIO 客户端并保存大小/时间限制，不读取用户指定路径。"""
        self.settings = settings
        self.bucket = settings.app_minio_bucket
        # 解析与渲染各最多同时运行两个子进程，避免 PDF 并发请求无界启动进程。
        self.parse_slots = asyncio.Semaphore(min(2,settings.worker_concurrency))
        self.client = Minio(settings.app_minio_endpoint,access_key=settings.app_minio_access_key,
            secret_key=settings.app_minio_secret_key.get_secret_value(),secure=False,region='us-east-1',
            http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=3,read=15),retries=0))

    async def put(self,key: str,data: bytes):
        """接收服务端对象键与原件字节，上传到唯一应用对象；无返回值，不覆盖其他申请对象。"""
        await asyncio.to_thread(self.client.put_object,self.bucket,key,BytesIO(data),len(data),content_type='application/octet-stream')

    async def remove_new(self,key: str):
        """接收本次未提交成功的新对象键，清理失败上传；只能用于新对象，不能清理已有知识历史。"""
        await asyncio.to_thread(self.client.remove_object,self.bucket,key)

    def read_sync(self,key: str) -> bytes:
        """接收内部对象键，有界读取并释放 SDK 连接；返回原始字节，不生成绕过鉴权的下载链接。"""
        stream = self.client.get_object(self.bucket,key)
        try:
            value = stream.read(self.settings.knowledge_max_file_bytes+1)
            if len(value)>self.settings.knowledge_max_file_bytes:
                raise auth_error(413,'FILE_LIMIT','原件超过大小限制')
            return value
        finally:
            stream.close()
            stream.release_conn()

    async def read_verified(self,row: dict) -> bytes:
        """接收已鉴权申请，读取其内部对象并检查实际 SHA-256；原件变更时拒绝，不使用过期批准。"""
        value = await asyncio.to_thread(self.read_sync,row['object_key'])
        if hashlib.sha256(value).hexdigest()!=row['original_sha256']:
            raise auth_error(409,'SOURCE_CHANGED','原件已发生变化，请重新提交，原批准不可继续使用')
        return value

    async def parse(self,data: bytes,kind: str,page: int | None=None) -> dict:
        """接收原件、格式和可选页码，取得有限解析名额后执行隔离处理，返回正文或页面。"""
        async with self.parse_slots:
            return await self._parse(data,kind,page)

    async def _parse(self,data: bytes,kind: str,page: int | None=None) -> dict:
        """接收原件、格式及可选 PDF 页码，启动独立有界子进程；返回正文或页面，超时/取消会终止子进程。"""
        process = await asyncio.create_subprocess_exec(sys.executable,'-m','app.file_parser',stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        payload = json.dumps({'data':base64.b64encode(data).decode(),'format':kind,'operation':'render' if page else 'parse','page':page}).encode()
        try:
            output,_ = await asyncio.wait_for(process.communicate(payload),timeout=self.settings.knowledge_parse_timeout_seconds)
        except (TimeoutError,asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
                await process.wait()
            if asyncio.current_task().cancelling():
                raise
            raise auth_error(422,'PARSE_TIMEOUT','文件处理超时，请缩小文件后重试')
        if process.returncode!=0 or len(output)>12*1024*1024:
            raise auth_error(422,'PARSE_RESOURCE_LIMIT','文件处理超过资源限制或格式异常')
        try:
            result = json.loads(output)
        except ValueError:
            raise auth_error(422,'INVALID_FILE','文件无法解析')
        if not result.get('ok'):
            raise auth_error(422,result['error']['code'],result['error']['message'])
        return result['result']

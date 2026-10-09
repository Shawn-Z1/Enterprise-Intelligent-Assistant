import os
from io import BytesIO
import unittest
from concurrent.futures import ThreadPoolExecutor
from app.ids import new_id, ShortId

from fastapi.testclient import TestClient
from minio import Minio
from minio.error import S3Error

from app.config import Settings
from app.events import InfrastructureEvents
from app.main import app


class FoundationTests(unittest.TestCase):
    """在真实 Compose 网络内验证基础接口及进程事件契约。"""

    def test_configuration_secrets(self):
        """验证 SecretStr 表示不会输出环境密码；无参数，无返回值。"""
        settings = Settings()
        self.assertNotIn(os.environ["POSTGRES_PASSWORD"], repr(settings))
        self.assertNotIn(os.environ["APP_MINIO_SECRET_KEY"], repr(settings))

    def test_real_minio_roundtrip_and_isolation(self):
        """实际写读应用文件并验证越权 Bucket 被拒；只清理本测试创建的对象。"""
        settings = Settings()
        client = Minio(settings.app_minio_endpoint, access_key=settings.app_minio_access_key,
                       secret_key=settings.app_minio_secret_key.get_secret_value(), secure=False,
                       region="us-east-1")
        # 随机对象键仅用于验收，不能碰到已有业务文件。
        object_key = f"verification/m01/{new_id()}.txt"
        payload = "M01 真实文件存储验收".encode("utf-8")
        try:
            client.put_object(settings.app_minio_bucket, object_key, BytesIO(payload), len(payload))
            response = client.get_object(settings.app_minio_bucket, object_key)
            try:
                self.assertEqual(response.read(), payload)
            finally:
                response.close()
                response.release_conn()
            with self.assertRaises(S3Error) as caught:
                client.bucket_exists("milvus-data")
            self.assertEqual(caught.exception.code, "AccessDenied")
        finally:
            client.remove_object(settings.app_minio_bucket, object_key)

    def test_event_window_and_concurrency(self):
        """并发追加事件，验证窗口上限与唯一递增序号；无参数，无返回值。"""
        events = InfrastructureEvents(capacity=7)
        trace_id = new_id()
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda number: events.record(trace_id, "test.event", {"number": number}), range(30)))
        stored = events.read(after=0, limit=100)
        self.assertEqual([event.event_id for event in stored], list(range(24, 31)))
        self.assertEqual(len(events.read(after=28, limit=1)), 1)
        self.assertTrue(all(event.timestamp.utcoffset().total_seconds() == 8*3600 for event in stored))

    def test_live_and_real_readiness(self):
        """实际访问存储与检索模型五种 Compose 依赖并核对追踪头；无参数，无返回值。"""
        with TestClient(app) as client:
            live = client.get("/api/health/live")
            self.assertEqual(live.status_code, 200)
            self.assertEqual(live.json()["status"], "alive")
            response = client.get("/api/health/ready")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["trace_id"], response.headers["X-Trace-ID"])
            self.assertRegex(response.json()["trace_id"], r"^[A-Za-z0-9_-]{16}$")
            self.assertEqual(set(response.json()["dependencies"]), {"postgres", "redis", "milvus", "minio", "retrieval_models"})
            self.assertTrue(all(item["status"] == "ready" for item in response.json()["dependencies"].values()))
            events = client.get("/api/events").json()
            self.assertFalse(events["durable"])
            self.assertEqual(events["scope"], "infrastructure_only")
            self.assertEqual(events["events"][-1]["trace_id"], response.json()["trace_id"])

    def test_real_failed_dependency(self):
        """将 Redis 探针指向真实不可用端口，验证 503 与存活 200；无参数，无返回值。"""
        with TestClient(app) as client:
            app.state.settings = app.state.settings.model_copy(update={"redis_port": 1})
            response = client.get("/api/health/ready")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["dependencies"]["redis"]["status"], "unavailable")
            self.assertEqual(client.get("/api/health/live").status_code, 200)

    def test_error_envelopes_and_disabled_business(self):
        """验证边界参数、未知路径及未实现登录均不会假成功；无参数，无返回值。"""
        with TestClient(app) as client:
            for path in ("/api/events?after=-1", "/api/events?limit=101"):
                response = client.get(path)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")
                self.assertEqual(response.json()["trace_id"], response.headers["X-Trace-ID"])
            response = client.post("/api/auth/login", json={"account": "preview", "password": "not-a-real-password"})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("not-a-real-password", response.text)
            self.assertIn("trace_id", response.json())


if __name__ == "__main__":
    unittest.main()

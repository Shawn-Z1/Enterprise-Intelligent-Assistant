import argparse
import asyncio
import re
from app.ids import new_id, ShortId

from app.audit import AuditRecorder
from app.db_admin import BootstrapSettings, connect_admin


async def cleanup(username: str) -> None:
    """接收本次浏览器测试账号名，确认零权限且无业务记录后清理账号/会话，保留全部永久审计。"""
    if not re.fullmatch(r"m03_browser_[a-f0-9]{8}",username):
        raise ValueError("仅允许清理明确的浏览器测试账号")
    conn = await connect_admin(BootstrapSettings())
    try:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM app_system.users WHERE username=$1 FOR UPDATE",username)
            if row is None:
                return
            if (row["display_name"] != "浏览器验收成员" or row["role"] != "member" or row["department_id"] is not None
                    or row["grade"] is not None or row["employee_id"] is not None or row["knowledge_enabled"] or row["business_query_enabled"]):
                raise RuntimeError("测试账号状态不符合清理条件")
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM app_runtime.conversations WHERE user_id=$1)",row["user_id"]):
                raise RuntimeError("已有会话数据，拒绝清理")
            await AuditRecorder(None,"M03-verification").append(new_id(),"verification.account_cleaned",
                {"username":username,"user_id":str(row["user_id"]),"audit_retained":True},connection=conn)
            await conn.execute("DELETE FROM app_system.auth_sessions WHERE user_id=$1",row["user_id"])
            await conn.execute("DELETE FROM app_system.users WHERE user_id=$1",row["user_id"])
        print("浏览器测试账号已清理，永久审计记录保留。")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    asyncio.run(cleanup(parser.parse_args().username))

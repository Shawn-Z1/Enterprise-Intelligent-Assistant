from datetime import datetime, timedelta, timezone


# 业务统一使用北京时间；保留带时区类型，不能通过加八小时改变历史记录的真实时刻。
BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
PG_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def beijing_now() -> datetime:
    """无参数，返回带 +08:00 偏移的当前北京时间，供新记录、事件和输出使用。"""
    return datetime.now(BEIJING)


def encode_timestamp(value: datetime) -> tuple[int]:
    """接收带时区日期，转为 PG 协议的微秒元组；拒绝无时区输入，返回不改变真实时刻的编码。"""
    if value.tzinfo is None:
        raise ValueError("数据库时间必须携带明确时区")
    delta = value - PG_EPOCH
    return ((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds,)


def decode_timestamp(value: tuple[int]) -> datetime:
    """接收 PG 微秒元组，返回北京时间 datetime；保留正负无穷时间的协议边界值。"""
    if value[0] == 2**63-1:
        return datetime.max.replace(tzinfo=BEIJING)
    if value[0] == -(2**63):
        return datetime.min.replace(tzinfo=BEIJING)
    return (PG_EPOCH + timedelta(microseconds=value[0])).astimezone(BEIJING)

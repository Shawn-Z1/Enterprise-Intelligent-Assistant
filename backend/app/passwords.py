from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# 明确固定 Argon2id 参数，后续登录复用，盐由库独立生成。
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def hash_password(password: str) -> str:
    """接收明文密码并生成带随机盐的 Argon2id 哈希；返回编码哈希，不输出日志。"""
    if len(password) < 12 or password == "replace-with-random-password":
        raise ValueError("初始密码至少 12 位，且不能使用示例占位值")
    return _hasher.hash(password)


def verify_password(encoded_hash: str, password: str) -> bool:
    """接收编码哈希和待校验密码；正确返回 true，错误或非法哈希返回 false。"""
    try:
        return _hasher.verify(encoded_hash, password)
    except (InvalidHashError, VerificationError):
        return False

"""Multica Plugin hook 签名校验。

契约来自上游源码 server/internal/service/plugin_hook.go（v0.4.38 / v0.4.43 一致）：

  签名串   = HMAC-SHA256(key, timestamp + "." + body)
  key      = HMAC-SHA256(deployment_key, "multica-plugin-hook-signature:v1:" + installation_id)
  请求头   X-Multica-Signature: v1=<hex>
           X-Multica-Timestamp: <unix seconds>
           X-Multica-Plugin-Installation: <installation uuid>
  展示形式 whsec_<hex>            （安装时一次性展示给插件作者的那个值）
  时间容差 5 分钟

注意 key 是 raw bytes 的 HMAC 输出，不是 hex 串本身；whsec_ 前缀后的 hex
解码回 bytes 才是 HMAC 的 key。这点写反了签名恒不匹配。
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_VERSION = "v1"
DERIVE_PREFIX = b"multica-plugin-hook-signature:v1:"
SECRET_DISPLAY_PREFIX = "whsec_"
# 上游 hookTimestampTolerance = 5 * time.Minute
TIMESTAMP_TOLERANCE_SECONDS = 300


class SignatureError(Exception):
    """签名校验失败。调用方应答 401，且不得透露具体失败原因给对端。"""


def derive_hook_key(deployment_key: bytes, installation_id: str) -> bytes:
    """由部署密钥派生出某个 installation 的签名 key。

    对应上游 PluginService.hookSigningKey。部署密钥必须是 32 字节，
    上游在长度不符时直接拒绝启用 hook，这里保持同样的 fail-closed。
    """
    if len(deployment_key) != 32:
        raise SignatureError("deployment key must decode to exactly 32 bytes")
    mac = hmac.new(deployment_key, digestmod=hashlib.sha256)
    mac.update(DERIVE_PREFIX)
    mac.update(installation_id.encode("utf-8"))
    return mac.digest()


def format_secret(key: bytes) -> str:
    """转成作者在安装界面看到的 whsec_ 展示形式。"""
    return SECRET_DISPLAY_PREFIX + key.hex()


def parse_secret(secret: str) -> bytes:
    """把 whsec_<hex> 还原成 HMAC key 的 raw bytes。"""
    raw = secret[len(SECRET_DISPLAY_PREFIX):] if secret.startswith(SECRET_DISPLAY_PREFIX) else secret
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise SignatureError("signing secret is not valid hex") from exc
    if not key:
        raise SignatureError("signing secret is empty")
    return key


def compute_signature(key: bytes, timestamp: str, body: bytes) -> str:
    """计算期望签名的 hex。

    上游把 timestamp 和 body 用 "." 连接后签名，而不是直接拼接：
    只签 body 会让抓到的请求永久可重放；朴素拼接则允许在两个字段之间挪动字节。
    """
    mac = hmac.new(key, digestmod=hashlib.sha256)
    mac.update(timestamp.encode("utf-8"))
    mac.update(b".")
    mac.update(body)
    return mac.hexdigest()


def verify(
    key: bytes,
    timestamp: str,
    body: bytes,
    presented: str,
    now: float | None = None,
    tolerance: int = TIMESTAMP_TOLERANCE_SECONDS,
) -> None:
    """校验一次投递。通过则返回 None，否则抛 SignatureError。

    对应上游 plugincontract 的 VerifyHookSignature，检查顺序保持一致：
    先确认版本前缀，再查时间窗，最后做等时比较。
    """
    if not presented.startswith(SIGNATURE_VERSION + "="):
        raise SignatureError("signature must carry the v1= scheme prefix")
    presented_hex = presented[len(SIGNATURE_VERSION) + 1:]

    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise SignatureError("timestamp is not an integer") from exc

    current = time.time() if now is None else now
    # 双向容差：时钟可能快也可能慢，未来方向同样要挡，否则签一个远未来的
    # 时间戳就得到一张长期有效的通行证。
    if abs(current - sent_at) > tolerance:
        raise SignatureError("timestamp outside the accepted window")

    expected = compute_signature(key, timestamp, body)
    # 等时比较：普通 == 会按首个不同字节提前返回，泄露可用于逐字节爆破的时序差。
    if not hmac.compare_digest(expected, presented_hex):
        raise SignatureError("signature mismatch")

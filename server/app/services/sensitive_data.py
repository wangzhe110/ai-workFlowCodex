"""持久化前的统一敏感字段清理工具。

本模块只按字段名判断敏感内容，不扫描普通文本值。这样提示词中出现
``token`` 等业务字样不会被误删，同时任何嵌套请求、响应或错误对象都能
在进入 ORM 之前得到同一套处理。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import re


REDACTED_VALUE = "[REDACTED]"

# 键名归一化后使用。下划线、连字符、空格和大小写均不影响匹配。
_SENSITIVE_NORMALIZED_KEYS = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "apikey",
        "xapikey",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "credential",
        "credentials",
        "cookie",
        "setcookie",
        "authentication",
        "bearer",
        "secretkey",
        "privatekey",
        "secret",
        "token",
        "password",
    }
)

_ERROR_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(authorization|proxy[-_ ]?authorization|api[-_ ]?key|x[-_ ]?api[-_ ]?key|"
    r"access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|credential(?:s)?|"
    r"set[-_ ]?cookie|cookie|authentication|bearer|secret[-_ ]?key|private[-_ ]?key|"
    r"secret|token|password)\b"
    r"\s*([:=])\s*(?:\"[^\"]*\"|'[^']*'|[^,\n\r}\]]*)"
)

# Seedream 图生图的 Data URL 仅允许在 Worker 内存中临时存在。即便异常文本或历史
# JSON 使用了非敏感字段名，也不能让 Base64 因为字段名白名单而绕过脱敏边界。
_IMAGE_DATA_URL = re.compile(
    r"(?i)data:image/(?:jpeg|png|webp);base64,[a-z0-9+/=\r\n\t ]+"
)


def is_sensitive_key(key: object) -> bool:
    """返回键名是否属于认证或密钥字段，不检查其值。"""

    normalized = re.sub(r"[\W_]+", "", str(key), flags=re.UNICODE).casefold()
    return normalized in _SENSITIVE_NORMALIZED_KEYS


def redact_sensitive_data(value: Any) -> Any:
    """递归复制并清理 Mapping、列表和元组中的敏感键值。"""

    if isinstance(value, Mapping):
        return {
            str(key): REDACTED_VALUE if is_sensitive_key(key) else redact_sensitive_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item) for item in value)
    if isinstance(value, str):
        return _IMAGE_DATA_URL.sub("data:image/[REDACTED]", value)
    return value


def sanitize_error_summary(error: object, *, max_length: int = 500) -> str:
    """清理可能形如 ``api_key=value`` 的异常文本并限制持久化长度。"""

    text = str(error)
    sanitized = _ERROR_SENSITIVE_VALUE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED_VALUE}", text
    )
    sanitized = _IMAGE_DATA_URL.sub("data:image/[REDACTED]", sanitized)
    return sanitized[:max_length]

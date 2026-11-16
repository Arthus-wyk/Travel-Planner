"""小红书搜索服务 - 基于 Spider_XHS 原生签名引擎

彻底替换第三方 xhs 库，使用本地 JS 签名 + 直连 edith.xiaohongshu.com API，
解决 300011 账号异常风控误杀问题。
"""

import json

import logging
import threading

from collections import OrderedDict, deque

logger = logging.getLogger(__name__)
_amap_geocode_warning_lock = threading.Lock()
_amap_geocode_warning_keys: set[tuple[str, str]] = set()
_amap_geocode_rate_lock = threading.Lock()
_amap_geocode_request_times = deque()
_AMAP_GEOCODE_RATE_LIMIT = 3
_AMAP_GEOCODE_RATE_WINDOW = 1.0


class XHSCookieExpiredError(Exception):
    """小红书 Cookie 过期致命异常，用于向前端报警"""
    pass


class XHSFetchError(Exception):
    """小红书普通抓取异常；区别于 Cookie 过期/风控。"""
    pass


# ============ Cookie 处理 ============

def normalize_xhs_cookie(cookie: str) -> str:
    """兼容 Cookie 请求头字符串和浏览器导出的 JSON Cookie 列表。"""
    normalized = cookie.strip()
    if not normalized:
        return normalized

    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        normalized = normalized[1:-1].strip()

    cookie_items = None
    if normalized.startswith("[") and normalized.endswith("]"):
        try:
            cookie_items = json.loads(normalized)
        except json.JSONDecodeError:
            cookie_items = None
    elif normalized.startswith("{") and '"name"' in normalized and '"value"' in normalized:
        try:
            cookie_items = json.loads(f"[{normalized}]")
        except json.JSONDecodeError:
            cookie_items = None

    if isinstance(cookie_items, list):
        pairs = []
        for item in cookie_items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            value = str(item.get("value", "")).strip()
            if name:
                pairs.append(f"{name}={value}")
        if pairs:
            print("已将 JSON 格式的小红书 Cookie 转换为请求头字符串格式。")
            return "; ".join(pairs)

    return normalized


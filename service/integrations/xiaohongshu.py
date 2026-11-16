"""Create short-lived clients for Xiaohongshu web requests."""

import requests
import json

from service.xiaohongshu_models import TextResult, TextSearchResponse
from service.config import get_env_or_dotenv
from service.xhs_sign.sign_util import generate_x_b3_traceid, generate_request_params
from service.xhs_sign.xhs_service import XHSCookieExpiredError, normalize_xhs_cookie, XHSFetchError

from functools import wraps
from typing import Callable, TypeVar



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


class XhsNativeClient:
    """
    使用 Spider_XHS 签名引擎直连小红书 API 的原生客户端。
    不依赖任何第三方 xhs Python 库，通过 PyExecJS 调用本地 JS
    生成 x-s / x-t / x-s-common 等完整签名，彻底绕过 300011 风控。
    """
    BASE_URL = "https://edith.xiaohongshu.com"

    def __init__(self, cookies_str: str):
        self.cookies_str = cookies_str
        self._session = requests.Session()
        self._session.trust_env = False

    def search_notes(self, keyword: str, page: int = 1, sort_type: int = 0,
                     page_size: int = 5) -> dict:
        """
        搜索笔记 - 直连 /api/sns/web/v1/search/notes

        Args:
            keyword: 搜索关键词
            page: 页码
            sort_type: 排序方式 0综合 1最新 2最多点赞
            page_size: 每页数量

        Returns:
            API 响应 JSON
        """
        sort_map = {
            0: "general",
            1: "time_descending",
            2: "popularity_descending",
            3: "comment_descending",
            4: "collect_descending",
        }
        sort = sort_map.get(sort_type, "general")

        api = "/api/sns/web/v1/search/notes"
        data = {
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
            "search_id": generate_x_b3_traceid(21),
            "sort": "general",
            "note_type": 0,
            "ext_flags": [],
            "filters": [
                {"tags": [sort], "type": "sort_type"},
                {"tags": ["不限"], "type": "filter_note_type"},
                {"tags": ["不限"], "type": "filter_note_time"},
                {"tags": ["不限"], "type": "filter_note_range"},
                {"tags": ["不限"], "type": "filter_pos_distance"},
            ],
            "geo": "",
            "image_formats": ["jpg", "webp", "avif"],
        }

        headers, cookies, serialized_data = generate_request_params(
            self.cookies_str, api, data, "POST"
        )
        response = self._session.post(
            self.BASE_URL + api,
            headers=headers,
            data=serialized_data.encode("utf-8"),
            cookies=cookies,
            timeout=15,
        )
        res_json = response.json()

        if not res_json.get("success"):
            code = res_json.get("code", "")
            msg = res_json.get("msg", "")
            if code == 300011 or "异常" in msg:
                raise XHSCookieExpiredError(
                    f"小红书 Cookie 已被风控拦截 (code={code}): {msg}。请更换 Cookie 后重试。"
                )
            raise Exception(f"小红书搜索失败 (code={code}): {msg}")

        return res_json

    def get_note_detail(self, note_id: str, xsec_token: str = "",
                        xsec_source: str = "pc_search") -> dict:
        """
        获取笔记详情 - 直连 /api/sns/web/v1/feed

        Args:
            note_id: 笔记 ID
            xsec_token: 安全令牌（来自搜索结果）
            xsec_source: 来源标识

        Returns:
            笔记详情 JSON
        """
        api = "/api/sns/web/v1/feed"
        data = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": "1"},
            "xsec_source": xsec_source,
            "xsec_token": xsec_token,
        }

        headers, cookies, serialized_data = generate_request_params(
            self.cookies_str, api, data, "POST"
        )
        response = self._session.post(
            self.BASE_URL + api,
            headers=headers,
            data=serialized_data,
            cookies=cookies,
            timeout=15,
        )
        res_json = response.json()
        print("笔记详情：",res_json)
        if not res_json.get("success"):
            code = res_json.get("code", "")
            msg = res_json.get("msg", "")
            if code == 300011 or "异常" in msg:
                raise XHSCookieExpiredError(
                    f"小红书 Cookie 已被风控拦截 (code={code}): {msg}"
                )

        return res_json


# ============ 客户端工厂 ============

def get_xhs_client() -> XhsNativeClient:
    """初始化并返回原生小红书客户端"""
    xhs_cookie=get_env_or_dotenv("XHS_COOKIE").strip()
    if not xhs_cookie:
        raise XHSCookieExpiredError("小红书 Cookie 未配置，请先在前端设置页完成配置")
    cookie_str = normalize_xhs_cookie(xhs_cookie)
    return XhsNativeClient(cookie_str)
# ============ SSR 降级方案（备用） ============

def get_note_detail_ssr(note_id: str) -> dict:
    """通过网页抓取 SSR 状态提取笔记详情，作为原生 API 的降级备选"""
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=8, trust_env=False)
        match = re.search(r'window\.__INITIAL_STATE__=({.*?})</script>', resp.text)
        if match:
            state_json = json.loads(match.group(1).replace('undefined', 'null'))
            return state_json.get("note", {}).get("noteDetailMap", {}).get(note_id, {}).get("note", {})
    except Exception as e:
        print(f"SSR详情提取失败 {note_id}: {e}")
    return {}

T = TypeVar("T")

# ---------- 统一异常处理 ----------

def _handle_xhs_errors(func: Callable[..., T]) -> Callable[..., T]:
    @wraps(func)
    def wrapper(*args, **kwargs) -> T:
        try:
            return func(*args, **kwargs)

        except XHSCookieExpiredError:
            raise

        except (requests.Timeout, requests.ConnectionError) as e:
            print(f"❌ 小红书网络请求失败: {e}")
            raise XHSFetchError(
                f"访问小红书超时或网络不可达，请检查网络/代理后重试: {e}"
            ) from e

        except XHSFetchError:
            raise

        except Exception as e:
            print(f"❌ 小红书接口抓取失败: {e}")
            raise XHSFetchError(
                f"小红书数据抓取失败: {e}"
            ) from e

    return wrapper


# ---------- 公共数据获取 ----------

@_handle_xhs_errors
def _fetch_xhs_notes(keywords: str) -> list[dict]:
    """
    搜索小红书，获取前 4 条笔记的完整详情。

    返回统一结构：
    [
        {
            "note_id": str,
            "title": str,
            "desc": str,
            "image_list": list
        }
    ]
    """
    print(f"🔍 [XHS_SERVICE] 正在搜索小红书: {keywords}")

    client = get_xhs_client()
    res_json = client.search_notes(keyword=keywords)
    items = res_json.get("data", {}).get("items", [])[:4]

    notes = []

    for note in items:
        if note.get("model_type") != "note":
            continue

        note_id = note.get("id", "")
        if not note_id:
            continue

        note_card = note.get("note_card") or {}
        title = (
            note_card.get("display_title")
            or note_card.get("title")
            or ""
        )

        desc = note_card.get("desc", "")
        image_list = note_card.get("image_list") or []

        xsec_token = note.get("xsec_token", "")

        # 优先获取原生 API 详情
        try:
            detail_res = client.get_note_detail(
                note_id, xsec_token
            )
            detail_items = (
                detail_res.get("data", {}).get("items", [])
            )

            if detail_items:
                detail_card = (
                    detail_items[0].get("note_card") or {}
                )

                title = (
                    detail_card.get("title")
                    or detail_card.get("display_title")
                    or title
                )
                desc = detail_card.get("desc", desc)
                image_list = (
                    detail_card.get("image_list")
                    or image_list
                )

        except XHSCookieExpiredError:
            raise

        except Exception:
            # 降级到 SSR
            try:
                detail = get_note_detail_ssr(note_id)

                title = (
                    detail.get("title")
                    or detail.get("display_title")
                    or title
                )
                desc = detail.get("desc", desc)
                image_list = (
                    detail.get("image_list")
                    or image_list
                )

            except Exception:
                # 保留搜索结果已有的数据
                pass

        notes.append({
            "note_id": note_id,
            "title": title,
            "desc": desc,
            "image_list": image_list,
        })

    return notes


# ---------- 文字搜索 ----------

@_handle_xhs_errors
def search_xhs_attractions_text(
    keywords: str,
    language: str = "zh",
) -> TextSearchResponse:
    """
    搜索小红书笔记，返回标题和正文文字。
    """
    notes = _fetch_xhs_notes(keywords)
    results: list[TextResult] = []

    for note in notes:
        title = note["title"]
        desc = note["desc"]

        if not (title or desc):
            continue

        results.append(
            TextResult(
                title=title,
                text=desc,
                note_url=(
                    "https://www.xiaohongshu.com/explore/"
                    f"{note['note_id']}"
                ),
            )
        )

    return TextSearchResponse(results=results)


# ---------- 图片搜索 ----------

@_handle_xhs_errors
def search_xhs_attractions_images(
    keywords: str,
    language: str = "zh",
) -> ImageSearchResponse:
    """
    搜索小红书笔记，返回真实图片链接。
    优先使用 url_default，降级到 url_pre。
    """
    notes = _fetch_xhs_notes(keywords)
    results: list[ImageResult] = []

    for note in notes:
        image_urls = []

        for image in note["image_list"]:
            url = (
                image.get("url_default")
                or image.get("url_pre")
                or image.get("url")
            )

            if url and url.startswith(("http://", "https://")):
                image_urls.append(url)

        if not image_urls:
            continue

        results.append(
            ImageResult(
                title=note["title"],
                note_url=(
                    "https://www.xiaohongshu.com/explore/"
                    f"{note['note_id']}"
                ),
                image_urls=list(dict.fromkeys(image_urls)),
            )
        )

    return ImageSearchResponse(results=results)
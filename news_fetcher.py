"""
新闻抓取模块 —— 多源容错 · 全球视野 · 可配置

数据源 (可在「参数设置」里开关, 新增源只需改 SOURCES 列表):

  国内 (akshare):
    - 东方财富个股新闻
    - 同花顺全球快讯
    - 财联社电报
    - 新浪财经全球快讯

  全球 (RSS, 免费无需 API Key):
    - BBC World        https://feeds.bbci.co.uk/news/world/rss.xml
    - CNN Top Stories  http://rss.cnn.com/rss/edition.rss
    - Reuters          https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best
    - AP News          https://rsshub.app/apnews/topics/apf-topnews
    - The Guardian     https://www.theguardian.com/world/rss
    - Yahoo Finance    https://finance.yahoo.com/news/rssindex
    - MarketWatch      https://feeds.content.dowjones.io/public/rss/mw_topstories
    - CNBC             https://www.cnbc.com/id/100003114/device/rss/rss.html

代理: 从 app_settings 表读取 http_proxy (例 http://127.0.0.1:7890),
      配置后自动用于所有 RSS / akshare 请求。
"""
import os
import sys
import time
import json
import logging
import hashlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTO
from datetime import datetime
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data_store

logger = logging.getLogger("news_fetcher")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


# ================ 可扩展源清单 ================

DOMESTIC_SOURCES = [
    {"key": "eastmoney",  "name": "东方财富",    "type": "ak_em",    "enabled": True},
    {"key": "baidu",      "name": "同花顺",      "type": "ak_baidu", "enabled": True},
    {"key": "cls",        "name": "财联社",      "type": "ak_cls",   "enabled": True},
    {"key": "sina",       "name": "新浪财经",    "type": "ak_sina",  "enabled": True},
]


def _ak_pick(*names):
    """按优先级返回第一个存在的 akshare 函数 (接口改名时自动兼容)"""
    import akshare as ak
    for n in names:
        f = getattr(ak, n, None)
        if callable(f):
            return f
    return None


def _ak_df(f, **kw):
    if f is None:
        return None
    try:
        return f(**kw)
    except Exception:
        return None

GLOBAL_RSS_SOURCES = [
    {"key": "bbc",        "name": "BBC World",      "url": "https://feeds.bbci.co.uk/news/world/rss.xml",                        "lang": "en", "enabled": True},
    {"key": "cnn",        "name": "CNN Top",        "url": "http://rss.cnn.com/rss/edition.rss",                                 "lang": "en", "enabled": False},
    {"key": "aljazeera",  "name": "Al Jazeera",     "url": "https://www.aljazeera.com/xml/rss/all.xml",                          "lang": "en", "enabled": True},
    {"key": "cnbc",       "name": "CNBC",           "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",             "lang": "en", "enabled": True},
    {"key": "guardian",   "name": "The Guardian",   "url": "https://www.theguardian.com/world/rss",                              "lang": "en", "enabled": True},
    {"key": "marketwatch","name": "MarketWatch",    "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",         "lang": "en", "enabled": True},
    {"key": "investing",  "name": "Investing.com",  "url": "https://www.investing.com/rss/news.rss",                             "lang": "en", "enabled": True},
    {"key": "reuters",    "name": "Reuters",        "url": "https://news.google.com/rss/search?q=reuters&hl=en-US&gl=US&ceid=US:en", "lang": "en", "enabled": True},
    {"key": "bloomberg",  "name": "Bloomberg",      "url": "https://feeds.bloomberg.com/markets/news.rss",                       "lang": "en", "enabled": True},
]

ALL_SOURCES = DOMESTIC_SOURCES + GLOBAL_RSS_SOURCES


def _load_enabled_sources() -> dict:
    """从 app_settings 读每个源的 enabled 状态, 未设置的保持默认"""
    raw = data_store.get_setting("news_sources", "{}")
    try:
        cfg = json.loads(raw or "{}")
    except Exception:
        cfg = {}
    result = {}
    for s in ALL_SOURCES:
        result[s["key"]] = cfg.get(s["key"], s.get("enabled", True))
    return result


_SUPPORTED_PROXY = ("http", "https", "socks5", "socks5h", "socks4", "socks4a")

# 本轮抓取是否已确定禁用代理 (探测失败后置位, 避免逐个源反复超时)
_PROXY_DISABLED = False
_PROXY_CACHE = {"ts": 0.0, "val": None}


def normalize_proxy(proxy: str) -> str:
    """清洗代理串: 去空白 / 去掉 #片段 (常见于订阅里的 '代理#节点名') / 补协议"""
    p = (proxy or "").strip()
    if not p:
        return ""
    p = p.split("#")[0].strip()
    if "://" not in p:
        p = "http://" + p
    return p


def _get_proxy() -> dict | None:
    """读代理配置, 返回给 requests 用的 proxies dict"""
    if _PROXY_DISABLED:
        return None
    proxy = normalize_proxy(data_store.get_setting("http_proxy", "") or "")
    if not proxy:
        return None
    scheme = proxy.split("://", 1)[0].lower()
    if scheme.startswith("socks"):
        try:
            import socks  # noqa: F401
        except Exception:
            logger.warning("未安装 PySocks, socks 代理不可用, 本次改直连")
            return None
    return {"http": proxy, "https": proxy}


# 境内域名后缀: 命中则永不经过代理 (A股行情/AI资讯等境内源直连更快)
_DOMESTIC_SUFFIX = (
    ".cn", "eastmoney.com", "eastmoney.cn", "sina.com.cn", "sina.cn",
    "10jqka.com.cn", "cls.cn", "gtimg.cn", "baidu.com", "qq.com",
    "163.com", "ifeng.com", "hexun.com", "cnstock.com", "yicai.com",
    "stcn.com", "cs.com.cn", "jrj.com.cn", "xueqiu.com", "szse.cn",
    "sse.com.cn", "chinaclear.cn", "cninfo.com.cn", "127.0.0.1",
    "localhost",
)

_PROXY_MODE_DESC = {
    "auto": "自动: 境内直连, 仅境外站点走代理",
    "always": "全部走代理",
    "off": "全部直连 (不使用代理)",
}


def is_domestic(url: str) -> bool:
    """判断 URL 是否属于境内站点"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return any(host == s.lstrip(".") or host.endswith(s) for s in _DOMESTIC_SUFFIX)


def get_proxy_mode() -> str:
    """代理模式: auto(默认) / always / off"""
    m = (data_store.get_setting("proxy_mode", "auto") or "auto").strip().lower()
    return m if m in _PROXY_MODE_DESC else "auto"


def proxy_for(url: str) -> dict | None:
    """按 URL 决定本次请求是否使用代理 (自动模式: 境内直连)"""
    mode = get_proxy_mode()
    if mode == "off":
        return None
    if mode == "auto" and is_domestic(url):
        return None
    return _get_proxy()


# 最近一次代理故障 (供 UI 弹提示)
_PROXY_ERR = {"msg": "", "ts": 0.0}


def proxy_alive(proxy: str | None = None, timeout: float = 3.0) -> tuple[bool, str]:
    """探测代理是否可用 -> (是否可用, 说明文字)"""
    import socket
    import time as _t
    from urllib.parse import urlparse

    raw = (data_store.get_setting("http_proxy", "") or "") if proxy is None else proxy
    hint = "（已忽略 # 片段）" if "#" in (raw or "") else ""
    p = normalize_proxy(raw)
    if not p:
        return True, "未配置代理（直连）"

    scheme = p.split("://", 1)[0].lower()
    if scheme not in _SUPPORTED_PROXY:
        return False, f"代理协议不支持: {scheme}{hint}"
    if scheme.startswith("socks"):
        try:
            import socks  # noqa: F401
        except Exception:
            return False, f"缺少 PySocks，socks 代理不可用{hint}"

    host, port = "", 0
    try:
        u = urlparse(p)
        host = u.hostname or ""
        port = u.port or (1080 if scheme.startswith("socks") else 8080)
        if not host:
            raise ValueError("缺少主机名")
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return True, f"代理可用: {host}:{port}{hint}"
    except Exception as e:
        msg = f"代理不可用 {host or p}:{port} → {type(e).__name__}{hint}"
        _PROXY_ERR["msg"] = msg
        _PROXY_ERR["ts"] = _t.time()
        return False, msg


def proxy_works(proxy: str | None = None, timeout: float = 6.0,
                use_cache: bool = True) -> tuple[bool, str]:
    """端到端探测: 通过代理访问外网轻量地址 (比只探端口可靠)"""
    import time as _t

    auto = proxy is None
    raw = (data_store.get_setting("http_proxy", "") or "") if auto else proxy
    p = normalize_proxy(raw)
    if not p:
        return True, "未配置代理（直连）"

    if auto and use_cache and _PROXY_CACHE["val"] is not None \
            and _t.time() - _PROXY_CACHE["ts"] < 120:
        return _PROXY_CACHE["val"]

    scheme = p.split("://", 1)[0].lower()
    if scheme not in _SUPPORTED_PROXY:
        res = (False, f"代理协议不支持: {scheme}")
    elif scheme.startswith("socks"):
        try:
            import socks  # noqa: F401
        except Exception:
            res = (False, "缺少 PySocks，socks 代理不可用")
        else:
            res = _canary_http(p, timeout)
    else:
        res = _canary_http(p, timeout)

    if not res[0]:
        _PROXY_ERR["msg"] = res[1]
        _PROXY_ERR["ts"] = _t.time()
    if auto:
        _PROXY_CACHE["ts"] = _t.time()
        _PROXY_CACHE["val"] = res
    return res


def _canary_http(p: str, timeout: float) -> tuple[bool, str]:
    host = p.split("@")[-1]
    try:
        r = requests.get("https://www.gstatic.com/generate_204",
                         proxies={"http": p, "https": p}, timeout=timeout)
        if r.status_code in (200, 204, 301, 302):
            return True, f"代理可用（{host}）"
        return False, f"代理连通但返回 HTTP {r.status_code}"
    except Exception as e:
        return False, f"代理无法访问外网（{type(e).__name__}，{host}）"


def last_proxy_error() -> str:
    """最近 10 分钟内发生过的代理故障 (无则空串)"""
    import time as _t
    if _PROXY_ERR["msg"] and _t.time() - _PROXY_ERR["ts"] < 600:
        return _PROXY_ERR["msg"]
    return ""



# ================ 通用工具 ================

def _safe(fn, timeout=15):
    """带超时地执行单个抓取函数"""
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except FTO:
            return None
        except Exception as e:
            logger.debug("source failed: %s", e)
            return None


def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


def _strip_html(s: str) -> str:
    """去掉 HTML 标签, 折叠空白"""
    import re
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ================ 翻译 (英→中, 免费 MyMemory API, 无 Key) ================

_translate_cache: dict[str, str] = {}   # text -> 中文译文, 内存缓存避免重复请求
_TRANSLATE_MAX = 5000                   # 缓存上限, 防内存膨胀


def _is_english(text: str) -> bool:
    """判断是否主要为英文 (含较多拉丁字母且少中文字符)"""
    if not text:
        return False
    letters = sum(1 for c in text if c.isascii() and c.isalpha())
    chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    return letters > chinese and letters >= 3


def translate_to_zh(text: str) -> str:
    """英文 → 中文翻译。中文/非英文直接返回。用 MyMemory 免费 API, 带代理与缓存。"""
    if not text or not text.strip():
        return text
    if not _is_english(text):
        return text

    key = text.strip()[:300]
    if key in _translate_cache:
        return _translate_cache[key]

    import requests
    url = "https://api.mymemory.translated.net/get"

    def _try(proxies):
        resp = requests.get(
            url, params={"q": text[:500], "langpair": "en|zh-CN"},
            timeout=8, proxies=proxies,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        translated = resp.json().get("responseData", {}).get("translatedText", "").strip()
        if translated and translated.lower() != "my memory free translation":
            return translated
        return None

    result = text
    # 1) 用配置的代理 (若有)
    proxies = _get_proxy()
    if proxies:
        try:
            t = _try(proxies)
            if t:
                result = t
        except Exception as e:
            logger.debug("translate via proxy failed, trying direct: %s", e)
    # 2) 代理失败或无代理 → 直连 (绕过环境变量代理)
    if result == text:
        try:
            t = _try({})  # 空 dict = 显式不走任何代理
            if t:
                result = t
        except Exception as e:
            logger.debug("translate direct failed: %s", e)

    if len(_translate_cache) > _TRANSLATE_MAX:
        _translate_cache.pop(next(iter(_translate_cache)))
    _translate_cache[key] = result
    return result


# ================ RSS 解析 ================

def _parse_rss_date(s: str) -> str:
    """RSS 常见日期格式 → 'YYYY-MM-DD HH:MM:SS'"""
    if not s:
        return _today_str() + " 00:00:00"
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(s.strip(), fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
    # 最后兜底: 截前 19 位
    return s[:19].replace("T", " ") if len(s) >= 19 else (s or _today_str())


def _fetch_rss(source: dict, timeout: int = 12) -> list[dict]:
    """抓取并解析一个 RSS 源"""
    import requests
    url = source["url"]
    name = source["name"]
    lang = source.get("lang", "en")
    proxies = proxy_for(url)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TommyQuant/1.0)"}
    items = []
    try:
        try:
            resp = requests.get(url, timeout=timeout, proxies=proxies, headers=headers)
            resp.raise_for_status()
        except requests.RequestException as e:
            if not proxies:
                raise
            _PROXY_ERR["msg"] = f"代理不可用，已自动改直连（{type(e).__name__}）"
            import time as _t
            _PROXY_ERR["ts"] = _t.time()
            logger.warning("RSS %s 代理失败(%s), 改直连重试", name, type(e).__name__)
            resp = requests.get(url, timeout=timeout, proxies={}, headers=headers)
            resp.raise_for_status()
        # 有些源返回 HTML 里嵌的 RSS, 让 requests 用正确编码
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        root = ET.fromstring(resp.content)

        # 处理 RSS / Atom 两种格式
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        # RSS 2.0
        for item in root.iter("item"):
            title = _strip_html((item.findtext("title") or "").strip())[:200]
            if not title:
                continue
            desc = _strip_html(item.findtext("description") or item.findtext("summary") or "")[:600]
            pub = item.findtext("pubDate") or item.findtext("published") or item.findtext("updated") or ""
            link = (item.findtext("link") or "").strip()
            st = _parse_rss_date(pub)
            items.append({
                "date": st[:10],
                "source": name,
                "symbol": "",
                "title": title,
                "content": desc,
                "sentiment": None,
                "source_time": st,
                "lang": lang,
                "link": link,
            })
        # Atom fallback (<entry>)
        if not items:
            for entry in root.iter("entry"):
                title = _strip_html((entry.findtext("title") or "").strip())[:200]
                if not title:
                    continue
                desc = _strip_html(entry.findtext("summary") or entry.findtext("content") or "")[:600]
                pub = entry.findtext("published") or entry.findtext("updated") or ""
                # Atom: <link href="..."/> (可能无文本)
                link = ""
                le = entry.find("link")
                if le is not None:
                    link = (le.get("href") or le.text or "").strip()
                st = _parse_rss_date(pub)
                items.append({
                    "date": st[:10],
                    "source": name,
                    "symbol": "",
                    "title": title,
                    "content": desc,
                    "sentiment": None,
                    "source_time": st,
                    "lang": lang,
                    "link": link,
                })
    except Exception as e:
        logger.info("RSS %s failed: %s", name, e)
    logger.info("RSS %s: %d items", name, len(items))
    return items


# ================ akshare 国内源 ================

def _fetch_eastmoney(symbol: str = "") -> list[dict]:
    import akshare as ak
    items = []
    try:
        if symbol:
            df = _ak_df(_ak_pick("stock_news_em"), symbol=symbol)
            for _, r in (df.iterrows() if df is not None and len(df) else []):
                st = str(r.get("发布时间", r.get("时间", _today_str())))[:19]
                items.append({
                    "date": st[:10], "source": "东方财富", "symbol": symbol,
                    "title": str(r.get("新闻标题", ""))[:200],
                    "content": str(r.get("新闻内容", ""))[:600],
                    "sentiment": None, "source_time": st, "lang": "zh",
                    "link": str(r.get("链接", r.get("url", "")) or "")[:500],
                })
        else:
            df = _ak_df(_ak_pick("stock_news_global_em", "stock_info_global_em"))
            for _, r in (df.head(50).iterrows() if df is not None and len(df) else []):
                st = str(r.get("发布时间", _today_str()))[:19]
                items.append({
                    "date": st[:10], "source": "东方财富", "symbol": "",
                    "title": str(r.get("标题", ""))[:200],
                    "content": str(r.get("摘要", r.get("内容", "")))[:600],
                    "sentiment": None, "source_time": st, "lang": "zh",
                    "link": str(r.get("链接", "") or "")[:500],
                })
    except Exception:
        pass
    return items


def _fetch_ths() -> list[dict]:
    """同花顺全球快讯 (stock_news_global_ths: 标题/内容/发布时间/链接)"""
    items = []
    try:
        df = _ak_df(_ak_pick("stock_news_global_ths", "stock_news_global_em"))
        if df is not None and len(df):
            for _, r in df.head(50).iterrows():
                st = str(r.get("发布时间", _today_str()))[:19]
                items.append({
                    "date": st[:10], "source": "同花顺", "symbol": "",
                    "title": str(r.get("标题", ""))[:200],
                    "content": str(r.get("内容", r.get("摘要", "")))[:600],
                    "sentiment": None, "source_time": st, "lang": "zh",
                    "link": str(r.get("链接", "") or "")[:500],
                })
    except Exception:
        pass
    return items


def _fetch_cls() -> list[dict]:
    """财联社电报 (stock_news_global_cls: 标题/内容/发布日期/发布时间)"""
    items = []
    try:
        df = _ak_df(_ak_pick("stock_news_global_cls", "stock_info_global_cls"))
        if df is not None and len(df):
            for _, r in df.head(30).iterrows():
                dv = str(r.get("发布日期", ""))[:10]
                tv = str(r.get("发布时间", ""))[:8]
                st = f"{dv} {tv}".strip() if tv else (dv or _today_str())
                title = str(r.get("标题", ""))
                items.append({
                    "date": st[:10], "source": "财联社", "symbol": "",
                    "title": title[:200],
                    "content": str(r.get("内容", ""))[:600],
                    "sentiment": None, "source_time": st, "lang": "zh",
                    "link": _search_link(title),
                })
    except Exception:
        pass
    return items


def _fetch_sina() -> list[dict]:
    """新浪财经全球快讯 (stock_news_global_sina: 时间/内容, 无标题)"""
    items = []
    try:
        df = _ak_df(_ak_pick("stock_news_global_sina", "stock_info_global_sina",
                             "stock_news_global_em"))
        if df is not None and len(df):
            for _, r in df.head(30).iterrows():
                st = str(r.get("时间", r.get("发布时间", _today_str())))[:19]
                content = str(r.get("内容", ""))
                title = str(r.get("标题", "") or content[:80])
                items.append({
                    "date": st[:10], "source": "新浪财经", "symbol": "",
                    "title": title[:200], "content": content[:600],
                    "sentiment": None, "source_time": st, "lang": "zh",
                    "link": str(r.get("链接", "") or "")[:500] or _search_link(title),
                })
    except Exception:
        pass
    return items


def _search_link(title: str) -> str:
    """无原生链接时, 用新闻搜索引擎兜底 (点击可跳转到相关结果页)"""
    import urllib.parse
    t = (title or "").strip()
    if not t:
        return ""
    return "https://www.baidu.com/s?wd=" + urllib.parse.quote(t[:60])


_AK_HANDLERS = {
    "ak_em":    _fetch_eastmoney,
    "ak_baidu": _fetch_ths,     # key 沿用 baidu (历史配置兼容), 实际数据源已换同花顺
    "ak_cls":   _fetch_cls,
    "ak_sina":  _fetch_sina,
}


# ================ 主入口 ================

def fetch_all_news(symbols: list[str] | None = None,
                   per_source_timeout: int = 15) -> dict:
    """
    抓取所有启用的新闻源并入库 (多线程并行, 单源超时不影响整体)。

    symbols: 个股新闻要抓的股票列表 (可选, 仅东方财富)
    Returns: {key: n, "total": n}
    """
    enabled = _load_enabled_sources()
    counts = {}

    # 代理探测一次: 不通就本轮全部直连, 避免每个源都卡超时
    global _PROXY_DISABLED
    _PROXY_DISABLED = False
    if (data_store.get_setting("http_proxy", "") or "").strip():
        _ok, _msg = proxy_works()
        _PROXY_DISABLED = not _ok
        if not _ok:
            logger.warning("代理不可用，本轮全部改直连: %s", _msg)

    futures = []   # (key, Future)

    with ThreadPoolExecutor(max_workers=8) as pool:
        # --- 国内源 ---
        for src in DOMESTIC_SOURCES:
            key = src["key"]
            if not enabled.get(key, True):
                counts[key] = 0
                continue
            ftype = src["type"]
            handler = _AK_HANDLERS.get(ftype)
            if handler is None:
                continue
            if ftype == "ak_em" and symbols:
                # 东方财富按个股抓 (限制数量)
                def _run_em():
                    all_items = []
                    for sym in symbols[:20]:
                        code = sym[2:]   # sh600519 -> 600519
                        all_items.extend(_fetch_eastmoney(code))
                    return all_items
                futures.append((key, pool.submit(_run_em)))
            else:
                futures.append((key, pool.submit(handler)))

        # --- 全球 RSS 源 ---
        for src in GLOBAL_RSS_SOURCES:
            key = src["key"]
            if not enabled.get(key, True):
                counts[key] = 0
                continue
            futures.append((key, pool.submit(_fetch_rss, src, per_source_timeout)))

        # 收集结果 (带超时保护)
        for key, fut in futures:
            try:
                items = fut.result(timeout=per_source_timeout + 3) or []
            except FTO:
                items = []
            except Exception as e:
                logger.info("source %s error: %s", key, e)
                items = []
            counts[key] = len(items)
            if items:
                try:
                    data_store.save_news(items)
                except Exception as e:
                    logger.error("save_news %s failed: %s", key, e)

    counts["total"] = sum(counts.values())
    logger.info("news fetched: %s", counts)
    return counts


def get_recent_news(limit: int = 30) -> list[dict]:
    """读取最近新闻 (按 id 倒序, 已按 title_hash 去重)"""
    conn = data_store.get_conn()
    cur = conn.execute(
        "SELECT id, date, source, symbol, title, content, source_time, link FROM news "
        "ORDER BY id DESC LIMIT ?", (limit,))
    cols = ["id", "date", "source", "symbol", "title", "content", "source_time", "link"]
    out = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()
    return out

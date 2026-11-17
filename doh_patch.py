"""
DoH (DNS over HTTPS) 兜底解析器.

NAS 路由器劫持了 UDP 53 端口, 公共 DNS 返回空响应, 导致容器内所有域名解析失败.
此模块 monkey-patch socket.getaddrinfo, 当系统 DNS 失败时自动回退到阿里 DoH
(https://dns.alidns.com), 保证 akshare/requests 等网络调用能正常工作.

用法 (在应用入口最顶部):
    import doh_patch
    doh_patch.install()
"""
import socket
import json
import ssl
import urllib.request
import threading

# 阿里 DoH (用 IP 直连 + Host 头, 避免对 dns.alidns.com 本身的 DNS 依赖)
_DOH_IP = "223.5.5.5"
_DOH_HOST = "dns.alidns.com"
_DOH_URL = f"https://{_DOH_IP}/resolve?name={{name}}&type=A"

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

_orig_getaddrinfo = socket.getaddrinfo
_doh_cache: dict[str, list[str]] = {}
_doh_lock = threading.Lock()


def _doh_resolve(host: str) -> list[str]:
    """通过阿里 DoH 解析域名, 返回 IP 列表"""
    if host in _doh_cache:
        return _doh_cache[host]
    url = _DOH_URL.format(name=host)
    req = urllib.request.Request(url, headers={
        "Accept": "application/dns-json",
        "Host": _DOH_HOST,
    })
    try:
        resp = urllib.request.urlopen(req, timeout=8, context=_ssl_ctx)
        data = json.loads(resp.read())
        ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
    except Exception:
        ips = []
    with _doh_lock:
        _doh_cache[host] = ips
    return ips


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """系统 DNS 失败时回退到 DoH"""
    try:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror:
        ips = _doh_resolve(host)
        if not ips:
            raise
        # 用第一个 IP 构造 addrinfo (兼容 IPv4)
        return [(socket.AF_INET, type, proto, "", (ips[0], port))]


def install() -> None:
    """安装 DoH 兜底 (幂等)"""
    if socket.getaddrinfo is not _patched_getaddrinfo:
        socket.getaddrinfo = _patched_getaddrinfo


def uninstall() -> None:
    """恢复原始 DNS"""
    socket.getaddrinfo = _orig_getaddrinfo

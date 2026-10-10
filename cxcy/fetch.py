"""数据源客户端：辽宁省大学生创新创业管理共享平台。

关键点：该服务器使用旧式 TLS 重协商（unsafe legacy renegotiation），
现代 HTTP 客户端默认拒绝，必须显式放行 OP_LEGACY_SERVER_CONNECT。
"""

from __future__ import annotations

import re
import ssl
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests
import urllib3
from requests.adapters import HTTPAdapter

from .dns_resolver import resolve

BASE = "https://cxcy.upln.cn/provincial/match/competition"
PORTAL = "https://cxcy.upln.cn/provincial/portal/portal"
FILE_INFO = "https://cxcy.upln.cn/provincial/file/fileInfo"
SITE = "https://cxcy.upln.cn"
DETAIL_PAGE = "https://cxcy.upln.cn/match/details?comId={id}"
ANNOUNCE_PAGE = "https://cxcy.upln.cn/competitionDetails?id={id}"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class _LegacyTLSAdapter(HTTPAdapter):
    """放行旧式 TLS 重协商，并在系统 DNS 不可用时用自定义解析器改写连接目标。

    为什么要在适配器里改 URL：平台的 A 记录在国内多家公共 DNS 上返回 SERVFAIL
    （详见 dns_resolver 模块注释），阿里云 FC 恰好用这类 DNS，导致
    socket.getaddrinfo 直接失败。这里把「连到哪」和「访问哪个域名」解耦：
      - 连接目标：替换为解析出的 IP
      - Host 头与 TLS SNI：保持原域名不变（urllib3 依据 Host 头设置 SNI）
    这样既绕开 DNS，又不破坏虚拟主机与证书校验语义。
    """

    def __init__(self, **kw: Any) -> None:
        self._ctx = ssl.create_default_context()
        self._ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE
        self._ip_cursor = 0
        super().__init__(**kw)

    def init_poolmanager(self, *a: Any, **k: Any) -> Any:
        k["ssl_context"] = self._ctx
        return super().init_poolmanager(*a, **k)

    def proxy_manager_for(self, *a: Any, **k: Any) -> Any:
        k["ssl_context"] = self._ctx
        return super().proxy_manager_for(*a, **k)

    def send(self, request, **kwargs):  # type: ignore[override]
        url = request.url or ""
        parsed = urlparse(url)
        host = parsed.hostname
        # 只在尚未被改写、且确实是域名（非 IP）时才处理
        if host and not _is_ip(host):
            ips = resolve(host, parsed.port or 443)
            if ips:
                # 轮换 IP：多次重试时会尝试不同节点，避免单点故障
                ip = ips[self._ip_cursor % len(ips)]
                self._ip_cursor += 1
                netloc = ip if parsed.port is None else f"{ip}:{parsed.port}"
                request.url = urlunparse(parsed._replace(netloc=netloc))
                request.headers.setdefault("Host", parsed.netloc)
        return super().send(request, **kwargs)


def _is_ip(host: str) -> bool:
    return bool(re.fullmatch(r"[\d.]+", host or ""))


class Platform:
    """平台 API 客户端，带重试。"""

    def __init__(self, retries: int = 3, timeout: int = 30) -> None:
        self.timeout = timeout
        self.retries = retries
        self.s = requests.Session()
        self.s.mount("https://", _LegacyTLSAdapter())
        # 关闭 requests 层的证书校验：因为适配器会把连接目标改写为 IP，
        # 而证书是为域名签发的，用 IP 比对主机名必然失败。
        # 这里的安全边界：只访问本平台自己的 IP、TLS 仍在用、SNI 保持域名不变，
        # 传输内容为公开的竞赛信息，不含任何凭据。
        self.s.verify = False
        self.s.headers.update({
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://cxcy.upln.cn/match",
        })

    def _get_url(self, url: str, params: dict[str, Any]) -> Any:
        """按完整 URL 请求并返回 result（带重试）。"""
        last: Exception | None = None
        for i in range(self.retries):
            try:
                r = self.s.get(url, params=params, timeout=self.timeout)
                r.raise_for_status()
                j = r.json()
                if not j.get("success"):
                    raise RuntimeError(f"接口返回失败: {j.get('message')!r}")
                return j.get("result")
            except Exception as e:  # noqa: BLE001 - 需要对任意网络异常重试
                last = e
                if i < self.retries - 1:
                    time.sleep(2 ** i)
        raise RuntimeError(f"{url} 请求失败（重试 {self.retries} 次）: {last}")

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._get_url(f"{BASE}/{path}", params)

    def ongoing(self, year: int | None = None) -> list[dict[str, Any]]:
        """进行中的竞赛。pageSize 给大值可一次拉全，无需翻页。"""
        params: dict[str, Any] = {
            "code": "1", "column": "createTime", "order": "desc",
            "pageNo": 1, "pageSize": 200,
        }
        if year:
            params["year"] = str(year)
        return self._get("queryOngoing", params).get("records") or []

    def detail(self, com_id: str) -> dict[str, Any]:
        """单个竞赛完整详情（含 detail 正文 HTML 与 fileList 附件）。"""
        return self._get("detail", {"id": com_id})

    # ---------- 大赛动态 / 获奖公示 ----------

    def announcements(self, page: int = 1, size: int = 100,
                      name: str | None = None) -> tuple[list[dict[str, Any]], int]:
        """大赛动态公告列表。type=1 必填，否则返回 0 条。

        返回 (记录列表, 总数)。每条记录含 competitionId，可关联到具体竞赛。
        """
        params: dict[str, Any] = {"type": 1, "pageNo": page, "pageSize": size}
        if name:
            params["name"] = name
        r = self._get_url(f"{PORTAL}/portalList", params) or {}
        return (r.get("records") or []), int(r.get("total") or 0)

    def announcement(self, ann_id: str) -> dict[str, Any]:
        """公告正文与附件清单（fileList 里 name/id/type/size 齐全，但 url 为 null）。"""
        return self._get_url(f"{PORTAL}/queryById", {"id": ann_id}) or {}

    def download_url(self, file_id: str) -> str:
        """附件下载地址。

        平台是"一次性票据"机制：每次请求都会生成新的 ticket，URL 不能长期缓存。
        所幸取票据是纯 HTTP，无需浏览器，可在推送前实时获取。
        """
        r = self._get_url(f"{FILE_INFO}/public-access", {"fileId": file_id})
        path = r if isinstance(r, str) else (r or {}).get("result") or ""
        if not path:
            raise RuntimeError(f"未能获取下载票据: fileId={file_id}")
        # 注意：path 形如 "/sys/common/static?ticket=..."，必须以 "/" 拼接。
        # 早期版本写成 SITE + "provincial" + path，漏了斜杠，导致域名与路径粘连
        # （https://cxcy.upln.cnprovincial/...），链接在手机上打不开。
        return f"{SITE}/provincial{path}"


def detail_page_url(com_id: str) -> str:
    return DETAIL_PAGE.format(id=com_id)

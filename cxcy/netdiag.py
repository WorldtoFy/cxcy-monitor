"""临时诊断函数：测清 FC 环境到 cxcy.upln.cn 的可达性。

部署方式：单独建一个 FC 函数，入口填 netdiag.handler，
代码包里放本文件 + requests 依赖（可直接用我们已有的包再加本文件）。

返回结果包含全部结论，无需日志。
"""
from __future__ import annotations

import json
import socket
import ssl
import struct
import time
import urllib.request

IP = "61.161.225.202"          # cxcy.upln.cn 已知 IP（从用户本机解析得到）
HOST = "cxcy.upln.cn"


def _raw_dns_query(server: str, name: str, timeout: float = 5.0) -> str:
    """手工构造 DNS A 记录查询（UDP），不依赖系统解析器。"""
    tid = 0x1234
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    packet = header + q + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(packet, (server, 53))
        data, _ = s.recvfrom(512)
    except Exception as e:  # noqa: BLE001
        return f"失败({type(e).__name__}: {e})"
    finally:
        s.close()

    rcode = data[3] & 0x0F
    if rcode != 0:
        return f"DNS 错误码 {rcode}（0=成功,3=NXDOMAIN,2=SERVFAIL）"
    ancount = struct.unpack(">H", data[6:8])[0]
    if ancount == 0:
        return "无应答记录"
    # 跳过问题段
    i = 12
    while data[i] != 0:
        i += data[i] + 1
    i += 5
    ips = []
    for _ in range(ancount):
        if data[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while data[i] != 0:
                i += data[i] + 1
            i += 1
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        if rtype == 1 and rdlen == 4:
            ips.append(".".join(str(b) for b in data[i:i + 4]))
        i += rdlen
    return ",".join(ips) if ips else "解析成功但无 A 记录"


def _tcp(ip: str, port: int, timeout: float = 8.0) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.time()
    try:
        s.connect((ip, port))
        return f"可连（{time.time()-t0:.2f}s）"
    except Exception as e:  # noqa: BLE001
        return f"不可连（{type(e).__name__}）"
    finally:
        s.close()


def _https_via_ip(ip: str, host: str, path: str, timeout: float = 20.0):
    """用 IP 直连 + SNI/Host 头，完全绕过 DNS。"""
    ctx = ssl.create_default_context()
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    t0 = time.time()
    try:
        conn = socket.create_connection((ip, 443), timeout=timeout)
        ss = ctx.wrap_socket(conn, server_hostname=host)
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
               f"User-Agent: Mozilla/5.0\r\nAccept: application/json\r\n"
               f"Connection: close\r\n\r\n")
        ss.sendall(req.encode())
        buf = b""
        while len(buf) < 8192:
            chunk = ss.recv(4096)
            if not chunk:
                break
            buf += chunk
        ss.close()
        head, _, body = buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n")[0].decode(errors="replace")
        return f"{status} | {len(body)} 字节 | {time.time()-t0:.2f}s | 前120字符: {body[:120].decode(errors='replace')}"
    except Exception as e:  # noqa: BLE001
        return f"失败 {type(e).__name__}: {str(e)[:160]}"


def handler(event, context):  # noqa: ANN001
    out: dict = {"ok": True, "logs": []}

    def note(m: str) -> None:
        out["logs"].append(m)
        print(m)

    # 1. 系统解析器
    note("=== 1. 系统 DNS 解析 ===")
    for h in (HOST, "www.baidu.com", "ntfy.sh", "api.github.com"):
        try:
            ip = socket.gethostbyname(h)
            note(f"  {h:20} -> {ip}")
        except Exception as e:  # noqa: BLE001
            note(f"  {h:20} -> 失败 {type(e).__name__}: {e}")

    # 2. 手工向公共 DNS 查询（绕过系统解析器）
    note("=== 2. 直接问公共 DNS（UDP 53）===")
    for server in ("223.5.5.5", "119.29.29.29", "8.8.8.8", "114.114.114.114"):
        note(f"  {server:16} {HOST} -> {_raw_dns_query(server, HOST)}")

    # 3. 已知 IP 的 TCP 连通性
    note("=== 3. 已知 IP 的 TCP 连通性 ===")
    note(f"  {IP}:443 -> {_tcp(IP, 443)}")
    note(f"  {IP}:80  -> {_tcp(IP, 80)}")

    # 4. IP 直连发 HTTPS 请求（绕开 DNS 的最终验证）
    note("=== 4. IP 直连 HTTPS（绕过 DNS）===")
    path = ("/provincial/match/competition/queryOngoing"
            "?year=2026&code=1&column=createTime&order=desc&pageNo=1&pageSize=1")
    r = _https_via_ip(IP, HOST, path)
    note(f"  {r}")
    out["ip_direct_ok"] = r.startswith("HTTP/1.1 200")

    # 5. 换一个已解析的域名做对照，确认出网正常
    note("=== 5. 对照组：通过系统 DNS 访问 GitHub API ===")
    try:
        with urllib.request.urlopen("https://api.github.com/zen", timeout=15) as resp:
            note(f"  api.github.com -> HTTP {resp.status}: {resp.read()[:60].decode(errors='replace')}")
    except Exception as e:  # noqa: BLE001
        note(f"  api.github.com -> 失败 {type(e).__name__}: {e}")

    note("=== 判读 ===")
    note("  若第 4 步返回 200 -> 可用硬编码 IP 绕开 DNS，程序能跑")
    note("  若第 4 步也失败   -> 该 FC 区域到平台网络不通，需换区域")
    return out

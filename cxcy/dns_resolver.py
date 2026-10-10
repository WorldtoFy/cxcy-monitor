"""DNS 解析器：把域名解析成 IP，带三级回退。

为什么需要它：
`cxcy.upln.cn` 的 A 记录在国内多数公共 DNS 上查询会返回 SERVFAIL：
    223.5.5.5（阿里云）    -> SERVFAIL
    119.29.29.29（腾讯）   -> SERVFAIL
    8.8.8.8                -> 61.161.225.202
    114.114.114.114        -> 59.46.55.11
阿里云 FC 恰好用阿里云 DNS，因此函数内 socket.getaddrinfo 直接失败。

三级回退：
    ① 系统解析器（socket.getaddrinfo）
    ② 手动构造 DNS 报文直接问公共解析器（绕开系统解析器配置）
    ③ 已知 IP 兜底（前两者都失败时使用，实测两个 IP 均可用）
"""

from __future__ import annotations

import random
import socket
import struct
import time

# 实测可用的平台 IP（GSLB 会返回不同节点，两个都验证过能取到数据）
FALLBACK_IPS = ("61.161.225.202", "59.46.55.11")

# 公共 DNS：优先国内可达的，再补国外的
PUBLIC_DNS = ("223.5.5.5", "119.29.29.29", "114.114.114.114", "8.8.8.8", "1.1.1.1")

_cache: dict[str, tuple[list[str], float]] = {}
_TTL = 600.0


def raw_dns_query(server: str, name: str, timeout: float = 5.0) -> list[str]:
    """手工构造 DNS A 查询（UDP/53），返回 IP 列表；失败返回空列表。

    不用 socket.getaddrinfo，因为那会走系统解析器 —— 而系统解析器可能
    正是那个查不到记录的服务器。
    """
    tid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.split(".")) + b"\x00"
    packet = header + qname + struct.pack(">HH", 1, 1)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(packet, (server, 53))
        data, _ = s.recvfrom(2048)
    except OSError:
        return []
    finally:
        s.close()

    if len(data) < 12 or (data[3] & 0x0F) != 0:
        return []                       # rcode != 0：SERVFAIL / NXDOMAIN 等

    ancount = struct.unpack(">H", data[6:8])[0]
    i = 12
    try:
        while data[i] != 0:             # 跳过 QNAME
            i += data[i] + 1
        i += 5                          # QTYPE + QCLASS
    except IndexError:
        return []

    ips: list[str] = []
    for _ in range(ancount):
        try:
            if data[i] & 0xC0 == 0xC0:  # 压缩指针
                i += 2
            else:
                while data[i] != 0:
                    i += data[i] + 1
                i += 1
            rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[i:i + 10])
            i += 10
            if rtype == 1 and rdlen == 4:
                ips.append(".".join(str(b) for b in data[i:i + 4]))
            i += rdlen
        except (IndexError, struct.error):
            break
    return ips


def resolve(host: str, _port: int = 443, *, use_cache: bool = True) -> list[str]:
    """三级回退解析，返回可用 IP 列表（可能为空）。"""
    now = time.time()
    if use_cache and host in _cache:
        ips, ts = _cache[host]
        if now - ts < _TTL and ips:
            return ips

    # ① 系统解析器
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        ips = sorted({i[4][0] for i in infos})
        if ips:
            _cache[host] = (ips, now)
            return ips
    except OSError:
        pass

    # ② 直接问公共 DNS
    for server in PUBLIC_DNS:
        ips = raw_dns_query(server, host)
        if ips:
            _cache[host] = (ips, now)
            return ips

    # ③ 硬编码兜底（仅对已知平台域名）
    if host.endswith("upln.cn"):
        _cache[host] = (list(FALLBACK_IPS), now)
        return list(FALLBACK_IPS)

    return []


def clear_cache() -> None:
    _cache.clear()

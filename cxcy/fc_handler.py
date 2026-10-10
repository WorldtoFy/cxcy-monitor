"""阿里云函数计算（FC）入口。

与 ECS 版本的区别：FC 是无状态的，每次调用都是全新环境，
不能依赖本地文件保存去重状态，否则每次都会把全部竞赛当成"新竞赛"重复推送。

因此本入口把状态持久化到 **GitHub 仓库**（而非 OSS）：
  优点：① 不用额外开通 OSS；② 状态天然带版本历史；③ 状态更新后
        GitHub Pages 会自动重新部署，手机网页跟着同步更新。
  需要：一个 GitHub Token（环境变量 GH_TOKEN），只需对这一个仓库有写权限。

FC 控制台配置：
  函数入口   : fc_handler.handler
  运行环境   : Python 3.12
  依赖       : requirements-fc.txt（requests / urllib3 / PyGithub 不需要，直接用 HTTP）
  环境变量   : NTFY_TOPIC、GH_TOKEN、GH_REPO(owner/name)
  时间触发器 : cron 表达式 0 0 0,6,12,18 * * *   （每 6 小时）
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from datetime import datetime

from .fetch import Platform, detail_page_url
from .notify import Ntfy
from .parse import extract_deadlines, html_to_text, is_award_notice
from .store import REMIND_LEVELS, Store

API = "https://api.github.com"

# 需要持久化的文件（state 是去重状态，details 是竞赛正文，comps 供网页读取）
PERSIST = ("data/state.json", "data/details.json", "data/comps.json")

# 供手机网页读取的获奖公示清单（附件只给元信息，下载走官方页面）
AWARDS_FILE = "data/awards.json"


class GitHubFiles:
    """把 GitHub 仓库当成无状态环境下的持久化存储。"""

    def __init__(self, repo: str, token: str, branch: str = "main") -> None:
        self.repo = repo
        self.token = token
        self.branch = branch

    def _call(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(f"{API}{path}", data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode()
                return r.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def read(self, rel: str) -> tuple[str, str]:
        """返回 (内容, sha)；文件不存在时返回 ("", "")。"""
        code, j = self._call("GET", f"/repos/{self.repo}/contents/{rel}?ref={self.branch}")
        if code != 200:
            return "", ""
        try:
            return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]
        except Exception:  # noqa: BLE001
            return "", ""

    def write(self, rel: str, text: str, sha: str, message: str) -> bool:
        payload = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode(),
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha
        code, j = self._call("PUT", f"/repos/{self.repo}/contents/{rel}", payload)
        if code in (200, 201):
            return True
        print(f"! 写入 {rel} 失败 HTTP {code}: {j.get('message')}")
        return False


def handler(event, context):  # noqa: ANN001 - FC 的固定签名
    """FC 入口。

    返回值刻意包含完整诊断信息：当函数未配置执行角色时日志不可用，
    此时「返回结果」是唯一的观测手段，因此不能依赖 print。
    """
    logs: list[str] = []
    started = datetime.now()

    def note(msg: str) -> None:
        logs.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
        print(msg)          # 有日志通道时也会进 SLS

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    token = os.environ.get("GH_TOKEN", "").strip()
    repo = os.environ.get("GH_REPO", "WorldtoFy/cxcy-monitor").strip()
    seed = os.environ.get("SEED", "").strip() == "1"

    note(f"启动；repo={repo} seed={seed} topic={'已配置' if topic else '缺失'}")

    if not topic:
        return {"ok": False, "error": "未配置 NTFY_TOPIC", "logs": logs}
    if not token:
        return {"ok": False, "error": "未配置 GH_TOKEN（无状态环境需要它读写状态）", "logs": logs}

    gh = GitHubFiles(repo, token)

    # ---- 1. 从仓库恢复状态 ----
    cached: dict[str, tuple[str, str]] = {}
    for rel in PERSIST:
        try:
            text, sha = gh.read(rel)
            cached[rel] = (text, sha)
            note(f"读取 {rel}: {len(text)} 字符")
        except Exception as e:  # noqa: BLE001
            note(f"读取 {rel} 失败: {e}")
            cached[rel] = ("", "")

    store = Store.__new__(Store)          # 不走文件系统，手工装配
    store.path = None                     # type: ignore[assignment]
    store.data = {"version": 1, "comps": {}, "awards": {}, "runs": []}
    if cached.get("data/state.json", ("", ""))[0]:
        try:
            store.data = json.loads(cached["data/state.json"][0])
        except json.JSONDecodeError as e:
            note(f"state.json 解析失败，改用空状态: {e}")
    store.data.setdefault("comps", {})
    store.data.setdefault("awards", {})
    store.data.setdefault("runs", [])
    store._details = {}
    if cached.get("data/details.json", ("", ""))[0]:
        try:
            store._details = json.loads(cached["data/details.json"][0])
        except json.JSONDecodeError:
            pass
    note(f"恢复状态: {len(store.data['comps'])} 个竞赛, {len(store.data['awards'])} 条公示")

    first_run = not store.data["comps"]
    seed = seed or first_run
    if first_run:
        note("检测到空状态，自动进入 seed 模式（只建基线，不推送）")

    # ---- 2. 抓取竞赛 ----
    plat = Platform()
    year = datetime.now().year
    try:
        comps = plat.ongoing(year)
    except Exception as e:  # noqa: BLE001
        note(f"抓取失败: {type(e).__name__}: {e}")
        return {"ok": False, "error": f"抓取竞赛列表失败: {e}", "logs": logs}

    note(f"抓到 {year} 年进行中竞赛 {len(comps)} 个")
    if not comps:
        return {"ok": False, "error": f"{year} 年抓到 0 个竞赛，疑接口异常，已终止以免误判",
                "logs": logs}

    dry = os.environ.get("DRY_RUN", "").strip() == "1"
    pusher = None if dry else Ntfy(topic, os.environ.get("NTFY_SERVER", "https://ntfy.sh"))
    note(f"推送通道: {'DRY_RUN（不推送）' if dry else 'ntfy 已就绪'}")

    def days_left_of(end: str) -> int | None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                end_dt = datetime.strptime(end.strip(), fmt)
                break
            except (ValueError, AttributeError):
                continue
        else:
            return None
        secs = (end_dt - datetime.now()).total_seconds()
        return -1 if secs < 0 else int(secs // 86400)

    comps.sort(key=lambda c: days_left_of(c.get("endTime") or "") or 9999)

    new_count = alerted = detail_ok = 0
    cap = int(os.environ.get("ALERT_CAP", "4"))
    for c in comps:
        cid, name, end = c["id"], (c.get("name") or "").strip(), c.get("endTime") or ""
        days = days_left_of(end)
        is_new = store.is_new(cid)

        detail_text = store.detail_of(cid)
        deadlines = store.get(cid).get("deadlines", [])
        attach = store.get(cid).get("attachments", 0)
        if is_new or not detail_text.strip():
            try:
                d = plat.detail(cid)
                detail_text = html_to_text(d.get("detail"))
                deadlines = extract_deadlines(detail_text)
                attach = len(d.get("fileList") or [])
            except Exception as e:  # noqa: BLE001
                note(f"详情抓取失败 {name}: {e}")
        if detail_text.strip():
            detail_ok += 1

        store.upsert(cid, {
            "name": name, "synopsis": (c.get("synopsis") or "").strip(),
            "year": c.get("year"), "start": c.get("startTime"), "end": end,
            "days_left": days, "deadlines": deadlines, "attachments": attach,
            "url": detail_page_url(cid),
        })
        store.set_detail(cid, detail_text)

        if seed:
            continue
        if is_new:
            new_count += 1
            if pusher and alerted < cap:
                alerted += 1
                pusher.send("🆕 新竞赛发布", _one_liner(name, end, days),
                            click=detail_page_url(cid), tags=["new"], priority=4)
                note(f"推送新竞赛: {name[:30]}")
            continue
        if days is not None and days >= 0:
            levels = store.pending_levels(cid, days)
            if levels and pusher and alerted < cap:
                lv = levels[0]
                alerted += 1
                icon, prio = {7: ("⏳", 3), 3: ("⚠️", 4), 1: ("🔥", 5), 0: ("🚨", 5)}.get(lv, ("⏰", 3))
                pusher.send(f"{icon} {name[:28]}", _one_liner(name, end, days),
                            click=detail_page_url(cid), tags=["warning"], priority=prio)
                note(f"推送提醒[{lv}天]: {name[:30]}")
                for lv2 in levels:
                    store.mark_reminded(cid, lv2)
            elif levels:
                for lv2 in levels:
                    store.mark_reminded(cid, lv2)

    note(f"竞赛处理完成: 新增 {new_count}, 推送 {alerted}, 详情覆盖 {detail_ok}/{len(comps)}")

    # ---- 3. 获奖公示 ----
    award_pushed = 0
    if os.environ.get("AWARD_NOTIFY", "1") != "0":
        try:
            award_pushed = _monitor_awards(store, plat, pusher, seed,
                                           int(os.environ.get("AWARD_CAP", "3")), note)
            note(f"获奖公示推送 {award_pushed} 条")
        except Exception as e:  # noqa: BLE001
            note(f"获奖公示监测失败: {type(e).__name__}: {e}")

    store.log_run({"total": len(comps), "new": new_count,
                   "alerted": alerted, "awards": award_pushed, "seed": bool(seed)})

    # ---- 4. 写回仓库（触发 Pages 重新部署，手机网页同步更新）----
    # 注意：这里刻意不加 [skip ci]，因为正是要靠这次提交触发 Pages 部署
    saved: list[str] = []
    failed: list[str] = []
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    new_state = json.dumps(store.data, ensure_ascii=False, indent=1, sort_keys=True)
    if new_state != cached.get("data/state.json", ("", ""))[0]:
        if gh.write("data/state.json", new_state, cached.get("data/state.json", ("", ""))[1],
                    f"chore: 更新监控状态 {stamp}"):
            saved.append("state")
        else:
            failed.append("state")

    live = {k: v for k, v in store._details.items() if k in store.data["comps"]}
    new_details = json.dumps(live, ensure_ascii=False)
    if new_details != cached.get("data/details.json", ("", ""))[0]:
        if gh.write("data/details.json", new_details, cached.get("data/details.json", ("", ""))[1],
                    f"chore: 更新竞赛详情 {stamp}"):
            saved.append("details")
        else:
            failed.append("details")

    web = _web_payload(store)
    if web != cached.get("data/comps.json", ("", ""))[0]:
        if gh.write("data/comps.json", web, cached.get("data/comps.json", ("", ""))[1],
                    f"chore: 更新网页数据 {stamp}"):
            saved.append("comps")
        else:
            failed.append("comps")

    # 顺手导出获奖公示清单供网页读取（复用已有 GitHub 通道，无需额外配置）
    try:
        awards_payload = _awards_payload(plat, limit=40)
        if awards_payload:
            old, sha = gh.read(AWARDS_FILE)
            if awards_payload != old:
                if gh.write(AWARDS_FILE, awards_payload, sha, f"chore: 更新公示清单 {stamp}"):
                    saved.append("awards")
                else:
                    failed.append("awards")
    except Exception as e:  # noqa: BLE001
        note(f"导出公示清单失败: {type(e).__name__}: {e}")

    note(f"写回仓库: 成功 {saved or '无'} 失败 {failed or '无'}")

    return {
        "ok": True,
        "date": stamp,
        "seed": seed,
        "elapsed_sec": round((datetime.now() - started).total_seconds(), 1),
        "comps": len(comps),
        "detail_ok": detail_ok,
        "new": new_count,
        "alerted": alerted,
        "awards": award_pushed,
        "saved": saved,
        "failed": failed,
        "logs": logs,
    }


def _one_liner(name: str, end: str, days: int | None) -> str:
    dl = (end or "?")[:10]
    if days is None:
        return f"{name} · 报名 {dl} 截止"
    if days < 0:
        return f"{name} · 报名已于 {dl} 截止"
    if days == 0:
        return f"{name} · 今天（{dl}）截止"
    return f"{name} · 报名 {dl} 截止（剩 {days} 天）"


def _web_payload(store: Store) -> str:
    comps = []
    for cid, r in store.data["comps"].items():
        comps.append({
            "id": cid, "name": r.get("name", ""), "host": r.get("host", ""),
            "synopsis": r.get("synopsis", ""), "start": r.get("start", ""),
            "end": r.get("end", ""), "days_left": r.get("days_left"),
            "attachments": r.get("attachments", 0),
            "deadlines": r.get("deadlines", []),
            "detail": store.detail_of(cid), "url": r.get("url", ""),
        })
    comps.sort(key=lambda x: (x["days_left"] is None or x["days_left"] < 0,
                              x["days_left"] if x["days_left"] is not None else 9999))
    return json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"),
                       "count": len(comps), "comps": comps},
                      ensure_ascii=False, indent=1)


def _monitor_awards(store: Store, plat: Platform, pusher: Ntfy | None,
                    seed: bool, cap: int, note=None) -> int:
    def log(msg: str) -> None:
        print(msg)
        if note:
            note(msg)

    anns: list[dict] = []
    for p in range(1, 5):
        recs, _t = plat.announcements(page=p, size=100)
        if not recs:
            break
        anns += recs

    fresh = [a for a in anns
             if not store.award_seen(a.get("id", "")) and is_award_notice(a.get("name") or "")]
    if seed:
        for a in fresh:
            store.mark_award(a["id"], {"name": a.get("name"), "time": a.get("createTime")})
        return 0

    pushed = 0
    for a in fresh:
        aid, title = a["id"], (a.get("name") or "").strip()
        # 只取附件元信息（名称/大小/文件id），**不在推送时取票据**：
        # 票据是短期凭证，而通知会长期留在通知栏里，取早了用户点开时已失效。
        # 下载改由手机网页按需换取新票据。
        files: list[dict] = []
        try:
            detail = plat.announcement(aid)
            for f in (detail.get("fileList") or []):
                files.append({"name": f.get("name"), "size": f.get("size"),
                              "id": f.get("id")})
        except Exception as e:  # noqa: BLE001
            print(f"! 公告详情失败 {title}: {e}")

        store.mark_award(aid, {"name": title, "time": a.get("createTime"),
                               "competition_id": a.get("competitionId"),
                               "files": [{"name": f["name"], "size": f["size"],
                                          "id": f.get("id")} for f in files]})
        if not pusher or pushed >= cap:
            continue
        # 点击跳转到手机网页的对应锚点（永久有效），而不是一次性票据链接。
        # 注意必须带 /web/ 路径：根目录是 meta refresh 跳转页，会丢掉查询参数。
        body = f"{title[:60]}\n{(a.get('createTime') or '')[:16]}"
        if files:
            n = len(files)
            body += f"\n📎 {files[0]['name'][:42]}" + (f" 等 {n} 个文件" if n > 1 else "")
            body += "\n点此在网页中查看与下载"
        pusher.send("🏅 获奖公示", body,
                    click=f"{WEB_BASE}/web/?award={aid}",
                    tags=["trophy"], priority=4)
        pushed += 1
    return pushed


# 通知点击跳转到手机网页（永久有效），而不是会过期的一次性票据链接
WEB_BASE = os.environ.get(
    "WEB_BASE", "https://worldtofy.github.io/cxcy-monitor").rstrip("/")


def _awards_payload(plat: Platform, limit: int = 40) -> str:
    """导出获奖公示清单（含附件名称/大小）供手机网页读取。

    不含下载票据 —— 票据是短期凭证，必须点击时才取。
    附件下载引导到官方公告页，那里是稳定地址。
    """
    out: list[dict] = []
    for page in range(1, 4):
        recs, _t = plat.announcements(page=page, size=100)
        if not recs:
            break
        for a in recs:
            if not is_award_notice(a.get("name") or ""):
                continue
            out.append({
                "id": a.get("id"),
                "name": (a.get("name") or "").strip(),
                "time": a.get("createTime"),
                "competitionId": a.get("competitionId"),
                "page": f"https://cxcy.upln.cn/competitionDetails?id={a.get('id')}",
                "files": [],
            })
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break

    # 前若干条附带附件清单（仍不含票据）
    for a in out[:12]:
        try:
            d = plat.announcement(a["id"])
            a["files"] = [{"name": f.get("name"), "size": f.get("size")}
                          for f in (d.get("fileList") or [])]
        except Exception:  # noqa: BLE001
            pass

    return json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"),
                       "count": len(out), "awards": out},
                      ensure_ascii=False, indent=1)


def list_awards(event, context):  # noqa: ANN001 - FC 函数入口（供手机网页调用）
    """返回最近的获奖公示列表。只给元信息，不给票据。"""
    limit = 40
    try:
        limit = int((event or {}).get("limit") or 40)
    except (TypeError, ValueError):
        pass

    plat = Platform()
    out: list[dict] = []
    for page in range(1, 4):
        recs, _t = plat.announcements(page=page, size=100)
        if not recs:
            break
        for a in recs:
            if is_award_notice(a.get("name") or ""):
                out.append({
                    "id": a.get("id"),
                    "name": (a.get("name") or "").strip(),
                    "time": a.get("createTime"),
                    "competitionId": a.get("competitionId"),
                })
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break

    # 前若干条附带附件清单（仍不含票据）
    for a in out[:12]:
        try:
            d = plat.announcement(a["id"])
            a["files"] = [{"name": f.get("name"), "size": f.get("size"), "id": f.get("id")}
                          for f in (d.get("fileList") or [])]
        except Exception:  # noqa: BLE001
            a["files"] = []

    return {"ok": True, "count": len(out), "awards": out}


def get_download_url(event, context):  # noqa: ANN001 - FC 函数入口（供手机网页调用）
    """按需换取一次新的下载票据。

    票据是短期凭证，必须"点的时候才取"——这也是它不能提前写进通知的原因。
    """
    file_id = str((event or {}).get("fileId") or "").strip()
    if not file_id:
        return {"ok": False, "error": "缺少 fileId"}

    plat = Platform()
    try:
        url = plat.download_url(file_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 校验票据确实能取到文件，避免把坏链接交给用户
    try:
        r = plat.s.get(url, timeout=30, stream=True)
        size = int(r.headers.get("content-length") or 0)
        r.close()
        if r.status_code != 200:
            return {"ok": False, "error": f"票据校验失败 HTTP {r.status_code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"票据校验异常: {type(e).__name__}"}

    return {"ok": True, "url": url, "size": size}

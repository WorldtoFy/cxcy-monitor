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
    """FC 入口。返回体只用于日志，不参与业务。"""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    token = os.environ.get("GH_TOKEN", "").strip()
    repo = os.environ.get("GH_REPO", "WorldtoFy/cxcy-monitor").strip()
    seed = os.environ.get("SEED", "").strip() == "1"

    if not topic:
        return {"ok": False, "error": "未配置 NTFY_TOPIC"}
    if not token:
        return {"ok": False, "error": "未配置 GH_TOKEN（无状态环境需要它读写状态）"}

    gh = GitHubFiles(repo, token)

    # ---- 1. 从仓库恢复状态 ----
    cached: dict[str, tuple[str, str]] = {}
    for rel in PERSIST:
        text, sha = gh.read(rel)
        cached[rel] = (text, sha)

    store = Store.__new__(Store)          # 不走文件系统，手工装配
    store.path = None                     # type: ignore[assignment]
    store.data = {"version": 1, "comps": {}, "awards": {}, "runs": []}
    if cached["data/state.json"][0]:
        try:
            store.data = json.loads(cached["data/state.json"][0])
        except json.JSONDecodeError:
            pass
    store.data.setdefault("comps", {})
    store.data.setdefault("awards", {})
    store.data.setdefault("runs", [])
    store._details = {}
    if cached["data/details.json"][0]:
        try:
            store._details = json.loads(cached["data/details.json"][0])
        except json.JSONDecodeError:
            pass

    first_run = not store.data["comps"]
    seed = seed or first_run

    plat = Platform()
    # DRY_RUN=1 时只跑逻辑不推送，用于测试与排障
    dry = os.environ.get("DRY_RUN", "").strip() == "1"
    pusher = None if dry else Ntfy(topic, os.environ.get("NTFY_SERVER", "https://ntfy.sh"))

    # ---- 2. 竞赛：判新 + 到档提醒 ----
    year = datetime.now().year
    comps = plat.ongoing(year)
    if not comps:
        return {"ok": False, "error": f"{year} 年抓到 0 个竞赛，疑接口异常，已终止以免误判"}

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

    new_count = alerted = 0
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
                print(f"! 详情抓取失败 {name}: {e}")

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
            continue
        if days is not None and days >= 0:
            levels = store.pending_levels(cid, days)
            if levels and pusher and alerted < cap:
                lv = levels[0]
                alerted += 1
                icon, prio = {7: ("⏳", 3), 3: ("⚠️", 4), 1: ("🔥", 5), 0: ("🚨", 5)}.get(lv, ("⏰", 3))
                pusher.send(f"{icon} {name[:28]}", _one_liner(name, end, days),
                            click=detail_page_url(cid), tags=["warning"], priority=prio)
                for lv2 in levels:
                    store.mark_reminded(cid, lv2)
            elif levels:
                # 无推送通道（dry-run）时也要标记，避免下次重复判定
                for lv2 in levels:
                    store.mark_reminded(cid, lv2)

    # ---- 3. 获奖公示 ----
    award_pushed = 0
    if os.environ.get("AWARD_NOTIFY", "1") != "0":
        try:
            award_pushed = _monitor_awards(store, plat, pusher, seed,
                                           int(os.environ.get("AWARD_CAP", "3")))
        except Exception as e:  # noqa: BLE001
            print(f"! 获奖公示监测失败: {e}")

    store.log_run({"total": len(comps), "new": new_count,
                   "alerted": alerted, "awards": award_pushed, "seed": bool(seed)})

    # ---- 4. 写回仓库（触发 Pages 重新部署，手机网页同步更新）----
    # 注意：这里刻意不加 [skip ci]，因为正是要靠这次提交触发 Pages 部署
    saved = []
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_state = json.dumps(store.data, ensure_ascii=False, indent=1, sort_keys=True)
    if new_state != cached["data/state.json"][0]:
        if gh.write("data/state.json", new_state, cached["data/state.json"][1],
                    f"chore: 更新监控状态 {stamp}"):
            saved.append("state")

    live = {k: v for k, v in store._details.items() if k in store.data["comps"]}
    new_details = json.dumps(live, ensure_ascii=False)
    if new_details != cached["data/details.json"][0]:
        if gh.write("data/details.json", new_details, cached["data/details.json"][1],
                    f"chore: 更新竞赛详情 {stamp}"):
            saved.append("details")

    web = _web_payload(store)
    if web != cached["data/comps.json"][0]:
        if gh.write("data/comps.json", web, cached["data/comps.json"][1],
                    f"chore: 更新网页数据 {stamp}"):
            saved.append("comps")

    return {
        "ok": True,
        "date": stamp,
        "seed": seed,
        "comps": len(comps),
        "new": new_count,
        "alerted": alerted,
        "awards": award_pushed,
        "saved": saved,
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
                    seed: bool, cap: int) -> int:
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
        files: list[dict] = []
        try:
            detail = plat.announcement(aid)
            for f in (detail.get("fileList") or []):
                try:
                    files.append({"name": f.get("name"), "size": f.get("size"),
                                  "url": plat.download_url(f["id"])})
                except Exception as e:  # noqa: BLE001
                    print(f"! 附件取票据失败 {f.get('name')}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"! 公告详情失败 {title}: {e}")

        store.mark_award(aid, {"name": title, "time": a.get("createTime"),
                               "competition_id": a.get("competitionId"),
                               "files": [{"name": f["name"], "size": f["size"]} for f in files]})
        if not pusher or pushed >= cap:
            continue
        first = files[0]["url"] if files else f"https://cxcy.upln.cn/competitionDetails?id={aid}"
        body = f"{title[:60]}\n{(a.get('createTime') or '')[:16]}"
        if files:
            body += f"\n📎 {files[0]['name'][:50]}" + (f" 等 {len(files)} 个文件" if len(files) > 1 else "")
        pusher.send("🏅 获奖公示", body, click=first, tags=["trophy"], priority=4)
        pushed += 1
    return pushed

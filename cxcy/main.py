"""主流程：抓取 → 比对 → 判定提醒 → 推送 → 存状态。

用法：
    python -m cxcy.main                    # 正常跑一次
    python -m cxcy.main --dry-run          # 只打印不推送（安全测试）
    python -m cxcy.main --seed             # 首次运行：只记录基线，不推送
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .fetch import ANNOUNCE_PAGE, Platform, detail_page_url
from .notify import Ntfy, from_env as pusher_from_env
from .parse import extract_deadlines, html_to_text, is_award_notice
from .store import REMIND_LEVELS, Store

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "state.json"
CONFIG = ROOT / "config.json"


def load_config() -> dict:
    """读取 config.json（可缺省）。文件里的 _开头 键是说明文字，会被忽略。"""
    if not CONFIG.exists():
        return {}
    try:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"! config.json 解析失败，改用默认值: {e}", file=sys.stderr)
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}

LEVEL_TAG = {
    7: ("⏳", 3),
    3: ("⚠️", 4),
    1: ("🔥", 5),
    0: ("🚨", 5),
}

# 单轮推送上限：防止状态重建或首次上线时一次涌出十几条通知
DEFAULT_ALERT_CAP = 4
# 获奖公示单轮上限（与截止提醒分开计，避免互相挤占配额）
DEFAULT_AWARD_CAP = 3


def days_until(end_str: str) -> int | None:
    """剩余整天数（向下取整，与界面展示口径一致）。

    已过期返回 -1；今天之内到期返回 0。
    """
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            end = datetime.strptime(end_str.strip(), fmt)
            break
        except (ValueError, AttributeError):
            continue
    else:
        return None
    secs = (end - datetime.now()).total_seconds()
    return -1 if secs < 0 else int(secs // 86400)


def fmt_deadline(end_str: str) -> str:
    try:
        return datetime.strptime(end_str.strip(), "%Y-%m-%d %H:%M:%S").strftime("%m-%d")
    except (ValueError, AttributeError):
        return (end_str or "?")[:10]


def one_liner(name: str, end_str: str, days: int | None) -> str:
    dl = fmt_deadline(end_str)
    if days is None:
        return f"{name} · 报名 {dl} 截止"
    if days < 0:
        return f"{name} · 报名已于 {dl} 截止"
    if days == 0:
        return f"{name} · 今天（{dl}）截止"
    return f"{name} · 报名 {dl} 截止（剩 {days} 天）"


def monitor_awards(store: Store, plat: Platform, pusher: Ntfy | None,
                   seed: bool, cap: int, watch: set[str] | None,
                   pages: int = 4) -> int:
    """监测大赛动态里的获奖公示，推送标题 + 附件下载链接。

    平台附件是"一次性票据"机制，URL 不能长期缓存，故在推送前实时换取。
    默认只看最近 pages 页（每页 100 条），足够覆盖 6 小时内的新公告。
    """
    anns: list[dict] = []
    for p in range(1, pages + 1):
        recs, _total = plat.announcements(page=p, size=100)
        if not recs:
            break
        anns += recs

    fresh = [a for a in anns
             if not store.award_seen(a.get("id", ""))
             and is_award_notice(a.get("name") or "")
             and (not watch or a.get("competitionId") in watch)]

    if seed:
        for a in fresh:
            store.mark_award(a["id"], {"name": a.get("name"), "time": a.get("createTime")})
        if fresh:
            print(f"  · 获奖公示基线已建立：{len(fresh)} 条（seed 模式，不推送）")
        return 0

    pushed = 0
    for a in fresh:
        aid = a["id"]
        title = (a.get("name") or "").strip()
        when = (a.get("createTime") or "")[:16]
        files: list[dict] = []
        try:
            detail = plat.announcement(aid)
            for f in (detail.get("fileList") or []):
                try:
                    files.append({
                        "name": f.get("name"),
                        "size": f.get("size"),
                        "url": plat.download_url(f["id"]),
                    })
                except Exception as e:  # noqa: BLE001
                    print(f"    ! 附件取票据失败 {f.get('name')}: {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"    ! 公告详情抓取失败 {title}: {e}", file=sys.stderr)

        store.mark_award(aid, {
            "name": title, "time": a.get("createTime"),
            "competition_id": a.get("competitionId"),
            "files": [{"name": f["name"], "size": f["size"]} for f in files],
        })

        first = files[0]["url"] if files else ANNOUNCE_PAGE.format(id=aid)
        print(f"  [公示] {title[:44]}  附件 {len(files)} 个")
        if not pusher or pushed >= cap:
            continue
        body = f"{title[:60]}\n{when}"
        if files:
            n = len(files)
            body += f"\n📎 {files[0]['name'][:50]}" + (f" 等 {n} 个文件" if n > 1 else "")
        pusher.send("🏅 获奖公示", body, click=first,
                    tags=["trophy"], priority=4)
        pushed += 1

    if pushed:
        print(f"  · 获奖公示推送 {pushed} 条" + (f"（限额 {cap}）" if len(fresh) > cap else ""))
    return pushed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="创新创业竞赛监控与提醒")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不真正推送")
    ap.add_argument("--seed", action="store_true",
                    help="首次运行：建立基线，不对已有竞赛推送")
    ap.add_argument("--no-detail", action="store_true",
                    help="跳过详情抓取（更快，但无子截止时间）")
    ap.add_argument("--year", type=int, default=None, help="限定年度")
    ap.add_argument("--max-alerts", type=int, default=0,
                    help="单轮最多推送几条截止提醒（0=用默认上限）。")
    ap.add_argument("--no-awards", action="store_true",
                    help="跳过获奖公示监测。")
    ap.add_argument("--award-cap", type=int, default=DEFAULT_AWARD_CAP,
                    help=f"单轮最多推送几条获奖公示（默认 {DEFAULT_AWARD_CAP}）。")
    ap.add_argument("--watch-comps", default="",
                    help="只监测这些竞赛 id（逗号分隔）的获奖公示；留空=全部竞赛。")
    ap.add_argument("--award-pages", type=int, default=4,
                    help="扫描最近几页公告（每页 100 条，默认 4 页）。")
    args = ap.parse_args(argv)
    # 保险：DRY_RUN=1 等价于 --dry-run，防止在已上线环境里误运行本机采集
    # （本机与云端共用同一份 GitHub 状态，真跑会互相"消费"提醒标记）
    if os.environ.get("DRY_RUN", "").strip() == "1":
        args.dry_run = True
    cfg = load_config()
    # 命令行优先，其次 config.json，最后默认值
    if args.award_cap == DEFAULT_AWARD_CAP and "award_cap" in cfg:
        args.award_cap = int(cfg["award_cap"])
    if args.award_pages == 4 and "award_pages" in cfg:
        args.award_pages = int(cfg["award_pages"])
    if not args.watch_comps and cfg.get("watch_competitions"):
        args.watch_comps = ",".join(str(x) for x in cfg["watch_competitions"])
    if not args.no_awards and cfg.get("award_notify") is False:
        args.no_awards = True

    store = Store(STATE)
    first_run = not store.data["comps"]
    seed = args.seed or first_run
    if seed and not args.seed:
        print("检测到首次运行（状态为空），自动进入 --seed 模式：只建基线，不推送。")

    pusher = None if (args.dry_run or seed) else pusher_from_env()
    if not args.dry_run and not seed and pusher is None:
        print("错误：未配置 NTFY_TOPIC，无法推送。可用 --dry-run 先看效果。",
              file=sys.stderr)
        return 2

    plat = Platform()
    # 注意：接口必须带 year，省略会返回 0 条
    year = args.year or datetime.now().year
    comps = plat.ongoing(year)
    print(f"抓到 {year} 年进行中竞赛 {len(comps)} 个")
    if not comps:
        # 静默返回 0 条通常意味着接口变了或被拦截，必须显式失败
        print("错误：抓到 0 个竞赛，疑接口变动或网络异常，终止以免误判。",
              file=sys.stderr)
        return 3

    # 按紧急度排序，确保 --max-alerts 限额永远优先保住最紧急的竞赛
    def _sort_key(c: dict) -> tuple[int, str]:
        d = days_until(c.get("endTime") or "")
        return (9999 if d is None else (9999 if d < 0 else d), c.get("endTime") or "")

    comps.sort(key=_sort_key)

    # 安全闸：状态被重建或首次上线时，可能一次性涌出大量提醒。
    # 默认单轮上限 4 条，可用 --max-alerts 调整，0 表示不限。
    alert_cap = args.max_alerts if args.max_alerts else DEFAULT_ALERT_CAP

    new_count = reminded = alerted = skipped = 0

    for c in comps:
        cid = c["id"]
        name = (c.get("name") or "").strip()
        end = c.get("endTime") or ""
        days = days_until(end)
        is_new = store.is_new(cid)

        # 详情抓取：网页端要展示全文，故默认全部抓取；
        # 若正文已存在（details.json 里有），则跳过以省请求。
        have_detail = bool((store.detail_of(cid) or "").strip())
        detail_text, deadlines, attach = (
            store.detail_of(cid),
            store.get(cid).get("deadlines", []),
            store.get(cid).get("attachments", 0),
        )
        if not args.no_detail and (is_new or not have_detail):
            try:
                d = plat.detail(cid)
                detail_text = html_to_text(d.get("detail"))
                deadlines = extract_deadlines(detail_text)
                attach = len(d.get("fileList") or [])
                if not detail_text:
                    print(f"  · 详情为空（平台未上传正文）: {name}")
            except Exception as e:  # noqa: BLE001 - 详情失败不应中断整轮
                print(f"  ! 详情抓取失败 {name}: {e}", file=sys.stderr)

        store.upsert(cid, {
            "name": name,
            "synopsis": (c.get("synopsis") or "").strip(),
            "year": c.get("year"),
            "start": c.get("startTime"),
            "end": end,
            "days_left": days,
            "host": store.get(cid).get("host", ""),
            "deadlines": deadlines,
            "attachments": attach,
            "url": detail_page_url(cid),
        })
        # 正文走独立文件（只存哈希在 state 里），避免 git 每次提交 200KB+ 重复内容
        store.set_detail(cid, detail_text)
        if seed:
            continue

        # ① 新竞赛
        if is_new:
            new_count += 1
            msg = one_liner(name, end, days)
            print(f"  [新] {msg}")
            if pusher and alerted < alert_cap:
                alerted += 1
                pusher.send("🆕 新竞赛发布", msg,
                            click=detail_page_url(cid), tags=["new"], priority=4)
            elif pusher:
                skipped += 1
            continue

        # ② 到档提醒（每个竞赛每轮最多推 1 条，绝不轰炸）
        if days is not None and days >= 0:
            levels = store.pending_levels(cid, days)
            if levels:
                lv = levels[0]           # 最紧急档位 = 本轮真正推送的那条
                odd = levels[1:]          # 更宽松的过时档位，静默标记
                icon, prio = LEVEL_TAG.get(lv, ("⏰", 3))
                msg = one_liner(name, end, days)
                # 最紧急的先推：配额已满则静默标记，避免下次补推时又涌出
                if alerted >= alert_cap:
                    store.mark_reminded(cid, lv)
                    for extra in odd:
                        store.mark_reminded(cid, extra)
                    skipped += 1
                    continue
                alerted += 1
                reminded += 1
                print(f"  [{lv}天] {msg}")

                if pusher:
                    pusher.send(f"{icon} {name[:28]}", msg,
                                click=detail_page_url(cid),
                                tags=["warning"], priority=prio)
                store.mark_reminded(cid, lv)
                for extra in odd:
                    store.mark_reminded(cid, extra)
        # ③ 刚过期，提示一次收尾
        elif days is not None and days < 0 and not store.closed_notified(cid):
            store.mark_closed(cid)
            print(f"  [截止] {name}")
            if pusher:
                pusher.send("🚫 报名已截止", one_liner(name, end, days),
                            click=detail_page_url(cid), tags=["no_entry"])

    # ③ 获奖公示监测（与截止提醒分开计配额）
    award_pushed = 0
    if not args.no_awards:
        watch = {s.strip() for s in args.watch_comps.split(",") if s.strip()} or None
        try:
            award_pushed = monitor_awards(store, plat, pusher, seed,
                                          args.award_cap, watch, args.award_pages)
        except Exception as e:  # noqa: BLE001 - 公示失败不应影响提醒主流程
            print(f"! 获奖公示监测失败: {e}", file=sys.stderr)

    store.log_run({"total": len(comps), "new": new_count,
                   "reminded": reminded, "alerted": alerted,
                   "skipped": skipped, "awards": award_pushed,
                   "seed": bool(seed)})
    store.save()
    export_web(store)

    # 自检摘要：便于在 shell 里可靠核对（避免依赖 shell 的编码/解析）
    with_detail = sum(1 for c in store.data["comps"] if store.detail_of(c).strip())
    print(f"\n完成：新竞赛 {new_count}，到档 {reminded}，实推 {alerted}"
          + (f"，因限额跳过 {skipped}" if skipped else "")
          + f"，获奖公示推送 {award_pushed}"
          + ("（seed 模式，未推送）" if seed else ""))
    print(f"详情覆盖：{with_detail}/{len(store.data['comps'])} 个竞赛有正文")
    return 0


def export_web(store: Store) -> None:
    """导出供手机网页读取的精简数据（不含状态机内部字段）。"""
    comps = []
    for cid, r in store.data["comps"].items():
        days = r.get("days_left")
        comps.append({
            "id": cid,
            "name": r.get("name", ""),
            "host": r.get("host", ""),
            "synopsis": r.get("synopsis", ""),
            "start": r.get("start", ""),
            "end": r.get("end", ""),
            "days_left": days,
            "attachments": r.get("attachments", 0),
            "deadlines": r.get("deadlines", []),
            "detail": store.detail_of(cid),
            "url": r.get("url", ""),
        })
    # 未截止的排前面，按剩余天数升序
    comps.sort(key=lambda x: (x["days_left"] is None or x["days_left"] < 0,
                              x["days_left"] if x["days_left"] is not None else 9999))
    out = {"generated_at": datetime.now().isoformat(timespec="seconds"),
           "count": len(comps), "comps": comps}
    path = ROOT / "data" / "comps.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

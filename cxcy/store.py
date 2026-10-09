"""状态存储：用 JSON 记录已见竞赛与已推送提醒，保证不重复打扰。

选 JSON 而非 SQLite 的原因：GitHub Actions 需要把状态提交回仓库，
JSON 的 diff 可读、冲突易处理。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

# 提醒档位：剩余天数 <= 该值且尚未推送过，则推一次
REMIND_LEVELS = (7, 3, 1)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {"version": 1, "comps": {}, "runs": []}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # 状态损坏时从头开始，宁可重推一次也不要崩掉整条流水线
                pass
        self.data.setdefault("version", 1)
        self.data.setdefault("comps", {})
        self.data.setdefault("awards", {})
        self.data.setdefault("runs", [])
        self._details: dict[str, str] = {}
        self.load_details()

    # ---- 查询 ----
    def is_new(self, com_id: str) -> bool:
        return com_id not in self.data["comps"]

    def get(self, com_id: str) -> dict[str, Any]:
        return self.data["comps"].get(com_id, {})

    def pending_levels(self, com_id: str, days_left: int) -> list[int]:
        """返回本次应推的档位；单次最多一个，取最紧急的那个。

        判定用 `<=` 而非 `==`：若某天任务没跑成（GitHub Actions 会延迟甚至跳过），
        下一个档位仍能补救，不会因为错过那一天就永久漏掉提醒。
        但绝不在一次运行里补推多个档位 —— 那会变成通知轰炸，
        所以只取最紧急的一个，其余档位直接标记为已过（不再补推）。
        """
        sent = {int(x) for x in self.get(com_id).get("reminded", [])}
        due = [lv for lv in REMIND_LEVELS if days_left <= lv and lv not in sent]
        if not due:
            return []
        urgent = min(due)
        # 比 urgent 更宽松的档位已过期，一并标记，避免后续补推
        return [urgent] + [lv for lv in due if lv > urgent]

    def closed_notified(self, com_id: str) -> bool:
        return bool(self.get(com_id).get("closed_notified"))

    # ---- 写入 ----
    def upsert(self, com_id: str, fields: dict[str, Any]) -> None:
        # detail 不允许写进 state（正文体积大且每次提交都会进 git），
        # 统一走 set_detail()，由调用方保证。
        fields = {k: v for k, v in fields.items() if k != "detail"}
        rec = self.data["comps"].setdefault(com_id, {})
        rec.update(fields)
        rec.setdefault("first_seen", datetime.now().isoformat(timespec="seconds"))
        rec["last_seen"] = datetime.now().isoformat(timespec="seconds")

    def mark_reminded(self, com_id: str, level: int) -> None:
        rec = self.data["comps"].setdefault(com_id, {})
        reminded = set(rec.get("reminded", []))
        reminded.add(str(level))
        rec["reminded"] = sorted(reminded, key=int)

    def mark_closed(self, com_id: str) -> None:
        self.data["comps"].setdefault(com_id, {})["closed_notified"] = True

    # ---------- 获奖公示去重 ----------
    def award_seen(self, ann_id: str) -> bool:
        return ann_id in self.data.setdefault("awards", {})

    def mark_award(self, ann_id: str, fields: dict[str, Any]) -> None:
        rec = self.data.setdefault("awards", {}).setdefault(ann_id, {})
        rec.update(fields)
        rec.setdefault("first_seen", datetime.now().isoformat(timespec="seconds"))

    def set_detail(self, com_id: str, text: str) -> bool:
        """把正文单独存到 data/details.json，state 里只留哈希。

        state.json 每次定时任务都会提交回仓库，塞入正文会让 git 体积迅速膨胀；
        而正文只在网页端展示，放在单独文件里更合适。
        返回正文是否发生变化。
        """
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        rec = self.data["comps"].setdefault(com_id, {})
        changed = rec.get("detail_hash") != digest
        rec["detail_hash"] = digest
        if changed:
            self._details[com_id] = text
        return changed

    def detail_of(self, com_id: str) -> str:
        return self._details.get(com_id, "")

    def load_details(self) -> None:
        p = self.path.parent / "details.json"
        if p.exists():
            try:
                self._details = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._details = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        # 只保留仍在跟踪的竞赛正文，避免文件无限增长
        live = {k: v for k, v in self._details.items() if k in self.data["comps"]}
        dp = self.path.parent / "details.json"
        dt = dp.with_suffix(".tmp")
        dt.write_text(json.dumps(live, ensure_ascii=False), encoding="utf-8")
        dt.replace(dp)

    def log_run(self, summary: dict[str, Any]) -> None:
        entry = {"at": datetime.now().isoformat(timespec="seconds"), **summary}
        self.data["runs"].append(entry)
        self.data["runs"] = self.data["runs"][-40:]  # 只留最近 40 次

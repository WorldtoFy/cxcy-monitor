"""把平台返回的详情 HTML 转成可读纯文本，并抽取日期类信息。"""

from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser

_BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}


class _Md(HTMLParser):
    """极简 HTML -> Markdown 文本转换（只保留段落、链接、表格分隔）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK:
            self.out.append("\n")
        elif tag == "a":
            self.href = dict(attrs).get("href")
            self.out.append("[")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK:
            self.out.append("\n")
        elif tag == "a":
            self.out.append(f"]({self.href})" if self.href else "]")
            self.href = None

    def handle_data(self, data: str) -> None:
        self.out.append(data)


def html_to_text(raw: str | None) -> str:
    """HTML -> 纯文本。嵌套标签（如 <strong><b>x</b></strong>）不会吞掉文字。"""
    if not raw:
        return ""
    p = _Md()
    p.feed(raw)
    t = _html.unescape("".join(p.out))
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r"\n[ \t]+", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


# 例：校赛比赛：2026年7月10日-2026年9月30日截止 / 案例作品提交时间：2026年10月15日截止
_DATE_LINE = re.compile(r"20\d{2}\s*年\s*\d{1,2}\s*月")
_DATE_VAL = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def extract_deadlines(text: str, max_items: int = 6) -> list[dict[str, str]]:
    """从正文里抽取含日期的短行，作为"子截止时间"（平台无结构化字段）。

    仅做保守提取：只取包含日期且长度合理的行，并标出首个日期。
    不猜测语义，避免给出错误日期。
    """
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in text.split("\n"):
        line = line.strip()
        if not line or len(line) > 100 or not _DATE_LINE.search(line):
            continue
        m = _DATE_VAL.search(line)
        if not m:
            continue
        iso = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        key = line[:40]
        if key in seen:
            continue
        seen.add(key)
        found.append({"text": line, "first_date": iso})
        if len(found) >= max_items:
            break
    return found


def clean_email_note(text: str) -> str:
    """平台存在文字与链接不一致的情况（如正文写 A 邮箱、链接指向 B）。

    这里不做"修正"，只在返回文本中保留原样，避免把平台录入错误当成事实传播。
    """
    return text


# ---------- 获奖公示识别 ----------
# 实测 1700 条公告里约 887 条含"获奖/公示/名单"，但其中包含开赛通知、QQ 群通知等
# 无关内容，因此采用"强关键词 或 关键词组合"的两级判定，宁可少报也不误报。
_STRONG = ("获奖名单", "获奖公示", "获奖结果", "拟获奖", "获奖情况",
           "成绩公示", "评审结果", "获奖公告", "获奖作品")
_WEAK_A = ("获奖", "奖项", "成绩", "结果")
_WEAK_B = ("公示", "名单", "公布", "公告")


def is_award_notice(title: str) -> bool:
    """判断一条大赛动态是否属于获奖公示类。"""
    t = (title or "").strip()
    if not t:
        return False
    if any(k in t for k in _STRONG):
        return True
    return any(a in t for a in _WEAK_A) and any(b in t for b in _WEAK_B)

/* 竞赛监控台 —— 移动端优先，数据来自 data/comps.json（由定时任务生成） */
(function () {
  "use strict";

  // 兼容三种访问方式：本地根目录起服务、GitHub Pages（站点根）、以及子目录部署
  var SOURCES = ["../data/comps.json", "data/comps.json", "comps.json"];
  var state = { data: null, q: "", hideExpired: true };

  var $ = function (id) { return document.getElementById(id); };
  var el = function (tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;   // textContent: 正文含 HTML 也不会被解析
    return n;
  };

  /* ---------- 工具 ---------- */
  function fmtEnd(s) { return (s || "").slice(5, 10).replace("-", "-"); }

  // 紧急度分级：与推送档位口径保持一致（<=7 / <=3 / <=1）
  function level(d) {
    if (d === null || d === undefined) return { c: "var(--gry)", txt: "待定" };
    if (d < 0) return { c: "var(--gry)", txt: "已截止" };
    if (d === 0) return { c: "var(--red)", txt: "今天截止" };
    if (d <= 1) return { c: "var(--red)", txt: d + " 天后截止" };
    if (d <= 3) return { c: "var(--org)", txt: d + " 天后截止" };
    if (d <= 7) return { c: "var(--yel)", txt: d + " 天后截止" };
    if (d <= 30) return { c: "var(--grn)", txt: d + " 天后截止" };
    return { c: "var(--mut)", txt: d + " 天后截止" };
  }

  function groupOf(d) {
    if (d === null || d === undefined) return "待定";
    if (d < 0) return "已截止";
    // 用"8 天内"而非"7 天内"：d 最大为 7，避免出现"7 天后截止"却归入"7 天内"的歧义
    if (d <= 7) return "⏰ 紧急 · 8 天内截止";
    if (d <= 30) return "📅 关注 · 31 天内截止";
    return "🗂 长期";
  }

  /* ---------- 渲染 ---------- */
  function renderKpis(comps) {
    var urgent = comps.filter(function (c) { return c.days_left !== null && c.days_left >= 0 && c.days_left <= 7; }).length;
    var soon = comps.filter(function (c) { return c.days_left !== null && c.days_left > 7 && c.days_left <= 30; }).length;
    var live = comps.filter(function (c) { return c.days_left === null || c.days_left >= 0; }).length;
    $("kpis").innerHTML = "";
    [[live, "进行中", ""], [urgent, "7 天内截止", "var(--red)"], [soon, "30 天内截止", "var(--yel)"]]
      .forEach(function (k) {
        var b = el("div", "kpi");
        var n = el("div", "n", k[0]);
        if (k[2]) n.style.color = k[2];
        b.appendChild(n);
        b.appendChild(el("div", "l", k[1]));
        $("kpis").appendChild(b);
      });
  }

  function makeCard(c) {
    var lv = level(c.days_left);
    var card = el("div", "card");
    card.appendChild(el("div", "t", c.name || "(未命名)"));

    var badge = el("span", "badge", lv.txt);
    badge.style.color = lv.c; badge.style.borderColor = lv.c;
    card.appendChild(badge);

    var m = el("div", "m");
    var a = el("span"); a.appendChild(document.createTextNode("报名 "));
    var b = el("b", null, fmtEnd(c.end) + (c.end ? " " + c.end.slice(11, 16) : ""));
    b.style.color = lv.c; a.appendChild(b); m.appendChild(a);
    if (c.deadlines && c.deadlines.length) m.appendChild(el("span", null, "含 " + c.deadlines.length + " 个子截止"));
    card.appendChild(m);

    if (c.synopsis && c.synopsis !== c.name) card.appendChild(el("div", "shot", c.synopsis));
    card.addEventListener("click", function () { openSheet(c); });
    return card;
  }

  function render() {
    var comps = state.data ? state.data.comps : [];
    renderKpis(comps);
    var q = state.q.trim().toLowerCase();

    var list = comps.filter(function (c) {
      if (state.hideExpired && c.days_left !== null && c.days_left < 0) return false;
      if (!q) return true;
      return (c.name || "").toLowerCase().indexOf(q) >= 0 ||
             (c.synopsis || "").toLowerCase().indexOf(q) >= 0 ||
             (c.detail || "").toLowerCase().indexOf(q) >= 0;
    });

    var host = $("list");
    host.innerHTML = "";
    if (!list.length) {
      host.appendChild(el("div", "empty", comps.length ? "没有匹配的竞赛" : "暂无数据"));
      return;
    }
    var cur = null;
    list.forEach(function (c) {
      var g = groupOf(c.days_left);
      if (g !== cur) { host.appendChild(el("div", "grp", g)); cur = g; }
      host.appendChild(makeCard(c));
    });
  }

  /* ---------- 详情抽屉 ---------- */
  function openSheet(c) {
    var lv = level(c.days_left);
    $("s-title").textContent = c.name || "(未命名)";
    $("s-meta").innerHTML = "";

    var rows = [
      ["报名截止", (c.end || "未知") + "（" + lv.txt + "）"],
      ["报名开始", c.start || "未知"],
      ["详情页", (c.detail ? c.detail.length : 0) + " 字正文" + (c.attachments ? " · " + c.attachments + " 个附件" : "")]
    ];
    rows.forEach(function (r) {
      var d = el("div");
      d.appendChild(document.createTextNode(r[0] + "："));
      d.appendChild(el("b", null, r[1]));
      $("s-meta").appendChild(d);
    });

    var acts = $("s-acts");
    acts.innerHTML = "";
    var go = el("a", "btn", "打开官方页面报名 →");
    go.href = c.url || "https://cxcy.upln.cn/match";
    go.target = "_blank"; go.rel = "noopener";
    acts.appendChild(go);

    var dbox = $("s-detail");
    dbox.innerHTML = "";
    if (c.deadlines && c.deadlines.length) {
      var dl = el("div", "dl");
      dl.appendChild(el("div", null, "⚠️ 正文中提取到的其他时间节点（平台无结构化字段，请以官方原文为准）"));
      c.deadlines.forEach(function (x) {
        dl.appendChild(el("div", null, "· " + x.text));
      });
      dbox.appendChild(dl);
    }
    dbox.appendChild(el("div", null, c.detail && c.detail.trim()
      ? c.detail
      : "（平台尚未上传该竞赛的详细方案正文，请点上方按钮前往官方页面查看附件。）"));

    $("sheet").hidden = false;
    document.body.style.overflow = "hidden";
  }

  function closeSheet() {
    $("sheet").hidden = true;
    document.body.style.overflow = "";
  }

  /* ---------- 启动 ---------- */
  function boot() {
    document.addEventListener("click", function (e) {
      if (e.target.hasAttribute && e.target.hasAttribute("data-close")) closeSheet();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeSheet();
    });
    $("q").addEventListener("input", function (e) { state.q = e.target.value; render(); });
    $("hideExpired").addEventListener("change", function (e) {
      state.hideExpired = e.target.checked; render();
    });

    var i = 0;
    (function tryNext() {
      if (i >= SOURCES.length) {
        $("meta").textContent = "未能加载数据文件 data/comps.json";
        $("list").appendChild(el("div", "empty", "数据加载失败：请先运行一次采集"));
        return;
      }
      fetch(SOURCES[i++] + "?t=" + Date.now())
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (j) {
          state.data = j;
          $("meta").textContent = j.count + " 个进行中竞赛 · 更新于 " + (j.generated_at || "").replace("T", " ").slice(0, 16);
          render();
        })
        .catch(tryNext);
    })();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();

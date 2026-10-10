/* 竞赛监控台 —— 移动端优先
   数据来自 data/comps.json 与 data/awards.json（由云端定时任务生成） */
(function () {
  "use strict";

  // 兼容三种访问方式：本地根目录起服务、GitHub Pages（站点根）、以及子目录部署
  var SRC_COMPS = ["../data/comps.json", "data/comps.json", "comps.json"];
  var SRC_AWARDS = ["../data/awards.json", "data/awards.json", "awards.json"];

  var state = { comps: null, awards: null, q: "", qa: "", hideExpired: true, view: "comps" };

  var $ = function (id) { return document.getElementById(id); };
  var el = function (tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;   // textContent: 正文含 HTML 也不会被解析
    return n;
  };
  var fmtSize = function (b) {
    if (!b) return "";
    return b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.round(b / 1024) + " KB";
  };

  /* ---------- 工具 ---------- */
  function fmtEnd(s) { return (s || "").slice(5, 10); }

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

  /* ---------- 竞赛视图 ---------- */
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

  function renderComps() {
    var comps = state.comps ? state.comps.comps : [];
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

  /* ---------- 获奖公示视图 ---------- */
  function renderAwards() {
    var data = state.awards;
    var host = $("alist");
    host.innerHTML = "";

    if (!data) {
      host.appendChild(el("div", "empty", "公示数据加载中…"));
      return;
    }
    var q = state.qa.trim().toLowerCase();
    var list = (data.awards || []).filter(function (a) {
      return !q || (a.name || "").toLowerCase().indexOf(q) >= 0;
    });
    if (!list.length) {
      host.appendChild(el("div", "empty", "没有匹配的公示"));
      return;
    }

    list.forEach(function (a) {
      var box = el("div", "aw");
      box.appendChild(el("div", "t", a.name || "(无标题)"));

      var m = el("div", "m", (a.time || "").slice(0, 16));
      box.appendChild(m);

      if (a.files && a.files.length) {
        var fl = el("div", "fl");
        a.files.forEach(function (f) {
          fl.appendChild(el("div", null, "📎 " + f.name + (f.size ? "  (" + fmtSize(f.size) + ")" : "")));
        });
        box.appendChild(fl);
      }

      var go = el("a", "go", "打开公告页下载附件 →");
      go.href = a.page || ("https://cxcy.upln.cn/competitionDetails?id=" + a.id);
      go.target = "_blank";
      go.rel = "noopener";
      box.appendChild(go);
      host.appendChild(box);
    });
  }

  /* ---------- 详情抽屉（竞赛用） ---------- */
  function openSheet(c) {
    var lv = level(c.days_left);
    $("s-title").textContent = c.name || "(未命名)";
    $("s-meta").innerHTML = "";

    [
      ["报名截止", (c.end || "未知") + "（" + lv.txt + "）"],
      ["报名开始", c.start || "未知"],
      ["详情页", (c.detail ? c.detail.length : 0) + " 字正文" + (c.attachments ? " · " + c.attachments + " 个附件" : "")]
    ].forEach(function (r) {
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
      c.deadlines.forEach(function (x) { dl.appendChild(el("div", null, "· " + x.text)); });
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

  /* ---------- 视图切换 ---------- */
  function setView(v) {
    state.view = v;
    $("view-comps").hidden = v !== "comps";
    $("view-awards").hidden = v !== "awards";
    Array.prototype.forEach.call($("tabs").children, function (b) {
      b.classList.toggle("on", b.getAttribute("data-view") === v);
    });
  }

  /* ---------- 启动 ---------- */
  function fetchFirst(urls) {
    var i = 0;
    return new Promise(function (resolve, reject) {
      (function next() {
        if (i >= urls.length) { reject(new Error("全部数据源失败")); return; }
        fetch(urls[i++] + "?t=" + Date.now())
          .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
          .then(resolve)
          .catch(next);
      })();
    });
  }

  function boot() {
    document.addEventListener("click", function (e) {
      if (e.target.hasAttribute && e.target.hasAttribute("data-close")) closeSheet();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeSheet();
    });
    $("q").addEventListener("input", function (e) { state.q = e.target.value; renderComps(); });
    $("qa").addEventListener("input", function (e) { state.qa = e.target.value; renderAwards(); });
    $("hideExpired").addEventListener("change", function (e) {
      state.hideExpired = e.target.checked; renderComps();
    });
    $("tabs").addEventListener("click", function (e) {
      var b = e.target.closest ? e.target.closest(".tab") : null;
      if (b) setView(b.getAttribute("data-view"));
    });

    // URL 带 ?award=<id> 时直接打开获奖公示视图（推送通知就跳这里）
    var wantAward = /[?&]award=/.test(location.search);
    if (wantAward) setView("awards");

    fetchFirst(SRC_COMPS).then(function (j) {
      state.comps = j;
      $("meta").textContent = j.count + " 个进行中竞赛 · 更新于 " +
        (j.generated_at || "").replace("T", " ").slice(0, 16);
      $("n-comps").textContent = j.count;
      renderComps();
    }).catch(function () {
      $("meta").textContent = "未能加载数据文件";
      $("list").appendChild(el("div", "empty", "数据加载失败：请先运行一次采集"));
    });

    fetchFirst(SRC_AWARDS).then(function (j) {
      state.awards = j;
      $("n-awards").textContent = j.count;
      renderAwards();
      if (wantAward) {
        var m = location.search.match(/award=([^&]+)/);
        if (m) {
          // 滚动到对应条目并高亮，避免用户还要自己找
          setTimeout(function () {
            var list = j.awards || [];
            for (var i = 0; i < list.length; i++) {
              if (list[i].id === m[1]) {
                var nodes = $("alist").children;
                if (nodes[i]) {
                  nodes[i].scrollIntoView({ block: "center" });
                  nodes[i].style.outline = "2px solid #1a7f37";
                }
                break;
              }
            }
          }, 120);
        }
      }
    }).catch(function () {
      $("n-awards").textContent = "";
      renderAwards();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();

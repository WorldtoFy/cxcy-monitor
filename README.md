# 竞赛监控台

监测**辽宁省大学生创新创业管理共享平台**（cxcy.upln.cn）：

1. **进行中竞赛**的报名截止 —— 到期推送到手机通知栏
2. **大赛动态里的获奖公示** —— 推送标题 + **可直接点击下载的附件链接**

并提供一个手机端查看页面。

## 它是怎么工作的

```
GitHub Actions（每 6 小时）
   ├─ 拉"进行中"竞赛                      → queryOngoing
   ├─ 抓竞赛方案正文 + 附件列表            → competition/detail
   ├─ 比对 state.json 找出新竞赛、算倒计时 → 命中档位则推送
   ├─ 扫"大赛动态"找获奖公示               → portalList
   │    └─ 抓公示附件 → 实时换下载票据     → queryById + fileInfo/public-access
   └─ 状态与数据提交回仓库 → GitHub Pages 自动更新网页
```

**手机不需要常驻任何程序**，电脑关着也照跑。点通知直接跳官方页面或下载附件。

## 两类推送

### ① 报名截止提醒

| 剩余天数 | 图标 | 优先级 |
|---|---|---|
| ≤ 7 天 | ⏳ | 3 |
| ≤ 3 天 | ⚠️ | 4 |
| ≤ 1 天 | 🔥 | 5 |
| 当天 / 已截止 | 🚨 / 🚫 | 5 |
| 新竞赛出现 | 🆕 | 4 |

### ② 获奖公示提醒（🏅）

推送格式：

```
🏅 获奖公示
2026第18届全国大学生广告艺术大赛（辽宁赛区）获奖公示
2026-09-15 19:17
📎 2026第18届…获奖名单.xlsx 等 2 个文件
                                     ← 点击通知 = 直接下载附件
```

**关键点：附件下载链接是"一次性票据"机制**，URL 每次请求都不同，不能长期缓存。
所以程序在**推送前实时换取新票据**（纯 HTTP，无需浏览器），保证你点的时候一定有效。

已实测验证：链接下载得到 `HTTP 200 / 408,399 bytes / xlsx(PK) 格式`，与平台元数据一致。

## 设计要点与取舍

- **每个竞赛每个档位只推一次**（`reminded` 记录在 state 里）
- 用 `<=` 而非 `==` 判定：某天任务没跑成，下一档仍会补上，不会永久漏提醒
- **截止提醒单轮最多 4 条**，**获奖公示单轮最多 3 条**（分开计配额，互不挤占），
  防止状态重建时一次涌出十几条通知
- 全部竞赛的 `createTime` 都是同一天（平台批量录入），**判新只能靠 `id`**
- 获奖公示判定用"强关键词 或 关键词组合"两级规则
  （`is_award_notice()`），因为 1700 条公告里约 887 条含"获奖/公示/名单"，
  其中夹杂开赛通知等无关内容，**宁可少报也不误报**
- **默认监测全部竞赛的公示**。若嫌吵，可在 `config.json` 里
  `watch_competitions` 填竞赛 id 只盯指定的几个

## 配置（config.json）

```jsonc
{
  "watch_competitions": [],   // 空=全部竞赛；填 id 则只监测这些（id 取详情页 comId= 后面）
  "award_notify": true,       // false = 只记录不推送
  "award_cap": 3,             // 单轮最多推几条公示
  "award_pages": 4            // 每次扫最近几页公告（每页 100 条）
}
```

> 以 `_` 开头的键是说明文字，程序会忽略。

## 本地运行

```bash
pip install -r requirements.txt

python -m cxcy.main --dry-run     # 只打印，不推送（安全）
python -m cxcy.main --seed        # 只建基线，不推送
python -m cxcy.main               # 正常跑一次（需要 NTFY_TOPIC）
python -m cxcy.main --no-awards   # 跳过获奖公示监测
```

预览网页：

```bash
python -m http.server 8901        # 然后访问 http://127.0.0.1:8901/web/
```

## 部署到云端（一次性配置）

### 1. 建仓库并推送

```bash
git init && git add -A && git commit -m "init"
gh repo create cxcy-monitor --public --source=. --push
```

> 网页要看 `data/comps.json`，GitHub Pages 在**私有仓库需要付费套餐**，
> 所以用 public。仓库里只有平台本来就公开的竞赛信息。

### 2. 配置推送主题（Repository secret）

```bash
gh secret set NTFY_TOPIC --body "你的主题名"
```

手机端：装 ntfy App（[Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
或 [GitHub Releases](https://github.com/binwiederhier/ntfy/releases)）→ 点 `+` → 输入同一主题名订阅。

> ntfy 主题名等同于密码，别外泄。想换服务器可设 `NTFY_SERVER` 变量（如自建实例）。

### 3. 开启 Pages

仓库 **Settings → Pages → Source** 选 **GitHub Actions**。

之后每 6 小时自动：抓取 → 推送提醒 → 提交状态 → 更新网页。

地址：`https://<用户名>.github.io/cxcy-monitor/`

### 4. 手动触发一次（可选）

```bash
gh workflow run 竞赛监控与提醒
```

## 几个已知的平台特性（踩过的坑）

1. **服务器用旧式 TLS 重协商**（unsafe legacy renegotiation），现代 HTTP 客户端默认拒绝。
   `cxcy/fetch.py` 里用 `OP_LEGACY_SERVER_CONNECT` 显式放行，换语言实现时要注意。
2. **竞赛列表接口必须带 `year`**，否则返回 0 条。
3. **公告列表接口必须带 `type=1`**，否则返回 0 条。
4. **`endTime` 就是报名截止时间**（详情页"报名起止日期"），`startTime` 是报名开始。
5. **正文里有更多硬期限**（材料提交截止等），平台无结构化字段，
   由 `cxcy/parse.py` 从正文按行保守提取，页面会标注"请以官方原文为准"。
6. **部分竞赛正文为空**（平台未上传），程序会降级：只显示关键字段 + 官方链接。
7. **附件下载必须用 `fileId` 参数**（不是 `id`），
   且返回的是一张**一次性票据**：`/sys/common/static?ticket=...`，不能缓存。
8. **公告 id 与竞赛 id 是两个不同的 ID**；公告通过 `competitionId` 字段关联竞赛。
9. **平台数据存在录入错误**：例如某竞赛正文写"发到 A 邮箱"但链接指向 B 邮箱。
   程序**不修正**这类数据，避免把错误当成事实传播。

## 文件结构

```
cxcy/
  fetch.py    平台 API 客户端（竞赛 / 公告 / 附件票据，含 TLS 兼容）
  parse.py    HTML → 文本、子截止时间抽取、获奖公示识别
  store.py    状态与去重（state.json + details.json 分离）
  notify.py   ntfy 推送
  main.py     主流程
config.json   订阅与限额配置
data/
  state.json    去重与提醒状态（小，会频繁提交）
  details.json  竞赛方案正文（大，单独存放）
  comps.json    供网页读取的数据
web/          手机端页面
```

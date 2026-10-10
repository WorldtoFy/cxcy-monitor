# 阿里云部署指南

> **为什么不部署在 GitHub Actions？**
> 已实测确认：GitHub Actions 的 runner（美国机房）**无法访问 `cxcy.upln.cn`**。
> - runner 本地 DNS：`SERVFAIL`
> - 公共 DNS 查询：失败
> - **硬编码 IP 直连（完全绕过 DNS）：`HTTP 000`**
>
> 第三项说明不只是 DNS 问题，**网络路径本身不通**，因此任何 DNS 绕过技巧都无效。
> 必须把采集放在**国内网络**里运行。

---

## 方案选择：FC vs ECS

| 维度 | FC（函数计算）✅ 推荐 | ECS（云服务器） |
|---|---|---|
| 匹配度 | 间歇型任务，原生时间触发器 | 为 40 秒脚本养一台 7×24 机器 |
| 维护成本 | **零**：不管系统/补丁/进程 | 需管系统更新与磁盘 |
| 状态持久化 | 无状态，需存外部（本方案存 GitHub） | 直接写本地磁盘 |
| 代码改动 | 有 `fc_handler.py`（已写好并测试） | **零改动**，现有代码直接跑 |
| 免费额度 | 按月给，用量极小长期够 | 试用期通常 1–3 个月 |

**结论：优先 FC。** 若不想动 FC 控制台，ECS 用 `deploy/ecs_setup.sh` 一键部署，零改动。

---

## 方案 A：阿里云 FC 部署步骤

### 1. 前置条件

- 一个**国内区域**的 FC 服务（如华北2-北京、华东1-杭州）
- 一个 GitHub Token（用于读写状态，见第 2 步）
- `NTFY_TOPIC`：你的 ntfy 主题名

### 2. 准备 GitHub Token

FC 是无状态的，需要把 `data/state.json` 等状态存回仓库。

生成一个 **Fine-grained PAT**（推荐，权限最小）：
1. 打开 <https://github.com/settings/personal-access-tokens/new>
2. Repository access → Only select repositories → 选 `cxcy-monitor`
3. Permissions → Repository permissions → **Contents: Read and write**
4. 有效期建议 1 年，生成后复制

> 也可用 classic token，勾 `repo` 即可，但权限过大，不推荐。

### 3. 创建函数

FC 控制台 → 函数 → 创建函数：

| 配置项 | 值 |
|---|---|
| 创建方式 | 使用示例代码 / 自定义 |
| 运行环境 | **Python 3.12** |
| 函数入口 | `cxcy.fc_handler.handler` |
| 内存 | 256 MB（足够） |
| 超时时间 | **120 秒**（单次运行约 40–60 秒，留余量） |
| 触发器 | **定时触发器**，cron 表达式：`0 0 0,6,12,18 * * *` |

### 4. 上传代码

把整个 `cxcy/` 目录和 `requirements-fc.txt` 打包上传（或用 Git 仓库方式部署）。

依赖安装：在 FC 控制台的「层」或「依赖」里按 `requirements-fc.txt` 安装，
或直接把依赖打包进 zip。

### 5. 配置环境变量

函数配置 → 环境变量：

| 变量 | 必填 | 说明 |
|---|---|---|
| `NTFY_TOPIC` | ✅ | ntfy 主题名（等同于密码，FC 会加密存储） |
| `GH_TOKEN` | ✅ | 上一步生成的 PAT |
| `GH_REPO` | ⬜ | 默认 `WorldtoFy/cxcy-monitor` |
| `SEED` | ⬜ | 首次部署设 `1`：只建基线不推送，**避免一次涌出大量通知** |
| `DRY_RUN` | ⬜ | 设 `1` 只跑逻辑不推送，用于排障 |
| `AWARD_NOTIFY` | ⬜ | `0` = 不推获奖公示 |
| `AWARD_CAP` | ⬜ | 单轮最多推几条公示，默认 3 |
| `ALERT_CAP` | ⬜ | 单轮最多推几条截止提醒，默认 4 |

### 6. 首次运行

1. 先设 `SEED=1`，手动触发一次 → 建立基线（不推送）
2. 确认日志里出现 `"ok": true` 与 `"comps": 24`
3. **删掉 `SEED`**，之后定时触发就会正常推送

### 7. 验证

- FC 日志里应看到 `{"ok": true, "comps": 24, "alerted": N, "awards": M, "saved": [...]}`
- 仓库 `data/` 下三个文件会被自动更新
- GitHub Pages 随后自动重新部署，手机网页同步更新

---

## 方案 B：阿里云 ECS 一键部署（零改动）

```bash
# SSH 登录 ECS 后执行
curl -fsSL https://raw.githubusercontent.com/WorldtoFy/cxcy-monitor/main/deploy/ecs_setup.sh -o setup.sh
sudo bash setup.sh 'cxcy-upln-你的主题名'
```

脚本会完成：装依赖 → 拉代码 → 建 venv → 写环境变量（600 权限）→
**首次运行建基线** → 安装 systemd 定时器（每 6 小时）。

**为什么不把 ntfy 主题写进代码？** 因为仓库是公开的。脚本把它写到
`/etc/cxcy-monitor.env`（权限 600），systemd 通过 `EnvironmentFile` 读取，
不会出现在仓库或日志里。

常用命令：

```bash
systemctl list-timers cxcy-monitor.timer      # 看下次运行时间
systemctl start cxcy-monitor.service          # 立即跑一次
journalctl -u cxcy-monitor.service -n 50      # 看日志
```

---

## ⚠️ 重要：代理必须绕过 upln.cn

若服务器或本机挂了代理，**必须把 `upln.cn` 加入直连/绕过列表**。

实测证据（同一台机器，同一分钟）：

| | 代理开启 | 代理关闭 |
|---|---|---|
| `cxcy.upln.cn` | ❌ 超时 | ✅ 恢复 |
| 竞赛接口 | ❌ 超时 | ✅ 24 个 |

`upln.cn` 是纯国内平台，走代理绕出国会被拒绝。**代理规则里加：**
```
*.upln.cn
upln.cn
```

---

## 平台特性备忘（踩坑记录）

1. **服务器用旧式 TLS 重协商**，现代客户端默认拒绝 → `fetch.py` 里用
   `OP_LEGACY_SERVER_CONNECT` 放行。
2. **竞赛列表接口必须带 `year`**，公告列表必须带 `type=1`，否则返回 0 条。
3. **附件下载是"一次性票据"**（`/sys/common/static?ticket=...`），
   每次请求都不同，不能缓存 → 推送前实时换取。
4. **取票据的参数名是 `fileId`**，用 `id` 会返回"该文件不是公开附件"。
5. 全部竞赛的 `createTime` 是同一天（平台批量录入），**判新只能靠 `id`**。
6. 平台存在录入错误（如正文写 A 邮箱但链接指向 B），程序**不修正**，避免传播错误信息。

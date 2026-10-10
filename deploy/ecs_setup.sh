#!/usr/bin/env bash
# 阿里云 ECS 一键部署脚本（国内节点，网络可达 cxcy.upln.cn）
#
# 用法（在 ECS 上以 root 或 sudo 执行）：
#   curl -fsSL https://raw.githubusercontent.com/WorldtoFy/cxcy-monitor/main/deploy/ecs_setup.sh -o setup.sh
#   sudo bash setup.sh 'cxcy-upln-xxxxxxxxxxxx'
#
# 注意：仓库是公开的，但 ntfy 主题等同于密码，所以通过参数传入，不写进任何文件。

set -euo pipefail

TOPIC="${1:-}"
REPO="${REPO:-https://github.com/WorldtoFy/cxcy-monitor.git}"
DIR="/opt/cxcy-monitor"

if [[ -z "$TOPIC" ]]; then
  echo "用法: sudo bash $0 '<NTFY_TOPIC>'" >&2
  echo "  NTFY_TOPIC 就是你 ntfy App 里订阅的主题名" >&2
  exit 1
fi

echo "=== [1/6] 安装依赖 ==="
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates curl
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 python3-pip git ca-certificates curl
else
  echo "未识别的包管理器，请手动安装 python3 / git" >&2
  exit 1
fi

echo "=== [2/6] 拉取代码 ==="
if [[ -d "$DIR/.git" ]]; then
  git -C "$DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$DIR"
fi
cd "$DIR"

echo "=== [3/6] 创建虚拟环境 ==="
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

echo "=== [4/6] 写入环境变量（仅 root 可读）==="
install -m 600 /dev/null /etc/cxcy-monitor.env
cat > /etc/cxcy-monitor.env <<EOF
NTFY_TOPIC=$TOPIC
EOF
chmod 600 /etc/cxcy-monitor.env
echo "  已写入 /etc/cxcy-monitor.env（权限 600）"

echo "=== [5/6] 首次运行建立基线（不推送，避免一次涌出大量通知）==="
set -a; source /etc/cxcy-monitor.env; set +a
./venv/bin/python -m cxcy.main --seed || {
  echo "⚠️  首次运行失败。常见原因：" >&2
  echo "    - 该 ECS 区域访问不到 cxcy.upln.cn" >&2
  echo "    - 安全组未放行出方向流量（默认应放行）" >&2
  exit 1
}

echo "=== [6/6] 安装 systemd 定时器（每 6 小时）==="
install -m 644 deploy/cxcy-monitor.service /etc/systemd/system/
install -m 644 deploy/cxcy-monitor.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cxcy-monitor.timer

echo
echo "=========================================="
echo "✅ 部署完成"
echo "=========================================="
echo "定时器状态："
systemctl list-timers cxcy-monitor.timer --no-pager || true
echo
echo "查看下次运行时间 / 立即手动跑一次："
echo "  systemctl start cxcy-monitor.service"
echo "  journalctl -u cxcy-monitor.service -n 50 --no-pager"
echo
echo "提示：ntfy 主题已存入 /etc/cxcy-monitor.env（权限 600），不会出现在日志或仓库里。"

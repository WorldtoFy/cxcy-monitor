#!/usr/bin/env bash
# 阿里云 FC 自动化部署（使用 Serverless Devs）
#
# 前置：
#   1. 安装并配置 Serverless Devs:  npm i -g @serverless-devs/s && s config add
#      （需要阿里云 AccessKey，建议用 RAM 子账号并只授予 FC 权限）
#   2. 导出环境变量：
#        export NTFY_TOPIC='cxcy-upln-xxxxxxxx'
#        export GH_TOKEN='github_pat_xxx'
#
# 用法：bash deploy/fc_deploy.sh
#
# 安全提示：NTFY_TOPIC 与 GH_TOKEN 只通过环境变量传入，
#          不写入任何文件、不进入代码仓库。

set -euo pipefail

REGION="${REGION:-cn-hangzhou}"
FUNC_NAME="${FUNC_NAME:-cxcy-monitor}"
SERVICE_NAME="${SERVICE_NAME:-cxcy}"

: "${NTFY_TOPIC:?请先 export NTFY_TOPIC='你的 ntfy 主题'}"
: "${GH_TOKEN:?请先 export GH_TOKEN='你的 GitHub PAT'}"

echo "=== 阿里云 FC 部署 ==="
echo "  区域     : $REGION"
echo "  服务/函数: $SERVICE_NAME / $FUNC_NAME"
echo "  ntfy 主题: ${NTFY_TOPIC:0:14}…（已隐藏）"
echo

if ! command -v s >/dev/null 2>&1; then
  echo "❌ 未找到 Serverless Devs（s）命令。请先安装：" >&2
  echo "   npm i -g @serverless-devs/s" >&2
  echo "   s config add   # 填入阿里云 AccessKey" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== 1/4 组装部署包 ==="
mkdir -p "$WORK/code"
cp -r "$ROOT/cxcy" "$WORK/code/"
cp "$ROOT/requirements-fc.txt" "$WORK/code/"
find "$WORK/code" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
echo "  $(find "$WORK/code" -type f | wc -l) 个文件"

echo "=== 2/4 生成 s.yaml ==="
cat > "$WORK/s.yaml" <<EOF
edition: 3.0.0
name: cxcy-monitor
access: default
resources:
  monitor:
    component: fc3
    props:
      region: ${REGION}
      functionName: ${FUNC_NAME}
      description: 辽宁省大学生创新创业竞赛监控与提醒
      runtime: python3.12
      code: ./code
      handler: cxcy.fc_handler.handler
      memorySize: 256
      timeout: 120
      instanceConcurrency: 1
      environmentVariables:
        NTFY_TOPIC: "${NTFY_TOPIC}"
        GH_TOKEN: "${GH_TOKEN}"
        GH_REPO: "WorldtoFy/cxcy-monitor"
        AWARD_CAP: "3"
        ALERT_CAP: "4"
      triggers:
        - triggerName: every6h
          triggerType: timer
          triggerConfig:
            cronExpression: "0 0 0,6,12,18 * * *"
            enable: true
            payload: '{}'
EOF

echo "=== 3/4 部署函数 ==="
cd "$WORK"
s deploy -y --use-local

echo
echo "=== 4/4 调用一次（首次会用 SEED 语义？不会——需手动设 SEED=1 再调）==="
echo "  提示：若这是首次部署，请先在控制台把环境变量 SEED 设为 1，"
echo "        再执行下面命令建基线（不推送），然后删掉 SEED。"
echo
echo "  手动调用命令："
echo "    s invoke -e '{}'"
echo
echo "=========================================="
echo "✅ 部署完成"
echo "=========================================="
echo "检查清单："
echo "  1. FC 控制台确认函数已创建、定时触发器已启用"
echo "  2. 环境变量 SEED=1 → s invoke -e '{}' → 建基线（应看到 seed: true, comps: 24）"
echo "  3. 删掉 SEED → s invoke -e '{}' → 正常推送"
echo "  4. 确认仓库 data/ 下三个文件被自动更新"
echo "  5. 确认手机收到 ntfy 通知"

#!/usr/bin/env bash
# 打包 FC 部署 zip（保留 cxcy/ 包目录结构，满足相对导入）
#
# 用法：bash deploy/pack_fc.sh
# 产物：dist/cxcy-monitor-fc.zip

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "=== 组装部署包 ==="
mkdir -p "$OUT" "$STAGE/cxcy"
cp "$ROOT"/cxcy/*.py "$STAGE/cxcy/"
cp "$ROOT/requirements-fc.txt" "$STAGE/"
# 去掉字节码，减小体积
find "$STAGE" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

ZIP="$OUT/cxcy-monitor-fc.zip"
rm -f "$ZIP"

if command -v zip >/dev/null 2>&1; then
  (cd "$STAGE" && zip -qr "$ZIP" .)
else
  # 无 zip 命令时用 python 打包（Windows Git Bash 常见）
  python3 - "$STAGE" "$ZIP" <<'PY'
import os, sys, zipfile
stage, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for base, _dirs, files in os.walk(stage):
        for f in files:
            full = os.path.join(base, f)
            z.write(full, os.path.relpath(full, stage))
PY
fi

echo "=== 完成 ==="
echo "  产物: $ZIP"
echo "  大小: $(du -h "$ZIP" | cut -f1)"
echo
echo "包内结构（相对导入要求 cxcy/ 作为目录存在）："
unzip -l "$ZIP" 2>/dev/null | head -20 || python3 -c "
import zipfile,sys
z=zipfile.ZipFile('$ZIP')
for n in z.namelist(): print('   ', n)
"

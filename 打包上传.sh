#!/usr/bin/env bash
# 打包成 zip，方便上传到 GitHub
cd "$(dirname "$0")"
OUT="../gd_price_collector_$(date +%Y%m%d).zip"
rm -f "$OUT"
cd ..
zip -r "$OUT" gd_price_collector \
  -x "*/__pycache__/*" "*/logs/*" "*/.git/*" "*/venv/*" "*/state/*" \
  > /dev/null
echo "已打包：$OUT"
ls -lh "$OUT"

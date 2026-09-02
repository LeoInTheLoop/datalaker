#!/bin/sh
# 数据集可达性检查。外置盘掉线是这台机器的常见故障（见 data/README.md）
cd "$(dirname "$0")/.." || exit 1
rc=0
for d in data/northwind data/olist data/exports; do
  if [ -e "$d" ] && [ -n "$(ls -A "$d" 2>/dev/null)" ]; then
    n=$(ls -1 "$d" 2>/dev/null | wc -l | tr -d ' ')
    printf "  ✅ %-16s %s 个文件\n" "$d" "$n"
  elif [ -L "$d" ]; then
    printf "  ❌ %-16s 符号链接已断——外置盘可能掉线，插回即可\n" "$d"; rc=1
  else
    printf "  ⚠️  %-16s 不存在或为空\n" "$d"
  fi
done
exit $rc

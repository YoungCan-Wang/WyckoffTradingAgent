#!/usr/bin/env bash
# 把周期代号解析成起止日期，写入 GITHUB_ENV。取快照和跑回测的 job 必须落在同一区间，
# 否则回测会读到覆盖范围不足的快照，因此共用这一份定义。
set -euo pipefail

PERIOD_KEY="${1:?缺少周期代号}"

case "$PERIOD_KEY" in
  bull_2020) START=2020-07-01; END=2021-02-18 ;;
  bear_2022) START=2021-12-13; END=2022-10-31 ;;
  sideways_2023) START=2023-01-03; END=2023-12-29 ;;
  volatile_2024) START=2024-01-02; END=2024-12-31 ;;
  recent_2m)
    END=$(date -d '-1 day' +%Y-%m-%d)
    START=$(date -d '-2 months -1 day' +%Y-%m-%d) ;;
  recent_6m)
    END=$(date -d '-1 day' +%Y-%m-%d)
    START=$(date -d '-6 months -1 day' +%Y-%m-%d) ;;
  *) echo "未知周期: ${PERIOD_KEY}" >&2; exit 1 ;;
esac

{
  echo "BT_START=${START}"
  echo "BT_END=${END}"
  echo "BT_PERIOD_KEY=${PERIOD_KEY}"
} >> "${GITHUB_ENV:?需要在 GitHub Actions 环境中运行}"

echo "[period] ${PERIOD_KEY}: ${START} ~ ${END}"

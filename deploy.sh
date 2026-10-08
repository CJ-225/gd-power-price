#!/usr/bin/env bash
# ============================================================
# 广东电价数据采集 —— 云服务器一键部署
# 适用：Ubuntu 22.04 / Debian 12
# 用法：sudo bash deploy.sh
# ============================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "==> 部署目录：$APP_DIR"

# ---------- 1. 系统依赖 ----------
echo "==> 安装系统依赖"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv sqlite3 \
    fonts-wqy-zenhei 2>/dev/null || true

# ---------- 2. Python 虚拟环境 ----------
echo "==> 创建虚拟环境"
cd "$APP_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q playwright requests

# ---------- 3. 浏览器内核 ----------
echo "==> 安装 Chromium（约 150MB，首次较慢）"
playwright install chromium
playwright install-deps chromium 2>/dev/null || true

# ---------- 4. 环境变量 ----------
if [ ! -f .env ]; then
    cp .env.example .env
    echo "==> 已生成 .env，请按需修改（气象采集无需填写账号）"
fi

# ---------- 5. 自检 ----------
echo "==> 自检：采集气象 + 行情"
python3 src/collector.py --only weather && \
python3 src/collector.py --only price || {
    echo "!! 自检未完全通过，请检查网络与依赖"; }

# ---------- 6. 定时任务 ----------
echo "==> 配置 crontab"
PY="$APP_DIR/.venv/bin/python"
CRON_LINES="
# 广东电价数据采集（北京时间）
# 气象：每日 06:10 拉取历史+7日预报
10 6 * * * cd $APP_DIR && $PY src/collector.py --only weather >> logs/cron.log 2>&1
# 行情：每日 09:10 / 14:10 / 23:10 三次，覆盖不同披露时点
10 9 * * * cd $APP_DIR && $PY src/collector.py --only price --export >> logs/cron.log 2>&1
10 14 * * * cd $APP_DIR && $PY src/collector.py --only price >> logs/cron.log 2>&1
10 23 * * * cd $APP_DIR && $PY src/collector.py --only price --export >> logs/cron.log 2>&1
# 中长期：每月 1 日
30 7 1 * * cd $APP_DIR && $PY src/collector.py --only midlong >> logs/cron.log 2>&1
"
# 去重写入
( crontab -l 2>/dev/null | grep -v "gd_price_collector"; echo "$CRON_LINES" ) | crontab -

echo ""
echo "=========================================="
echo " 部署完成"
echo "=========================================="
echo " 目录   : $APP_DIR"
echo " 虚拟环境: $APP_DIR/.venv"
echo " 数据库 : $APP_DIR/data/gd_power.sqlite"
echo " 导出   : $APP_DIR/output/"
echo ""
echo " 手动执行:"
echo "   source .venv/bin/activate"
echo "   python src/collector.py --export"
echo ""
echo " 查看定时任务: crontab -l"
echo " 查看日志    : tail -f logs/cron.log"
echo ""
echo " 注：若服务器时区非北京时间，请先执行"
echo "     sudo timedatectl set-timezone Asia/Shanghai"
echo "=========================================="

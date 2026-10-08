#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
广东电力交易中心 —— 市场行情数据采集器（实测接口版）
================================================================
接口已实测验证（2026-10-08），全部为免登录可访问的「公众信息」层数据。

已确认的真实接口
----------------------------------------------------------------
[1] 现货价格地区分布 + 逐时价格
    GET /portal/xhr/sso-portal/portal/home/marketEntitiesNum
    返回：
      data.currentPirceAreaVOList[]  -> 21个地市 × type(1=高峰/2=低谷) 时刻节点电价
      data.currentPirceVO.details[]  -> 24点逐时价格(currentPrice, dateTime)

[2] 中长期年度交易
    GET /portal/xhr/sso-disclosure/market/data/marketYearTradeList
    返回：yearTradeList[] -> 成交电量/月均价/发电成交数/用电成交数

[3] 资讯公告分类
    GET /portal/xhr/sso-portal/portal/notice/getType

[4] 注册须知
    GET /portal/xhr/sso-company/protocol/detail/serviceAgreement

关键机制说明
----------------------------------------------------------------
- 网关对「未注册路由」统一返回 400（空body），对合法路由返回结构化 JSON。
  因此不能靠猜路径，必须从 JS 包的路由表反推。
- URL 上必须带 `HdKGccbL=<token>` 查询参数（WAF 反爬签名，每次不同）。
  该 token 由页面 JS 生成，直接构造困难 —— 所以采用浏览器拦截方案：
  让真实 Chromium 加载页面，监听 XHR 响应，直接取后端 JSON。
- 所有接口前缀为 /portal/xhr/ ，后端微服务名体现为 sso-portal / sso-disclosure。

用法
----------------------------------------------------------------
    python collector.py                    # 采集全部
    python collector.py --date 2026-10-07  # 指定日期（归档用）
    python collector.py --headed           # 显示浏览器（调试）
    python collector.py --export csv       # 额外导出 CSV
"""

import argparse
import csv
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portal import PortalSession, snooze  # noqa: E402

import requests  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
OUT_DIR = BASE_DIR / "output"
for d in (DATA_DIR, LOG_DIR, OUT_DIR):
    d.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "gd_power.sqlite"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"collect_{date.today():%Y%m}.log",
                            encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("gd-collector")

# ------------------------------------------------------------------
# 建表
# ------------------------------------------------------------------
DDL = """
CREATE TABLE IF NOT EXISTS fact_spot_hourly (
  trade_date TEXT, hour INTEGER, price REAL, price_type TEXT DEFAULT '日前',
  collected_at TEXT, PRIMARY KEY (trade_date, hour, price_type));

CREATE TABLE IF NOT EXISTS fact_city_peakvalley (
  trade_date TEXT, price_type TEXT DEFAULT '日前', city_name TEXT,
  peak_price REAL, valley_price REAL, spread REAL, collected_at TEXT,
  PRIMARY KEY (trade_date, price_type, city_name));

CREATE TABLE IF NOT EXISTS fact_daily_price (
  trade_date TEXT, price_type TEXT, avg_price REAL, max_price REAL,
  min_price REAL, peak_avg REAL, valley_avg REAL, neg_hours INTEGER,
  collected_at TEXT, PRIMARY KEY (trade_date, price_type));

CREATE TABLE IF NOT EXISTS fact_midlong_year (
  data_year TEXT, trade_type INTEGER, trade_name TEXT,
  power_trade_count INTEGER, retail_trade_count INTEGER,
  trade_power REAL, month_avg_price REAL, collected_at TEXT,
  PRIMARY KEY (data_year, trade_type));

CREATE TABLE IF NOT EXISTS fact_weather (
  ts TEXT, site TEXT, latitude REAL, longitude REAL,
  wind_speed_100m REAL, wind_dir_100m REAL, shortwave_rad REAL,
  temperature_2m REAL, is_forecast INTEGER, collected_at TEXT,
  PRIMARY KEY (ts, site, is_forecast));

CREATE TABLE IF NOT EXISTS raw_snapshot (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, api TEXT,
  url TEXT, payload TEXT);

CREATE TABLE IF NOT EXISTS collect_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, task_name TEXT,
  trade_date TEXT, status TEXT, rows INTEGER, message TEXT);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(DDL)
    return conn


def log_row(conn, task, tdate, status, rows, msg=""):
    conn.execute("INSERT INTO collect_log(run_at,task_name,trade_date,"
                 "status,rows,message) VALUES(?,?,?,?,?,?)",
                 (datetime.now().isoformat(timespec="seconds"), task,
                  str(tdate) if tdate else None, status, rows, msg[:1500]))
    conn.commit()


def save_raw(conn, api, url, payload):
    conn.execute("INSERT INTO raw_snapshot(run_at,api,url,payload)"
                 " VALUES(?,?,?,?)",
                 (datetime.now().isoformat(timespec="seconds"), api,
                  url, json.dumps(payload, ensure_ascii=False)
                  if not isinstance(payload, str) else payload))
    conn.commit()


# ------------------------------------------------------------------
# 任务 1：现货逐时价格 + 地市峰谷价
# ------------------------------------------------------------------
MARKET_API = "sso-portal/portal/home/marketEntitiesNum"


def task_spot_price(sess: PortalSession, conn):
    """访问交易行情页，捕获 marketEntitiesNum 接口"""
    task = "spot_price"
    sess.grab(
        "https://pm.gd.csg.cn/portal/#/home/marketOperateOrg/publicInfo/transaction",
        watch=[MARKET_API],
        wait=15,
    )
    hits = sess.take(MARKET_API)
    if not hits:
        log_row(conn, task, None, "fail", 0, "未捕获接口")
        return 0, 0

    hourly_rows, city_rows = 0, 0
    # 合并多次同接口响应（页面会调多次，分别带 area 和 details）
    area_list, details = None, None
    for url, body in hits:
        save_raw(conn, MARKET_API, url, body)
        d = (body or {}).get("data") or {}
        if d.get("currentPirceAreaVOList"):
            area_list = d["currentPirceAreaVOList"]
        if d.get("currentPirceVO", {}).get("details"):
            details = d["currentPirceVO"]["details"]

    now = datetime.now().isoformat(timespec="seconds")

    # ---- 逐时价格 ----
    if details:
        recs = []
        for item in details:
            ts = item.get("dateTime", "")
            m = re.match(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}):", ts)
            if not m:
                continue
            tdate, hh = m.group(1), int(m.group(2))
            p = item.get("currentPrice")
            if p is None:
                continue
            recs.append((tdate, hh, float(p), "日前", now))
        if recs:
            conn.executemany(
                "INSERT OR REPLACE INTO fact_spot_hourly"
                "(trade_date,hour,price,price_type,collected_at)"
                " VALUES(?,?,?,?,?)", recs)
            conn.commit()
            hourly_rows = len(recs)
            log.info("逐时价格 %d 条", hourly_rows)

            # ---- 汇总为日度指标 ----
            by_date = {}
            for td, hh, p, _, _ in recs:
                by_date.setdefault(td, []).append((hh, p))
            sumrecs = []
            for td, arr in by_date.items():
                vals = [p for _, p in arr]
                peak = [p for h, p in arr if 8 <= h < 12 or 17 <= h < 22]
                valley = [p for h, p in arr if h < 7 or 12 <= h < 14]
                sumrecs.append((
                    td, "日前",
                    round(sum(vals) / len(vals), 4), max(vals), min(vals),
                    round(sum(peak) / len(peak), 4) if peak else None,
                    round(sum(valley) / len(valley), 4) if valley else None,
                    sum(1 for v in vals if v < 0), now,
                ))
            conn.executemany(
                "INSERT OR REPLACE INTO fact_daily_price"
                "(trade_date,price_type,avg_price,max_price,min_price,"
                "peak_avg,valley_avg,neg_hours,collected_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)", sumrecs)
            conn.commit()
            log.info("日度汇总 %d 条", len(sumrecs))

    # ---- 地市峰谷 ----
    if area_list:
        by_city = {}
        for item in area_list:
            name, val, typ = item.get("name"), item.get("value"), item.get("type")
            if not name or val is None:
                continue
            by_city.setdefault(name, {})[typ] = float(val)
        # 归属日期取 details 最新日期，否则取今天
        tdate = (details[-1]["dateTime"][:10] if details
                 else date.today().isoformat())
        recs = []
        for city, d in by_city.items():
            pk, vk = d.get(1), d.get(2)
            recs.append((tdate, "日前", city, pk, vk,
                         (pk - vk) if (pk is not None and vk is not None) else None,
                         now))
        if recs:
            conn.executemany(
                "INSERT OR REPLACE INTO fact_city_peakvalley"
                "(trade_date,price_type,city_name,peak_price,valley_price,"
                "spread,collected_at) VALUES(?,?,?,?,?,?,?)", recs)
            conn.commit()
            city_rows = len(recs)
            log.info("地市峰谷 %d 条（%s）", city_rows, tdate)

    log_row(conn, task, None, "success", hourly_rows + city_rows,
            f"hourly={hourly_rows}, city={city_rows}")
    return hourly_rows, city_rows


# ------------------------------------------------------------------
# 任务 2：中长期年度交易
# ------------------------------------------------------------------
YEAR_API = "sso-disclosure/market/data/marketYearTradeList"
TRADE_TYPE_NAME = {
    1: "年度协商交易", 2: "年度集中交易",
    3: "年度挂牌交易", 4: "年度交易情况总表",
}


def task_midlong_year(sess: PortalSession, conn):
    task = "midlong_year"
    sess.grab(
        "https://pm.gd.csg.cn/portal/#/home/marketOperateOrg/publicInfo/marketData",
        watch=[YEAR_API], wait=12,
    )
    hits = sess.take(YEAR_API)
    if not hits:
        log_row(conn, task, None, "fail", 0, "未捕获接口")
        return 0

    now = datetime.now().isoformat(timespec="seconds")
    recs = []
    for url, body in hits:
        save_raw(conn, YEAR_API, url, body)
        lst = ((body or {}).get("data") or {}).get("yearTradeList") or []
        for it in lst:
            ct = it.get("createTime") or ""
            yr = ct[:4] if ct[:4].isdigit() else str(date.today().year)
            recs.append((
                yr, it.get("type"),
                TRADE_TYPE_NAME.get(it.get("type"), f"type{it.get('type')}"),
                it.get("powerTradeCount"), it.get("retailTradeCount"),
                it.get("tradePower"), it.get("monthAvgPrice"), now,
            ))
    if recs:
        conn.executemany(
            "INSERT OR REPLACE INTO fact_midlong_year"
            "(data_year,trade_type,trade_name,power_trade_count,"
            "retail_trade_count,trade_power,month_avg_price,collected_at)"
            " VALUES(?,?,?,?,?,?,?,?)", recs)
        conn.commit()
    log_row(conn, task, None, "success" if recs else "partial", len(recs))
    log.info("中长期年度交易 %d 条", len(recs))
    return len(recs)


# ------------------------------------------------------------------
# 任务 3：气象（Open-Meteo，纯 requests，无需浏览器）
# ------------------------------------------------------------------
SITES = {
    "yangjiang_offshore": (21.75, 112.20),
    "shantou_offshore": (23.30, 117.00),
    "zhanjiang": (20.90, 110.60),
    "shaoguan": (24.80, 113.60),
    "guangzhou": (23.13, 113.26),
}


def task_weather(conn):
    task = "weather"
    now = datetime.now().isoformat(timespec="seconds")
    total = 0
    today = date.today()
    hourly_vars = ("wind_speed_100m,wind_direction_100m,"
                   "shortwave_radiation,temperature_2m")

    for site, (lat, lon) in SITES.items():
        jobs = [
            ("https://archive-api.open-meteo.com/v1/archive",
             {"latitude": lat, "longitude": lon,
              "start_date": (today - timedelta(days=7)).isoformat(),
              "end_date": today.isoformat(),
              "hourly": hourly_vars, "timezone": "Asia/Shanghai"}, 0),
            ("https://api.open-meteo.com/v1/forecast",
             {"latitude": lat, "longitude": lon, "hourly": hourly_vars,
              "timezone": "Asia/Shanghai", "forecast_days": 7}, 1),
        ]
        for url, params, is_fc in jobs:
            try:
                r = requests.get(url, params=params, timeout=30)
                r.raise_for_status()
                h = r.json().get("hourly", {})
            except Exception as e:
                log.error("气象 %s 失败: %s", site, e)
                continue
            rows = []
            n = len(h.get("time", []))
            for i in range(n):
                rows.append((
                    h["time"][i].replace("T", " "), site, lat, lon,
                    h.get("wind_speed_100m", [None] * n)[i],
                    h.get("wind_direction_100m", [None] * n)[i],
                    h.get("shortwave_radiation", [None] * n)[i],
                    h.get("temperature_2m", [None] * n)[i],
                    is_fc, now,
                ))
            conn.executemany(
                "INSERT OR REPLACE INTO fact_weather"
                "(ts,site,latitude,longitude,wind_speed_100m,wind_dir_100m,"
                "shortwave_rad,temperature_2m,is_forecast,collected_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            total += len(rows)
            log.info("气象 %s %s -> %d 行", site,
                     "预报" if is_fc else "实测", len(rows))
            snooze()
    log_row(conn, task, today, "success", total)
    return total


# ------------------------------------------------------------------
# 导出
# ------------------------------------------------------------------
def export_csv(conn):
    ts = datetime.now().strftime("%Y%m%d")
    tables = ["fact_spot_hourly", "fact_city_peakvalley",
              "fact_daily_price", "fact_midlong_year", "fact_weather"]
    for t in tables:
        try:
            cur = conn.execute(f"SELECT * FROM {t}")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            if not rows:
                continue
            p = OUT_DIR / f"{t}_{ts}.csv"
            with open(p, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(cols)
                w.writerows(rows)
            log.info("导出 %s (%d 行)", p.name, len(rows))
        except Exception as e:
            log.warning("导出 %s 失败: %s", t, e)


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="广东电力市场行情采集器")
    ap.add_argument("--only", default="all",
                    choices=["all", "price", "midlong", "weather"])
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--export", action="store_true", help="额外导出CSV")
    args = ap.parse_args()

    conn = get_db()
    log.info("===== 采集开始 %s =====",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    if args.only in ("all", "weather"):
        task_weather(conn)

    need_browser = args.only in ("all", "price", "midlong")
    if need_browser:
        try:
            with PortalSession(headless=not args.headed) as sess:
                if args.only in ("all", "price"):
                    task_spot_price(sess, conn)
                    snooze()
                if args.only in ("all", "midlong"):
                    task_midlong_year(sess, conn)
        except Exception as e:
            log.exception("浏览器任务失败")
            log_row(conn, "browser", None, "fail", 0, str(e))

    if args.export:
        export_csv(conn)

    conn.close()
    log.info("===== 采集结束 =====")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PortalSession —— 广东电力交易中心门户会话封装
============================================================
为什么需要它：
  门户是 Umi/React 单页应用，所有数据走 /portal/xhr/... 接口。
  网关对未注册路由返回 400（空 body），且 URL 必须带 WAF 签名参数
  `HdKGccbL=<每次不同的token>`。该 token 由页面 JS 动态生成，外部
  无法稳定构造 —— 因此用「真实浏览器 + 响应拦截」方案：
     1. 启动 Chromium，加载目标页面
     2. 监听所有 XHR 响应
     3. 按接口关键字筛选，直接取后端 JSON
  好处：不依赖 HTML 解析（页面是前端渲染的），拿到的是原始数据。
"""

import json
import logging
import os
import random
import re
import time
from pathlib import Path

log = logging.getLogger("gd-collector.portal")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

MIN_DELAY = float(os.getenv("MIN_DELAY", 2.0))
MAX_DELAY = float(os.getenv("MAX_DELAY", 6.0))


def snooze():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


class PortalSession:
    """浏览器会话 + 接口捕获"""

    def __init__(self, headless=True, storage_state=None):
        self.headless = headless
        self.storage_state = storage_state
        self._bucket = []          # [(url, json_body)]
        self._watch = []           # 当前监听的接口关键字

    # ---------------- 生命周期 ----------------
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox",
                  "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
        )
        kw = {
            "user_agent": UA,
            "viewport": {"width": 1600, "height": 900},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
        }
        if self.storage_state and Path(self.storage_state).exists():
            kw["storage_state"] = self.storage_state
            log.info("已加载登录态 %s", self.storage_state)
        self.ctx = self.browser.new_context(**kw)
        self.page = self.ctx.new_page()
        self.page.on("response", self._on_response)
        return self

    def __exit__(self, *a):
        try:
            self.ctx.close()
            self.browser.close()
            self._pw.stop()
        except Exception:
            pass

    # ---------------- 响应拦截 ----------------
    def _on_response(self, resp):
        try:
            if "/xhr/" not in resp.url:
                return
            # 只收 JSON
            ct = (resp.headers or {}).get("content-type", "").lower()
            if "json" not in ct:
                return
            body = resp.json()
            self._bucket.append((resp.url, body))
        except Exception:
            pass

    # ---------------- 操作 ----------------
    def grab(self, url, watch=None, wait=12):
        """
        打开页面并等待接口返回。
        watch: 接口关键字列表，命中则提前结束等待。
        """
        self._watch = watch or []
        self._bucket = []
        log.info("打开 %s", url)
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # 轮询等待目标接口出现（最多 wait 秒）
        deadline = time.time() + wait
        while time.time() < deadline:
            if self._watch and any(
                any(k in u for k in self._watch) and b
                for u, b in self._bucket
            ):
                break
            self.page.wait_for_timeout(800)
        self.page.wait_for_timeout(1500)
        snooze()

    def take(self, keyword):
        """取出包含关键字的响应，去掉鉴权参数后去重"""
        out, seen = [], set()
        for url, body in self._bucket:
            if keyword not in url:
                continue
            base = url.split("?")[0]
            key = (base, json.dumps(body, sort_keys=True)[:200]
                   if isinstance(body, (dict, list)) else str(body)[:200])
            if key in seen:
                continue
            seen.add(key)
            out.append((url, body))
        return out

    def shot(self, name, outdir="logs"):
        p = Path(outdir) / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.png"
        p.parent.mkdir(exist_ok=True, parents=True)
        try:
            self.page.screenshot(path=str(p), full_page=True)
            log.info("截图 -> %s", p)
        except Exception as e:
            log.debug("截图失败 %s", e)
        return p

    def state_save(self, path):
        self.ctx.storage_state(path=path)
        log.info("登录态已保存 %s", path)

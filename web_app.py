# -*- coding: utf-8 -*-
"""
Local web console for the NetEase Music partner scorer.

Usage:
  python web_app.py
  python web_app.py --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import argparse
import collections
import hmac
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from music_partner import LOG_FILE, MusicPartner, redact_sensitive


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "copartner_ck.json"
RUN_LOCK = threading.Lock()
RUN_PROCESS: subprocess.Popen | None = None
STATUS_CACHE_TTL = 45.0
STATUS_CACHE_LOCK = threading.Lock()
STATUS_CACHE: dict[str, tuple[float, dict]] = {}


def read_config(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def mask_text(value: str, head: int = 4, tail: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= head + tail:
        return "*" * len(value)
    return f"{value[:head]}...{value[-tail:]}"


def tail_text(path: str | Path, max_lines: int = 160) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = collections.deque(f, maxlen=max_lines)
        sanitized = []
        for line in lines:
            clean = redact_sensitive(line)
            clean = re.sub(r"(?i)\b[a-z]:[\\/][^\r\n]+", "<path>", clean)
            clean = re.sub(r"\\\\[^\\\r\n]+\\[^\r\n]+", "<path>", clean)
            clean = re.sub(r"(?<!:)\/(?:[^\s/]+\/)+[^\s]+", "<path>", clean)
            if "Traceback (most recent call last)" in clean or re.match(r"\s*File ['\"]", clean):
                clean = "<exception details omitted>"
            clean = re.sub(
                r"(?i)(\b(?:[A-Za-z_][\w.]*Error|Exception):\s*).*$",
                r"\1<details omitted>",
                clean,
            )
            clean = clean.rstrip("\r\n")[:240]
            sanitized.append(clean + "\n")
        return "".join(sanitized)
    except FileNotFoundError:
        return ""


def process_state() -> dict:
    global RUN_PROCESS
    if RUN_PROCESS is None:
        return {"running": False, "pid": None, "returncode": None}
    returncode = RUN_PROCESS.poll()
    return {
        "running": returncode is None,
        "pid": RUN_PROCESS.pid,
        "returncode": returncode,
    }


def build_account_status(account: dict, index: int) -> dict:
    cookie = account.get("cookie", "")
    partner = MusicPartner(cookie, account.get("delay"), quiet=True)
    user_name = partner._get_user_name()
    task = partner.fetch_task()
    extra_works = partner.fetch_extra_works()
    record = partner.fetch_today_record()

    score_limit = task.get("dailyTaskScoreLimit") or {}
    basic_score = score_limit.get("dailyBasicTaskScore", 8)
    extend_score = score_limit.get("dailyMaxExtendEvaluateScore", 15)
    evaluate_target = basic_score + extend_score

    works = task.get("works") or []
    basic_target = len(works) or 5
    basic_done = min(task.get("completedCount", 0), basic_target)
    basic_integral = task.get("integral", 0)

    list_total = len(extra_works)
    list_completed = sum(1 for item in extra_works if item.get("completed"))

    record_complete_count = record.get("completeCount", 0) if record else 0
    record_integral = record.get("taskIntegral") if record else None
    record_extra_done = max(0, min(extend_score, record_complete_count - basic_target))
    record_completed = bool(record and record.get("taskCompleted") and record_extra_done >= extend_score)

    if record_extra_done:
        extra_done = record_extra_done
        progress_source = "record"
    else:
        extra_done = list_completed
        progress_source = "list"

    strategy = "ready"
    if record_completed or partner._is_extra_no_more_today():
        strategy = "completed"
    elif extra_done >= extend_score:
        strategy = "completed"

    return {
        "index": index,
        "user_name": user_name,
        "basic": {
            "done": basic_done,
            "target": basic_target,
            "integral": basic_integral,
            "completed": basic_done >= basic_target,
        },
        "extra": {
            "done": extra_done,
            "target": extend_score,
            "progress_source": progress_source,
            "list_total": list_total,
            "list_completed": list_completed,
            "list_residue": max(0, list_total - list_completed),
            "completed": strategy == "completed",
        },
        "score": {
            "basic": basic_score,
            "extend": extend_score,
            "evaluate_target": evaluate_target,
            "record_integral": record_integral,
            "record_complete_count": record_complete_count,
        },
        "strategy": strategy,
    }


def _build_account_summaries(config_path: Path) -> list[dict]:
    config = read_config(config_path)
    accounts = config.get("MUSIC_COPARTNER") or []
    summaries = []
    for i, account in enumerate(accounts, 1):
        try:
            summaries.append(build_account_status(account, i))
        except Exception as exc:
            summaries.append(
                {
                    "index": i,
                    "error": "账号状态暂不可用",
                    "strategy": "error",
                }
            )
    return summaries


def clear_status_cache() -> None:
    with STATUS_CACHE_LOCK:
        STATUS_CACHE.clear()


def build_status(config_path: Path, ttl: float = STATUS_CACHE_TTL) -> dict:
    key = str(config_path.resolve())
    now = time.monotonic()
    with STATUS_CACHE_LOCK:
        cached = STATUS_CACHE.get(key)
        if cached and now - cached[0] < ttl:
            summaries = cached[1]["accounts"]
        else:
            summaries = _build_account_summaries(config_path)
            STATUS_CACHE[key] = (now, {"accounts": summaries})
    return {
        "process": process_state(),
        "accounts": summaries,
        "config_name": config_path.name,
        "log_name": Path(LOG_FILE).name,
    }


def start_runner(config_path: Path) -> dict:
    global RUN_PROCESS
    with RUN_LOCK:
        state = process_state()
        if state["running"]:
            return {"started": False, **state}

        cmd = [sys.executable, str(ROOT / "music_partner.py"), "--config", str(config_path)]
        kwargs = {
            "cwd": str(ROOT),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        RUN_PROCESS = subprocess.Popen(cmd, **kwargs)
        return {"started": True, **process_state()}


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>网易云音乐合伙人控制台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f8;
      --panel: #ffffff;
      --text: #17202a;
      --muted: #667085;
      --line: #d9dee7;
      --accent: #c91d2e;
      --accent-dark: #a91424;
      --ok: #138a43;
      --warn: #b7791f;
      --bad: #b42318;
      --mono: Consolas, "SFMono-Regular", Menlo, monospace;
      --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: var(--sans);
      font-size: 14px;
    }
    header {
      height: 56px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 10;
    }
    h1 { font-size: 18px; margin: 0; font-weight: 650; letter-spacing: 0; }
    main { max-width: 1180px; margin: 0 auto; padding: 20px 24px 28px; }
    button {
      appearance: none;
      border: 1px solid var(--accent);
      background: var(--accent);
      color: white;
      border-radius: 6px;
      padding: 8px 14px;
      font-weight: 650;
      cursor: pointer;
    }
    button:hover { background: var(--accent-dark); border-color: var(--accent-dark); }
    button.secondary {
      background: white;
      color: var(--text);
      border-color: var(--line);
    }
    button.secondary:hover { background: #f2f4f7; border-color: #c8ced8; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .toolbar { display: flex; gap: 10px; align-items: center; }
    .grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-bottom: 16px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    section h2 {
      font-size: 15px;
      margin: 0 0 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .muted { color: var(--muted); }
    .status-line { display: flex; gap: 12px; flex-wrap: wrap; margin: 8px 0; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 10px;
      background: #fbfcfd;
      white-space: nowrap;
    }
    .pill.ok { color: var(--ok); border-color: #b7e4c7; background: #f0fbf4; }
    .pill.warn { color: var(--warn); border-color: #f5d08a; background: #fff8e8; }
    .pill.bad { color: var(--bad); border-color: #f2b8b5; background: #fff1f0; }
    .metric-row {
      display: grid;
      grid-template-columns: 150px 1fr auto;
      gap: 12px;
      align-items: center;
      margin: 12px 0;
    }
    .bar {
      height: 10px;
      background: #edf0f4;
      border-radius: 999px;
      overflow: hidden;
      border: 1px solid #e2e7ef;
    }
    .bar > span { display: block; height: 100%; background: var(--accent); }
    .score {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 12px;
    }
    .score div {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fbfcfd;
    }
    .score strong { display: block; font-size: 20px; margin-bottom: 3px; }
    pre {
      margin: 0;
      height: 420px;
      overflow: auto;
      background: #111827;
      color: #d1d5db;
      border-radius: 8px;
      padding: 14px;
      font-family: var(--mono);
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    .full { grid-column: 1 / -1; }
    .account + .account { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 14px; }
    .notice { color: var(--muted); line-height: 1.7; }
    @media (max-width: 860px) {
      header { padding: 0 14px; }
      main { padding: 14px; }
      .grid { grid-template-columns: 1fr; }
      .score { grid-template-columns: 1fr 1fr; }
      .metric-row { grid-template-columns: 1fr; gap: 6px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>网易云音乐合伙人控制台</h1>
    <div class="toolbar">
      <button class="secondary" id="refreshBtn">刷新</button>
      <button id="runBtn">立即运行</button>
    </div>
  </header>
  <main>
    <div class="grid">
      <section>
        <h2>运行状态 <span class="muted" id="updatedAt"></span></h2>
        <div class="status-line" id="processState"></div>
        <p class="notice" id="paths"></p>
      </section>
      <section>
        <h2>策略说明</h2>
        <p class="notice">真实完成进度优先读取评定记录接口；拓展展示列表只用于排队提交，不作为完成口径。遇到服务端“今日没有更多可评定歌曲”后，当天会跳过残留展示项。</p>
      </section>
      <section class="full">
        <h2>帐号状态</h2>
        <div id="accounts"></div>
      </section>
      <section class="full">
        <h2>日志</h2>
        <pre id="logs"></pre>
      </section>
    </div>
  </main>
  <script>
    const ADMIN_TOKEN = __ADMIN_TOKEN__;
    const $ = (id) => document.getElementById(id);
    const pct = (done, total) => total > 0 ? Math.max(0, Math.min(100, Math.round(done * 100 / total))) : 0;
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));

    async function api(path, options) {
      const res = await fetch(path, options);
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }

    function renderProcess(process) {
      const cls = process.running ? 'warn' : 'ok';
      const text = process.running ? `运行中 PID ${process.pid}` : '空闲';
      $('processState').innerHTML = `<span class="pill ${cls}">${esc(text)}</span>`;
      $('runBtn').disabled = !!process.running;
    }

    function renderAccounts(accounts) {
      if (!accounts.length) {
        $('accounts').innerHTML = '<p class="notice">没有配置帐号。</p>';
        return;
      }
      $('accounts').innerHTML = accounts.map(account => {
        if (account.error) {
          return `<div class="account"><div class="pill bad">帐号 ${account.index}: ${esc(account.error)}</div></div>`;
        }
        const b = account.basic;
        const e = account.extra;
        const s = account.score;
        const strategyText = account.strategy === 'completed' ? '今日评定已完成' : account.strategy === 'ready' ? '可运行' : account.strategy;
        const strategyClass = account.strategy === 'completed' ? 'ok' : 'warn';
        const totalIntegral = s.record_integral == null ? '-' : s.record_integral;
        const listNote = e.progress_source === 'record' && e.completed
          ? `展示列表仍返回 ${e.list_total} 项；真实进度以评定记录接口为准，展示残留已忽略`
          : `展示列表标记已评 ${e.list_completed}/${e.list_total}；真实拓展进度来源：${e.progress_source === 'record' ? '评定记录接口' : '展示列表回退'}`;
        return `
          <div class="account">
            <div class="status-line">
              <span class="pill ok">${esc(account.user_name)}</span>
              <span class="pill ${strategyClass}">${esc(strategyText)}</span>
            </div>
            <div class="metric-row">
              <strong>基础任务</strong>
              <div class="bar"><span style="width:${pct(b.done, b.target)}%"></span></div>
              <span>${b.done}/${b.target} | ${b.integral} 分</span>
            </div>
            <div class="metric-row">
              <strong>拓展任务</strong>
              <div class="bar"><span style="width:${pct(e.done, e.target)}%"></span></div>
              <span>${e.done}/${e.target}</span>
            </div>
            <div class="score">
              <div><strong>${s.evaluate_target}</strong><span class="muted">评定目标</span></div>
              <div><strong>${totalIntegral}</strong><span class="muted">今日记录总积分</span></div>
              <div><strong>${s.record_complete_count || '-'}</strong><span class="muted">记录完成数</span></div>
              <div><strong>${e.list_total}</strong><span class="muted">展示列表项</span></div>
            </div>
            <p class="notice">${esc(listNote)}</p>
          </div>
        `;
      }).join('');
    }

    async function refresh() {
      try {
        const [status, logs] = await Promise.all([api('/api/status'), api('/api/logs')]);
        renderProcess(status.process);
        renderAccounts(status.accounts);
        $('paths').textContent = `配置: ${status.config_name} | 日志: ${status.log_name}`;
        $('logs').textContent = logs.text || '';
        $('updatedAt').textContent = new Date().toLocaleTimeString();
      } catch (err) {
        $('processState').innerHTML = `<span class="pill bad">${esc(err.message)}</span>`;
      }
    }

    $('refreshBtn').addEventListener('click', refresh);
    $('runBtn').addEventListener('click', async () => {
      $('runBtn').disabled = true;
      try { await api('/api/run', { method: 'POST', headers: { 'X-Admin-Token': ADMIN_TOKEN } }); }
      catch (err) { alert(err.message); }
      await refresh();
    });

    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    config_path: Path = DEFAULT_CONFIG
    admin_token: str = ""
    allowed_hosts: set[str] = {"127.0.0.1", "localhost", "::1"}

    def log_message(self, fmt: str, *args) -> None:
        message = redact_sensitive(fmt % args)
        sys.stdout.write("%s - %s\n" % (self.address_string(), message))

    def send_json(self, data: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _valid_run_request(self) -> bool:
        host_header = (self.headers.get("Host") or "").strip().lower()
        origin = (self.headers.get("Origin") or "").strip()
        token = self.headers.get("X-Admin-Token") or ""
        if not host_header or not origin or not token:
            return False
        try:
            host_name = urlparse(f"//{host_header}").hostname
            origin_parts = urlparse(origin)
        except ValueError:
            return False
        if not host_name or origin_parts.scheme not in ("http", "https"):
            return False
        host_name = host_name.lower()
        return bool(
            host_name in self.allowed_hosts
            and origin_parts.netloc.lower() == host_header
            and hmac.compare_digest(token, self.admin_token)
        )

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                self.send_html(INDEX_HTML.replace("__ADMIN_TOKEN__", json.dumps(self.admin_token)))
            elif path == "/api/status":
                self.send_json(build_status(self.config_path))
            elif path == "/api/logs":
                self.send_json({"text": tail_text(LOG_FILE)})
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as exc:
            self.send_json({"error": "status_unavailable"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/run":
                if not self._valid_run_request():
                    self.send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
                    return
                result = start_runner(self.config_path)
                status = HTTPStatus.ACCEPTED if result.get("started") else HTTPStatus.CONFLICT
                self.send_json(result, status)
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as exc:
            self.send_json({"error": "run_failed"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def validate_bind_options(host: str, allow_remote: bool, allowed_hosts: list[str]) -> bool:
    try:
        is_loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host.lower() == "localhost"
    if not is_loopback and not allow_remote:
        raise ValueError("外部监听必须显式添加 --allow-remote")
    if host in ("0.0.0.0", "::") and not allowed_hosts:
        raise ValueError("监听通配地址时必须至少提供一个 --allowed-host")
    return is_loopback


def main() -> None:
    parser = argparse.ArgumentParser(description="NetEase Music partner local web console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--allow-remote", action="store_true", help="显式允许监听非回环地址")
    parser.add_argument("--allowed-host", action="append", default=[], help="允许的 Host 名称，可重复指定")
    args = parser.parse_args()

    try:
        is_loopback = validate_bind_options(args.host, args.allow_remote, args.allowed_host)
    except ValueError as exc:
        parser.error(str(exc))

    Handler.config_path = Path(args.config).resolve()
    Handler.admin_token = secrets.token_urlsafe(32)
    Handler.allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    Handler.allowed_hosts.update(host.lower() for host in args.allowed_host)
    if args.host not in ("0.0.0.0", "::"):
        Handler.allowed_hosts.add(args.host.lower())
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Web console: http://{args.host}:{args.port}")
    print(f"Config: {Handler.config_path.name}")
    print("管理令牌已为本次启动随机生成，并仅注入本地控制页")
    if not is_loopback:
        print("安全警告: 已启用外部监听；请使用防火墙限制来源，并避免暴露到公网", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web console")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

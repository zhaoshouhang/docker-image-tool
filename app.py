#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Docker 镜像搜索 / 拉取 / 打包（本地 Web UI，无需安装 docker）

    python3 ~/docker-image-tool/app.py            # http://127.0.0.1:8799

流程：搜索仓库 → 看它的各个版本（tag）+ 每个版本有哪些架构/大小 → 选架构入队
      → 后台直接向 registry 拉取（可走镜像站/代理）→ 落成 tar.gz + SHA256SUMS。
不依赖本地 docker；输出可直接在内网 docker load。
"""
import argparse
import hashlib
import json
import os
import sys
import queue
import re
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import imgfetch as F

# Windows 控制台默认 GBK，打印中文/异常时容易 UnicodeEncodeError —— 统一切到 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")      # index.html / app.css / app.js
CONFIG_PATH = os.path.join(BASE, "config.json")
DEFAULT_OUT = os.path.join(BASE, "output")
JOBS = {}
JOB_ORDER = []
JOBS_LOCK = threading.Lock()
Q = queue.Queue()
WORKERS = 2

DEFAULT_CONFIG = {
    "api_base": "https://hub.docker.com",     # 搜索 / 标签用的 API
    "use_mirror": True,                       # True=从镜像站拉取；False=直连官方 registry（Docker Hub 走代理）
    "registry": "docker.1ms.run",             # 镜像站地址（use_mirror=True 时生效）
    "registry_mode": "hub",                   # hub=只给 Docker Hub 镜像换源 / all=全部走该地址
    "proxy": "",                              # 例 http://127.0.0.1:18080
    "proxy_scope": "auto",                    # auto=Hub走代理其余直连 / all=全走 / off=全直连
    "username": "", "password": "",
    "insecure": False,
    "os": "linux", "arch": "amd64", "variant": "",
    "gzip": True,
    "tag_suffix": False,                      # 归档内标签加 -amd64 后缀
    "outdir": DEFAULT_OUT,
    "retries": 2,                             # 下载失败自动重试次数（0=不自动重试，只用界面上的"重试"按钮）
    "retry_delay": 3,                         # 每次重试的间隔（秒），第 n 次等待 n×间隔
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(CONFIG_PATH):
        try:
            cfg.update(json.load(open(CONFIG_PATH, encoding="utf-8")))
        except Exception:
            pass
    if not cfg.get("proxy"):  # 自动探测本机代理
        for p in (18080, 7890, 1087, 8080):
            s = socket.socket()
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", p))
                cfg["proxy"] = f"http://127.0.0.1:{p}"
                break
            except Exception:
                pass
            finally:
                s.close()
    return cfg


CFG = load_config()


def save_config(d):
    global CFG
    for k in DEFAULT_CONFIG:
        if k in d:
            CFG[k] = d[k]
    CFG["insecure"] = bool(CFG.get("insecure"))
    CFG["gzip"] = bool(CFG.get("gzip"))
    CFG["tag_suffix"] = bool(CFG.get("tag_suffix"))
    CFG["use_mirror"] = bool(CFG.get("use_mirror", True))
    if CFG.get("proxy_scope") not in ("auto", "all", "off"):
        CFG["proxy_scope"] = "auto"
    try:
        CFG["retries"] = max(0, int(CFG.get("retries", 2)))
        CFG["retry_delay"] = max(0.0, float(CFG.get("retry_delay", 3)))
    except (TypeError, ValueError):
        CFG["retries"], CFG["retry_delay"] = 2, 3
    json.dump(CFG, open(CONFIG_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return CFG


# ------------------------------------------------------------------ 任务
def jlog(jid, line):
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if not j:
            return
        j["log"].append(line.rstrip())
        if len(j["log"]) > 300:
            del j["log"][:-150]


def jset(jid, **kw):
    with JOBS_LOCK:
        JOBS[jid].update(kw)


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")


def worker_loop():
    while True:
        jid = Q.get()
        try:
            run_job(jid)
        except Exception as e:  # noqa
            jlog(jid, "ERROR " + repr(e))
            jset(jid, status="failed", error=repr(e))
        finally:
            Q.task_done()


def run_job(jid):
    with JOBS_LOCK:
        job = dict(JOBS[jid])
    ev = job.get("cancel")
    if ev is not None and ev.is_set():          # 排队期间就被取消掉了
        jset(jid, status="cancelled", ended=time.time(), prog=None)
        jlog(jid, "已取消（未开始下载）")
        return
    active = {"resp": None}

    def _watch_cancel():                        # 取消时直接掐掉连接，避免卡在阻塞读上等 socket 超时
        if ev is None:
            return
        ev.wait()
        F.kill_response(active["resp"])

    if ev is not None:
        threading.Thread(target=_watch_cancel, daemon=True).start()
    c = job["cfg"]
    repo, tag = job["repo"], job["tag"]
    arch, variant, os_ = job["arch"], job["variant"], job["os"]
    jset(jid, status="running", started=time.time())

    def log(*a):
        jlog(jid, " ".join(str(x) for x in a))

    def progress(idx, layers, done, total, speed):
        jset(jid, prog={"layer": idx, "layers": layers, "done": done,
                        "total": total or 0, "speed": speed})

    log(f"registry={c['registry']}  代理={c.get('proxy') or '无'}")
    suffix = f"-{arch}" if c.get("tag_suffix") else ""
    tag_in_archive = (job["tag_out"] or tag) + suffix
    fname = f"{safe_name(job['display'] or repo)}_{safe_name(tag)}_{os_}-{arch}"
    if variant:
        fname += f"-{variant}"
    fname += ".tar.gz" if c.get("gzip") else ".tar"
    outdir = c.get("outdir") or DEFAULT_OUT
    os.makedirs(outdir, exist_ok=True)
    out_path = os.path.join(outdir, fname)
    try:
        res = F.pull(repo, tag, out_path,
                     platform_os=os_, platform_arch=arch, variant=variant or None,
                     host=c["registry"], proxy=c.get("proxy") or None,
                     username=c.get("username") or None, password=c.get("password") or None,
                     insecure=bool(c.get("insecure")), repo_tag=tag_in_archive,
                     display_repo=job["display"] or None,
                     gz=bool(c.get("gzip")), log=log, progress=progress,
                     proxy_scope=c.get("proxy_scope", "auto"),
                     retries=int(c.get("retries", 2)), retry_delay=float(c.get("retry_delay", 3)),
                     should_stop=(ev.is_set if ev is not None else None),
                     on_response=(lambda r: active.__setitem__("resp", r)))
        digest = res["sha256"]
        with open(os.path.join(outdir, "SHA256SUMS"), "a", encoding="utf-8") as f:
            f.write(f"{digest}  {os.path.basename(out_path)}\n")
        jset(jid, status="done", ended=time.time(), prog=None,
             result={"file": os.path.basename(out_path), "bytes": res["bytes"], "sha256": digest,
                     "platform": res["platform"], "tag_in_archive": f"{job['display'] or repo}:{tag_in_archive}",
                     "layers": res["layers"]})
        log(f"OK -> {os.path.basename(out_path)}  {F.human(res['bytes'])}  sha256={digest[:16]}...")
    except F.Cancelled as e:
        with JOBS_LOCK:
            last = (JOBS.get(jid) or {}).get("prog") or {}
        try:
            if os.path.exists(out_path):
                os.remove(out_path)             # 半成品删掉，不留垃圾
        except OSError:
            pass
        jset(jid, status="cancelled", ended=time.time(), prog=None, error=str(e))
        log("已取消，未完成的文件已删除"
            + (f"（取消前已下载 {F.human(last.get('done', 0))}／层 {last.get('layer')}/{last.get('layers')}）"
               if last.get("done") else ""))
    except Exception as e:  # noqa
        jset(jid, status="failed", ended=time.time(), prog=None, error=str(e))
        log("失败: " + str(e))


def enqueue(items, cfg_snapshot=None):
    """items: [{repo, tag, display, arch, variant, os, host?}] → 返回 job id 列表"""
    ids = []
    seen = set()
    # 同一批里 (镜像, tag) 重复但架构不同 → 归档内标签加架构后缀，避免互相覆盖
    counts = {}
    for it in items:
        counts[(it["repo"], it["tag"])] = counts.get((it["repo"], it["tag"]), 0) + 1
    for it in items:
        key = (it["repo"], it["tag"], it["arch"], it.get("variant") or "")
        if key in seen:
            continue
        seen.add(key)
        jid = uuid.uuid4().hex[:10]
        tag_out = it["tag"]
        if counts[(it["repo"], it["tag"])] > 1:
            tag_out = f"{it['tag']}-{it['arch']}"
        cfg = dict(cfg_snapshot or CFG)          # 每个任务快照设置，跑的时候不再受改设置影响
        if it.get("host"):
            cfg["registry"] = it["host"]
        with JOBS_LOCK:
            JOBS[jid] = {"id": jid, "repo": it["repo"], "tag": it["tag"], "display": it.get("display") or "",
                         "arch": it["arch"], "variant": it.get("variant") or "", "os": it.get("os") or "linux",
                         "tag_out": tag_out, "status": "queued", "log": [], "cfg": cfg,
                         "cancel": threading.Event(),
                         "title": f"{it.get('display') or it['repo']}:{it['tag']}  {it.get('os') or 'linux'}/{it['arch']}",
                         "queued": time.time(), "prog": None, "result": None, "error": None}
            JOB_ORDER.insert(0, jid)
        Q.put(jid)
        ids.append(jid)
    return ids


for _ in range(WORKERS):
    threading.Thread(target=worker_loop, daemon=True).start()


# ------------------------------------------------------------------ HTTP 页面


class H(BaseHTTPRequestHandler):
    server_version = "imgweb/2.0"

    def log_message(self, *a):
        pass

    MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
            ".txt": "text/plain; charset=utf-8"}

    def _static(self, name):
        """只允许 static/ 下的单层文件（basename 防目录穿越），并禁用缓存，
        免得改了样式还看到旧页面 —— 之前 内联样式 时不明显，拆成文件后必须显式关掉。"""
        name = os.path.basename((name or "").strip("/"))
        path = os.path.join(STATIC, name)
        if not name or not os.path.isfile(path):
            return self._send(404, {"error": f"static 文件不存在: {name}"})
        data = open(path, "rb").read()
        if name == "index.html":                   # ?v=__V__ 动态替换成 css/js 内容的哈希
            try:
                h = hashlib.sha256()
                for f in ("app.css", "app.js"):
                    h.update(open(os.path.join(STATIC, f), "rb").read())
                data = data.replace(b"__V__", h.hexdigest()[:8].encode())
            except OSError:
                pass
        self.send_response(200)
        self.send_header("Content-Type", self.MIME.get(os.path.splitext(name)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u, q = urlparse(self.path), None
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._static("index.html")
            if u.path.startswith("/static/"):
                return self._static(u.path[len("/static/"):])
            if u.path == "/api/config":
                return self._send(200, {"config": CFG})
            if u.path == "/api/search":
                d = F.search_repos(q.get("q", [""])[0], page=int(q.get("page", ["1"])[0]),
                                   page_size=25, api_base=CFG["api_base"],
                                   proxy=CFG.get("proxy") or None, insecure=CFG.get("insecure"),
                                   proxy_scope=CFG.get("proxy_scope", "auto"))
                return self._send(200, d)
            if u.path == "/api/tags":
                d = F.repo_tags(q.get("repo", [""])[0], name_filter=q.get("filter", [""])[0],
                                page=int(q.get("page", ["1"])[0]), page_size=50,
                                ordering=q.get("ordering", ["last_updated"])[0],
                                api_base=CFG["api_base"], proxy=CFG.get("proxy") or None,
                                insecure=CFG.get("insecure"), proxy_scope=CFG.get("proxy_scope", "auto"))
                return self._send(200, d)
            if u.path == "/api/compose":
                p = os.path.expanduser(q.get("path", [""])[0])
                if not os.path.isfile(p):
                    return self._send(400, {"error": f"文件不存在: {p}"})
                txt = open(p, encoding="utf-8", errors="ignore").read()
                imgs = []
                for m in re.finditer(r'^\s+image:\s*["\']?([^\s"\'#]+)["\']?(?:\s*#.*)?$', txt, re.M):
                    if m.group(1) not in imgs:
                        imgs.append(m.group(1))
                return self._send(200, {"images": imgs, "file": p})
            if u.path == "/api/jobs":
                with JOBS_LOCK:
                    js = []
                    for i in JOB_ORDER[:14]:
                        j = dict(JOBS[i])
                        j.pop("cancel", None)      # 线程事件不可 JSON 序列化
                        j.pop("cfg", None)
                        js.append(j)
                for j in js:
                    j["log"] = j["log"][-30:]
                return self._send(200, {"jobs": js, "queued": Q.qsize()})
            if u.path == "/api/files":
                od = CFG.get("outdir") or DEFAULT_OUT
                os.makedirs(od, exist_ok=True)
                fs = [{"name": f, "bytes": os.path.getsize(os.path.join(od, f))}
                      for f in sorted(os.listdir(od)) if f.endswith((".tar", ".tar.gz"))]
                return self._send(200, {"files": fs, "dir": od})
            if u.path == "/api/download":
                f = os.path.basename(q.get("f", [""])[0])
                p = os.path.join(CFG.get("outdir") or DEFAULT_OUT, f)
                if not f or not os.path.isfile(p):
                    return self._send(404, {"error": "not found"})
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Length", str(os.path.getsize(p)))
                self.send_header("Content-Disposition", f'attachment; filename="{f}"')
                self.end_headers()
                with open(p, "rb") as fh:
                    while True:
                        b = fh.read(1 << 20)
                        if not b:
                            break
                        self.wfile.write(b)
                return
            return self._send(404, {"error": "no route"})
        except Exception as e:  # noqa
            return self._send(200, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        try:
            if u.path == "/api/config":
                return self._send(200, {"config": save_config(body)})
            if u.path == "/api/test":
                return self._send(200, self._test({**CFG, **(body or {})}))
            if u.path == "/api/queue":
                items = [it for it in body.get("items", []) if it.get("repo")]
                if not items:
                    return self._send(400, {"error": "没有可入队的镜像"})
                snap = dict(CFG)
                fixed = []
                use_mirror = bool(CFG.get("use_mirror", True))
                for it in items:
                    repo = it["repo"].strip().lstrip("/")
                    host = it.pop("host_override", "") or None
                    if host is None:                       # 镜像名自带 registry（含 . 或 :）→ 默认保持原样
                        parts = repo.split("/")
                        if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
                            host = parts[0]
                            repo = "/".join(parts[1:])
                    if host is None:                       # Docker Hub 镜像（无自带 registry）
                        if use_mirror:
                            host = CFG["registry"]          # 走镜像站
                        else:
                            host = "registry-1.docker.io"   # 直连官方（国内需要代理）
                    elif use_mirror and CFG.get("registry_mode") == "all":
                        host = CFG["registry"]              # 全部走镜像站（含 ghcr/lscr 等）
                    fixed.append({**it, "repo_full": repo, "host": host})
                ids = enqueue([{"repo": it["repo_full"], "tag": it["tag"],
                                "display": it.get("display") or it["repo_full"],
                                "os": it.get("os") or CFG["os"], "arch": it.get("arch") or CFG["arch"],
                                "variant": it.get("variant") or "", "host": it["host"]}
                               for it in fixed], cfg_snapshot=snap)
                hosts = sorted({it["host"] for it in fixed})
                return self._send(200, {"ids": ids, "note": "拉取源: " + ", ".join(hosts)})
            if u.path == "/api/cancel":
                with JOBS_LOCK:
                    if body.get("id"):
                        targets = [JOBS[body["id"]]] if body["id"] in JOBS else []
                    else:                          # 不带 id = 取消所有排队中与运行中的
                        targets = [JOBS[i] for i in JOB_ORDER if JOBS[i]["status"] in ("queued", "running")]
                    n = 0
                    for j in targets:
                        if j["status"] not in ("queued", "running"):
                            continue
                        ev = j.get("cancel")
                        if ev is None:
                            ev = j["cancel"] = threading.Event()
                        ev.set()
                        n += 1
                        if j["status"] == "queued":     # 还没开跑：立刻判定取消，不等 worker
                            j["status"] = "cancelled"
                            j["ended"] = time.time()
                            j["prog"] = None
                            j["log"].append("已取消（排队中移除）")
                        else:                          # 正在跑：worker 在下一个检查点中断
                            j["log"].append("已请求取消，正在中断下载…")
                if not n:
                    return self._send(400, {"error": "没有排队中或运行中的任务"})
                return self._send(200, {"n": n})
            if u.path == "/api/retry":
                with JOBS_LOCK:
                    if body.get("id"):
                        targets = [dict(JOBS[body["id"]])] if body["id"] in JOBS else []
                    else:                                  # 不给 id = 重试全部失败的
                        targets = [dict(JOBS[i]) for i in JOB_ORDER if JOBS[i]["status"] == "failed"]
                if not targets:
                    return self._send(400, {"error": "没有可重试的任务"})
                ids = enqueue([{"repo": j["repo"], "tag": j["tag"], "display": j["display"],
                                "os": j["os"], "arch": j["arch"], "variant": j["variant"]}
                               for j in targets], cfg_snapshot=dict(CFG))
                return self._send(200, {"ids": ids, "n": len(ids)})
            if u.path == "/api/delete":
                f = os.path.basename(body.get("file") or "")
                p = os.path.join(CFG.get("outdir") or DEFAULT_OUT, f)
                if f and os.path.isfile(p):
                    os.remove(p)
                    # 同步删掉 SHA256SUMS 里对应的行，否则离线校验会报「文件不存在」
                    sums = os.path.join(CFG.get("outdir") or DEFAULT_OUT, "SHA256SUMS")
                    if os.path.isfile(sums):
                        try:
                            with open(sums, encoding="utf-8") as fh:
                                lines = [ln for ln in fh if f not in ln]
                            with open(sums, "w", encoding="utf-8") as fh:
                                fh.writelines(lines)
                        except OSError:
                            pass
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "no route"})
        except Exception as e:  # noqa
            return self._send(200, {"error": f"{type(e).__name__}: {e}"})

    def _test(self, cfg):
        out = []
        scope = cfg.get("proxy_scope") or "auto"
        try:
            d = F.search_repos("alpine", page_size=1, api_base=cfg.get("api_base") or DEFAULT_CONFIG["api_base"],
                               proxy=cfg.get("proxy") or None, insecure=bool(cfg.get("insecure")),
                               proxy_scope=scope)
            out.append(f"搜索API OK（命中 {d['count']}）")
        except Exception as e:
            out.append(f"搜索API 失败: {e}")
        try:
            rhost = "registry-1.docker.io" if not cfg.get("use_mirror", True) else (cfg.get("registry") or "registry-1.docker.io")
            cli = F.Client(rhost, proxy=cfg.get("proxy") or None,
                           insecure=bool(cfg.get("insecure")), timeout=25, proxy_scope=scope)
            data, digest, _ = cli.manifest("library/alpine", "latest")
            n = len(data.get("manifests", []))
            out.append(f"registry {rhost} OK（{n} 个平台清单，{(digest or '')[:19]}…）" if n
                       else f"registry {rhost} OK（单平台镜像）")
        except Exception as e:
            out.append(f"registry 失败: {e}")
        ok = all("失败" not in o for o in out)
        return {"ok": ok, "detail": "；".join(out)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    a = ap.parse_args()
    os.makedirs(CFG.get("outdir") or DEFAULT_OUT, exist_ok=True)
    missing = [f for f in ("index.html", "app.css", "app.js") if not os.path.isfile(os.path.join(STATIC, f))]
    if missing:
        print(f"!! static/ 缺少文件: {missing} —— 页面会打不开，请确认目录完整")
    print(f"搜索API : {CFG['api_base']}   代理: {CFG['proxy'] or '无'}")
    print(f"拉取源  : {CFG['registry']}（模式 {CFG['registry_mode']}）  默认平台: {CFG['os']}/{CFG['arch']}{'/'+CFG['variant'] if CFG['variant'] else ''}")
    print(f"输出目录: {CFG.get('outdir')}")
    print(f"打开 http://{a.host}:{a.port}   （Ctrl+C 停止）")
    if a.open:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{a.host}:{a.port}")).start()
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()


if __name__ == "__main__":
    main()

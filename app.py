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
CONFIG_PATH = os.path.join(BASE, "config.json")
DEFAULT_OUT = os.path.join(BASE, "output")
JOBS = {}
JOB_ORDER = []
JOBS_LOCK = threading.Lock()
Q = queue.Queue()
WORKERS = 2

DEFAULT_CONFIG = {
    "api_base": "https://hub.docker.com",     # 搜索 / 标签用的 API
    "registry": "docker.1ms.run",             # 拉取用的 registry（镜像站）
    "registry_mode": "hub",                   # hub=只给 Docker Hub 镜像换源 / all=全部走该地址
    "proxy": "",                              # 例 http://127.0.0.1:18080
    "proxy_scope": "auto",                    # auto=Hub走代理其余直连 / all=全走 / off=全直连
    "username": "", "password": "",
    "insecure": False,
    "os": "linux", "arch": "amd64", "variant": "",
    "archive": "docker-archive",              # docker-archive(兼容旧docker) / oci-archive
    "gzip": True,
    "tag_suffix": False,                      # 归档内标签加 -amd64 后缀
    "outdir": DEFAULT_OUT,
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
    if CFG.get("proxy_scope") not in ("auto", "all", "off"):
        CFG["proxy_scope"] = "auto"
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
                     archive=c.get("archive", "docker-archive"),
                     gz=bool(c.get("gzip")), log=log, progress=progress,
                     proxy_scope=c.get("proxy_scope", "auto"))
        digest = res["sha256"]
        with open(os.path.join(outdir, "SHA256SUMS"), "a", encoding="utf-8") as f:
            f.write(f"{digest}  {os.path.basename(out_path)}\n")
        jset(jid, status="done", ended=time.time(), prog=None,
             result={"file": os.path.basename(out_path), "bytes": res["bytes"], "sha256": digest,
                     "platform": res["platform"], "tag_in_archive": f"{job['display'] or repo}:{tag_in_archive}",
                     "layers": res["layers"]})
        log(f"OK -> {os.path.basename(out_path)}  {F.human(res['bytes'])}  sha256={digest[:16]}...")
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
                         "title": f"{it.get('display') or it['repo']}:{it['tag']}  {it.get('os') or 'linux'}/{it['arch']}",
                         "queued": time.time(), "prog": None, "result": None, "error": None}
            JOB_ORDER.insert(0, jid)
        Q.put(jid)
        ids.append(jid)
    return ids


for _ in range(WORKERS):
    threading.Thread(target=worker_loop, daemon=True).start()


# ------------------------------------------------------------------ HTTP 页面
PAGE = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>镜像搜索 / 拉取 / 打包</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{
  --bg:#111318;--card:#1a1d24;--fg:#eceef4;--dim:#a9b2c0;--acc:#5b93ff;--ok:#4ac95f;--bad:#ff6a60;--warn:#e3ad33;
  /* 字体栈：macOS 优先，Windows(Segoe UI/微软雅黑/Consolas) 与 Linux 都有回退，避免落到 Courier New 那种发灰的细体 */
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei","Hiragino Sans GB","Noto Sans CJK SC","Noto Sans SC",Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Cascadia Mono","DejaVu Sans Mono","Liberation Mono",monospace;
}
/* 根字号随视口放大：笔记本高分辨率(2560/1920 下 100% 缩放)不会再显得小，最大 19px 防止过大 */
html{font-size:clamp(15px,0.42vw + 11.6px,19px);-webkit-text-size-adjust:100%}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:1rem/1.6 var(--font);text-rendering:optimizeLegibility}
header{padding:.8rem 1rem;border-bottom:1px solid #262a33;display:flex;gap:.7rem;align-items:center;flex-wrap:wrap}
h1{font-size:1.15rem;margin:0;font-weight:600}
.dim{color:var(--dim);font-size:.83rem}
main{padding:.9rem 1rem;display:grid;grid-template-columns:minmax(0,1.62fr) minmax(0,1fr);gap:1rem;align-items:start}
@media(max-width:1180px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid #262a33;border-radius:.7rem;padding:.75rem .85rem;margin-bottom:.9rem}
.card h2{font-size:.9rem;margin:0 0 .55rem;color:var(--dim);font-weight:600;letter-spacing:.03em}
table{width:100%;border-collapse:collapse;font-size:.92rem;min-width:34rem}
th,td{text-align:left;padding:.42em .5em;border-bottom:1px solid #22262f;vertical-align:top;color:var(--fg)}
th{color:var(--dim);font-weight:500;white-space:nowrap}
tbody tr:hover{background:#1f2430}tr.sel{background:#232c3e}
/* 镜像名 / tag 名：明确纯白 + 中等字重，Windows 上 Consolas 也不会显得发灰 */
.mono{font-family:var(--mono);color:#fff;font-weight:500;letter-spacing:.01em}
.scroll{max-height:min(42vh,34rem);overflow:auto}
input,select,textarea,button{background:#0e1015;color:var(--fg);border:1px solid #2b3140;border-radius:.5rem;
  padding:.45em .7em;font:inherit;font-size:.95rem;min-height:2.35em}
input[type=text],input[type=password],textarea{width:100%}
input[type=checkbox]{width:1.05em;height:1.05em;min-height:0;margin:0;vertical-align:-.12em}
textarea{min-height:6em;font-family:var(--mono);font-size:.88rem}
button{cursor:pointer;background:#232a38;white-space:nowrap}button:hover{border-color:var(--acc)}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
button.mini{padding:.25em .6em;font-size:.86rem;min-height:1.95em}
button:disabled{opacity:.4;cursor:not-allowed}
.row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-bottom:.55rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(14.5rem,1fr));gap:.6rem}
label.f{display:block}.f .lb{color:var(--dim);font-size:.8rem;margin-bottom:.2rem;display:block}
.chip{display:inline-block;font-size:.76rem;padding:.12em .5em;border-radius:1.2em;background:#252b38;color:#b9c2d0;margin:.08em .18em .08em 0;white-space:nowrap}
.chip.on{background:#1d3a23;color:#6fd984;font-weight:600}
.tag{font-size:.76rem;padding:.12em .5em;border-radius:1.2em;background:#272d3a;color:var(--dim)}
.tag.done{background:#16351f;color:var(--ok)}.tag.failed{background:#3a1b1b;color:var(--bad)}
.tag.running{background:#1b2b47;color:#8cbaff}.tag.queued{background:#2c2a1c;color:var(--warn)}
pre{margin:.35rem 0 0;max-height:12rem;overflow:auto;font-size:.8rem;font-family:var(--mono);color:#c3cbd8;white-space:pre-wrap;word-break:break-all}
.job{border:1px solid #262a33;border-radius:.6rem;padding:.5rem .65rem;margin-bottom:.45rem;background:#171a21}
a{color:var(--acc)}
.bar{height:.4rem;background:#20242e;border-radius:.4rem;overflow:hidden;margin-top:.35rem}
.bar>i{display:block;height:100%;background:var(--acc);transition:width .3s}
.hint{font-size:.8rem;color:var(--dim)}
@media(max-width:640px){html{font-size:15px}table{min-width:30rem}}
</style></head><body>
<header>
  <h1>镜像搜索 / 拉取 / 打包</h1>
  <span class="dim">无需安装 docker · 支持镜像站与代理 · 按架构导出 tar.gz · 仅绑 127.0.0.1</span>
  <span style="flex:1"></span>
  <button onclick="toggle('setBox')">设置</button>
</header>
<main>
  <div>
    <div class="card" id="setBox" style="display:none">
      <h2>设置（保存到 config.json）</h2>
      <div class="grid">
        <label class="f"><span class="lb">搜索 / 标签 API 地址</span><input type="text" id="c_api"></label>
        <label class="f"><span class="lb">拉取 registry（镜像站）</span><input type="text" id="c_reg" list="mirrors"></label>
        <datalist id="mirrors">
          <option value="docker.1ms.run"><option value="docker.m.daocloud.io">
          <option value="docker-0.unsee.tech"><option value="registry-1.docker.io">
          <option value="ghcr.io"><option value="lscr.io"><option value="quay.io">
        </datalist>
        <label class="f"><span class="lb">换源范围</span><select id="c_mode">
          <option value="hub">只给 Docker Hub 官方/普通镜像换源（ghcr.io、lscr.io 等保持原样）</option>
          <option value="all">全部走上面这个地址</option></select></label>
        <label class="f"><span class="lb">HTTP 代理（留空=直连）</span><input type="text" id="c_proxy" placeholder="http://127.0.0.1:18080"></label>
        <label class="f"><span class="lb">代理用在哪</span><select id="c_pscope">
          <option value="auto">自动：Docker Hub 走代理；镜像站/ghcr 直连，连不上再回退代理</option>
          <option value="all">全部请求都走代理</option>
          <option value="off">完全不用代理（全直连）</option></select></label>
        <label class="f"><span class="lb">私有仓库用户名</span><input type="text" id="c_user"></label>
        <label class="f"><span class="lb">私有仓库密码</span><input type="password" id="c_pass"></label>
        <label class="f"><span class="lb">默认平台</span>
          <div class="row" style="margin:0">
            <select id="c_os" style="width:88px"><option>linux</option></select>
            <select id="c_arch" style="width:100px"><option>amd64</option><option>arm64</option><option>arm</option><option>386</option><option>ppc64le</option><option>s390x</option><option>riscv64</option></select>
            <select id="c_var" style="width:78px"><option value="">variant</option><option>v8</option><option>v7</option><option>v6</option></select>
          </div></label>
        <label class="f"><span class="lb">归档格式</span><select id="c_arch_fmt">
          <option value="docker-archive">docker-archive（推荐，老 docker 也能 load）</option>
          <option value="oci-archive">oci-archive（需 docker 25+）</option></select></label>
        <label class="f"><span class="lb">输出</span><select id="c_gz">
          <option value="1">tar.gz</option><option value="0">tar（不压缩，内网 load 更快）</option></select></label>
        <label class="f"><span class="lb">输出目录</span><input type="text" id="c_out"></label>
      </div>
      <div class="row" style="margin-top:9px">
        <button class="primary" onclick="saveCfg()">保存设置</button>
        <button onclick="testCfg()">测试连通性</button>
        <label class="hint"><input type="checkbox" id="c_insecure" style="width:auto"> 跳过 TLS 校验（自建 registry 自签证书）</label>
        <label class="hint"><input type="checkbox" id="c_suffix" style="width:auto"> 归档内标签加 -架构 后缀</label>
        <span class="hint" id="cfgMsg"></span>
      </div>
    </div>

    <div class="card">
      <h2>1. 搜索镜像仓库</h2>
      <div class="row">
        <input type="text" id="q" placeholder="例如 nginx / mysql / siyuan / ghcr.io/advplyr/audiobookshelf" style="max-width:420px">
        <button class="primary" onclick="doSearch(1)">搜索</button>
        <span class="hint" id="sMsg"></span>
      </div>
      <div class="scroll"><table>
        <thead><tr><th>仓库</th><th style="width:74px">★ / 拉取</th><th>说明</th><th style="width:52px"></th></tr></thead>
        <tbody id="sRows"><tr><td colspan="4" class="hint">输入关键字搜索；镜像站只解析拉取，搜索走上面的 API 地址。</td></tr></tbody>
      </table></div>
      <div class="row" style="margin-top:7px"><button id="moreS" onclick="doSearch(sPage+1)" style="display:none">下一页</button></div>
    </div>

    <div class="card">
      <h2>2. 选择版本（tag）与架构 <span class="dim" id="tRepo"></span></h2>
      <div class="row">
        <input type="text" id="tFilter" placeholder="按 tag 过滤，如 1.27 / alpine / stable" style="max-width:250px">
        <select id="tOrder" style="width:190px"><option value="last_updated">按更新时间（新→旧）</option><option value="-last_updated">按更新时间（旧→新）</option><option value="name">按名字 A→Z</option></select>
        <button onclick="loadTags(1)">刷新版本</button>
        <select id="tArch" onchange="renderTags()" style="width:150px"><option value="">显示全部架构</option><option value="amd64">只看 amd64</option><option value="arm64">只看 arm64</option><option value="arm">只看 arm/v7</option></select>
        <span class="dim" id="tMsg"></span>
      </div>
      <div class="scroll"><table>
        <thead><tr><th style="width:40px"></th><th style="width:210px">tag</th><th style="width:92px">更新</th><th>可用架构（点「拉取」按设置里的平台导）</th></tr></thead>
        <tbody id="tRows"><tr><td colspan="4" class="hint">先在上面搜索并点一个仓库。</td></tr></tbody>
      </table></div>
      <div class="row" style="margin-top:7px">
        <button id="moreT" onclick="loadTags(tPage+1)" style="display:none">下一页</button>
        <span style="flex:1"></span>
        <button class="primary" onclick="queueChecked()">把勾选的 tag 按当前平台导出</button>
        <span class="hint" id="selMsg"></span>
      </div>
    </div>
  </div>

  <div>
    <div class="card">
      <h2>批量：一次拉一个列表</h2>
      <div class="row"><input type="text" id="bCompose" placeholder="docker-compose.yml 路径（可选）"></div>
      <div class="row"><button onclick="parseCompose()">读取 compose 里的镜像</button>
        <span class="hint" id="bMsg"></span></div>
      <textarea id="bList" rows="5" placeholder="每行一个，例如：&#10;nginx:1.27.3&#10;mysql:8.4&#10;gtstef/filebrowser:1.5-stable&#10;ghcr.io/advplyr/audiobookshelf:latest"></textarea>
      <div class="row" style="margin-top:7px">
        <button class="primary" onclick="queueBatch()">按当前平台全部入队</button>
        <span class="hint">平台=设置里的 默认平台（当前 <b id="curArch"></b>）</span>
      </div>
    </div>

    <div class="card">
      <h2>任务 <span class="dim" id="qMsg"></span></h2>
      <div id="jobs"><span class="hint">暂无任务</span></div>
    </div>

    <div class="card">
      <h2>已生成的包 <span class="dim" id="outDir"></span></h2>
      <div id="files"><span class="hint">无</span></div>
    </div>
  </div>
</main>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let CFG={}, SELREPO=null, TAGS=[], sPage=1, tPage=1, CHK=new Set();
const esc=s=>String(s==null?'':s).replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
const fmt=b=>b>1073741824?(b/1073741824).toFixed(2)+' GB':b>1048576?(b/1048576).toFixed(1)+' MB':(b/1024).toFixed(0)+' KB';
const nfmt=n=>n>1e8?(n/1e8).toFixed(1)+'亿':n>1e4?(n/1e4).toFixed(1)+'万':String(n);
const api=async(p,o)=>{const r=await fetch(p,o);const j=await r.json(); if(j.error) throw new Error(j.error); return j;};
const post=(p,b)=>api(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});

function toggle(id){const e=$('#'+id); e.style.display = e.style.display==='none'?'':'none'}
async function loadCfg(){
  const d=await api('/api/config'); CFG=d.config;
  $('#c_api').value=CFG.api_base; $('#c_reg').value=CFG.registry; $('#c_mode').value=CFG.registry_mode;
  $('#c_proxy').value=CFG.proxy; $('#c_user').value=CFG.username; $('#c_pass').value=CFG.password;
  $('#c_pscope').value=CFG.proxy_scope||'auto';
  $('#c_os').value=CFG.os; $('#c_arch').value=CFG.arch; $('#c_var').value=CFG.variant||'';
  $('#c_arch_fmt').value=CFG.archive; $('#c_gz').value=CFG.gzip?'1':'0'; $('#c_out').value=CFG.outdir;
  $('#c_insecure').checked=!!CFG.insecure; $('#c_suffix').checked=!!CFG.tag_suffix;
  $('#outDir').textContent=CFG.outdir; drawArch();
}
function collectCfg(){return {api_base:$('#c_api').value.trim(),registry:$('#c_reg').value.trim(),registry_mode:$('#c_mode').value,
  proxy:$('#c_proxy').value.trim(),proxy_scope:$('#c_pscope').value,username:$('#c_user').value.trim(),password:$('#c_pass').value,
  os:$('#c_os').value,arch:$('#c_arch').value,variant:$('#c_var').value,archive:$('#c_arch_fmt').value,
  gzip:$('#c_gz').value==='1',outdir:$('#c_out').value.trim(),insecure:$('#c_insecure').checked,tag_suffix:$('#c_suffix').checked};}
async function saveCfg(){const d=await post('/api/config',collectCfg()); CFG=d.config; $('#cfgMsg').textContent='已保存 '+new Date().toLocaleTimeString(); drawArch();}
function drawArch(){const v=CFG.variant?('/'+CFG.variant):''; const t=CFG.os+'/'+CFG.arch+v; $('#curArch').textContent=t;
  $$('#curArch').forEach(e=>e.textContent=t);}
async function testCfg(){
  $('#cfgMsg').textContent='测试中…';
  try{const d=await post('/api/test',collectCfg());
    $('#cfgMsg').innerHTML=d.ok?'<span style="color:#3fb950">连通 OK</span> '+esc(d.detail):'<span style="color:#f85149">失败</span> '+esc(d.detail);
  }catch(e){$('#cfgMsg').innerHTML='<span style="color:#f85149">'+esc(e.message)+'</span>';}
}

async function doSearch(p){
  const q=$('#q').value.trim(); if(!q) return;
  sPage=p; $('#sMsg').textContent='搜索中…';
  try{
    const d=await api('/api/search?q='+encodeURIComponent(q)+'&page='+p);
    $('#sMsg').textContent=`命中 ${d.count}，第 ${d.page} 页`;
    $('#sRows').innerHTML=d.results.map(r=>`<tr data-ref="${esc(r.pull_ref)}">
      <td class="mono">${esc(r.pull_ref)} ${r.official?'<span class="chip on">官方</span>':''}</td>
      <td class="dim">★${nfmt(r.stars)}<br>${nfmt(r.pulls)}</td>
      <td class="dim">${esc(r.desc.slice(0,110))}</td>
      <td><button class="mini" onclick="pickRepo('${esc(r.pull_ref)}')">选</button></td></tr>`).join('');
    $('#moreS').style.display = d.results.length ? '' : 'none';
  }catch(e){$('#sRows').innerHTML=`<tr><td colspan="4" style="color:#f85149">${esc(e.message)}</td></tr>`; $('#sMsg').textContent='';}
}
function pickRepo(ref,keep){
  SELREPO=ref; CHK.clear(); $('#tRepo').textContent='→ '+ref; if(!keep) $('#tFilter').value=''; loadTags(1);
  $$('#sRows tr').forEach(tr=>tr.classList.toggle('sel', tr.dataset.ref===ref));
  const box=document.querySelector('#tRows').closest('.scroll'); if(box) box.scrollTop=0;
}
async function loadTags(p){
  if(!SELREPO) return;
  tPage=p; $('#tMsg').textContent='读取版本…';
  const url=`/api/tags?repo=${encodeURIComponent(SELREPO)}&filter=${encodeURIComponent($('#tFilter').value.trim())}&page=${p}&ordering=${$('#tOrder').value}`;
  try{ const d=await api(url); TAGS = p===1? d.results : TAGS.concat(d.results);
    $('#tMsg').textContent=`共 ${d.count} 个 tag，已载入 ${TAGS.length}`;
    $('#moreT').style.display = d.results.length ? '' : 'none';
    renderTags();
  }catch(e){$('#tRows').innerHTML=`<tr><td colspan="4" style="color:#f85149">${esc(e.message)}</td></tr>`; $('#tMsg').textContent='';}
}
function renderTags(){
  const fa=$('#tArch').value;
  $('#tRows').innerHTML=TAGS.map((t,i)=>{
    const chips=t.platforms.filter(p=>!fa||p.arch===fa).map(p=>
      `<span class="chip ${p.arch===CFG.arch&&(p.variant||'')===(CFG.variant||'')?'on':''}" title="${p.os}/${p.arch}${p.variant?'/'+p.variant:''}">${p.arch}${p.variant?'/'+p.variant:''} ${fmt(p.size)}</span>`).join('');
    const has=t.platforms.some(p=>p.arch===CFG.arch && (!CFG.variant || (p.variant||'')===CFG.variant));
    return `<tr><td><input type="checkbox" style="width:auto" data-i="${i}" ${CHK.has(t.tag)?'checked':''} ${has?'':'disabled'}></td>
      <td class="mono">${esc(t.tag)}</td><td class="dim">${esc(t.updated.slice(0,10))}</td>
      <td>${chips||'<span class="dim">无跨平台信息</span>'}
        ${has?'':`<span class="chip" style="color:#f85149">无 ${esc(CFG.arch)}</span>`}
        <button class="mini" style="margin-left:6px" ${has?'':'disabled'} onclick="queueOne(${i})">拉取 ${esc(CFG.arch)}</button></td></tr>`;
  }).join('')||'<tr><td colspan="4" class="hint">没有匹配的 tag</td></tr>';
  $$('#tRows input[type=checkbox]').forEach(c=>c.onchange=()=>{const tg=TAGS[+c.dataset.i].tag; c.checked?CHK.add(tg):CHK.delete(tg); $('#selMsg').textContent=CHK.size?`已勾选 ${CHK.size} 个` : ''});
}
function item(tag){const [host,repo]=split(SELREPO); return {repo, tag, display:SELREPO.replace(/^library\//,''),
  os:CFG.os, arch:CFG.arch, variant:CFG.variant||''};}
function split(ref){ // 返回 [registry_host, repository]，镜像站模式下统一换源
  ref=ref.replace(/^library\//,'');
  let host=null, r=ref;
  if(ref.includes('/') && (ref.split('/')[0].includes('.')||ref.split('/')[0].includes(':'))){host=ref.split('/')[0]; r=ref.split('/').slice(1).join('/')}
  else r = ref.includes('/')?ref:('library/'+ref);
  if(CFG.registry_mode==='all' || !host) host=null;
  return [host, r];
}
async function queueOne(i){ await push([item(TAGS[i].tag)]); }
async function queueChecked(){
  if(!CHK.size) return alert('先勾选 tag（没有对应架构的会置灰）');
  const items=TAGS.filter(t=>CHK.has(t.tag)).map(t=>item(t.tag));
  CHK.clear(); await push(items);
}
async function push(items){
  const d=await post('/api/queue',{items});
  $('#qMsg').textContent=`已入队 ${d.ids.length} 个`+(d.note?('，'+d.note):'');
  tick();
}
async function parseCompose(){
  const p=$('#bCompose').value.trim(); if(!p) return alert('填 compose 文件路径');
  try{const d=await api('/api/compose?path='+encodeURIComponent(p));
    $('#bList').value=d.images.join('\n'); $('#bMsg').textContent=`读到 ${d.images.length} 个镜像`;}
  catch(e){$('#bMsg').innerHTML='<span style="color:#f85149">'+esc(e.message)+'</span>'}
}
async function queueBatch(){
  const lines=$('#bList').value.split('\n').map(s=>s.trim()).filter(s=>s&&!s.startsWith('#'));
  if(!lines.length) return alert('列表是空的');
  const items=[];
  for(const line of lines){
    let ref=line, tag='latest';
    if(ref.split('/').pop().includes(':')){const i=ref.lastIndexOf(':'); tag=ref.slice(i+1); ref=ref.slice(0,i)}
    const [host,repo]=split(ref);
    items.push({repo, tag, display:ref.replace(/^library\//,''), os:CFG.os, arch:CFG.arch, variant:CFG.variant||'', host_override:host||''});
  }
  await push(items);
}
async function tick(){
  try{
    const d=await api('/api/jobs');
    $('#jobs').innerHTML=d.jobs.map(j=>{
      const p=j.prog, pct=p&&p.total?Math.min(100,Math.round(p.done*100/p.total)):0;
      return `<div class="job"><b>${esc(j.title)}</b> <span class="tag ${j.status}">${j.status}</span>
        ${p?`<div class="hint">层 ${p.layer}/${p.layers} · ${fmt(p.done)}/${fmt(p.total)} · ${fmt(p.speed)}/s</div><div class="bar"><i style="width:${pct}%"></i></div>`:''}
        ${j.result?`<div class="hint">${esc(j.result.file)} · ${fmt(j.result.bytes)} · ${esc(j.result.platform)} · 镜像名 ${esc(j.result.tag_in_archive)} · <a href="/api/download?f=${encodeURIComponent(j.result.file)}">下载</a> · sha256 ${esc(j.result.sha256.slice(0,12))}…</div>`:''}
        ${j.error?`<div style="color:#f85149">${esc(j.error)}</div>`:''}
        <pre>${esc(j.log.slice(-8).join('\n'))}</pre></div>`;
    }).join('')||'<span class="hint">暂无任务</span>';
    const f=await api('/api/files');
    $('#files').innerHTML=f.files.map(x=>`<div class="row" style="margin:3px 0"><span class="mono" style="flex:1">${esc(x.name)}</span>
      <span class="dim">${fmt(x.bytes)}</span><a href="/api/download?f=${encodeURIComponent(x.name)}">下载</a>
      <a href="#" onclick="del('${esc(x.name)}');return false">删除</a></div>`).join('')||'<span class="hint">无</span>';
  }catch(e){}
}
async function del(f){if(!confirm('删除 '+f+' ?'))return; await post('/api/delete',{file:f}); tick();}
loadCfg().then(()=>{           // 支持带参数直达：?q=nginx&repo=library/nginx&filter=1.27&set=1
  const P=new URLSearchParams(location.search);
  if(P.get('set')) toggle('setBox');
  (async()=>{
    if(P.get('q')){ $('#q').value=P.get('q'); await doSearch(1); }
    if(P.get('repo')){ if(P.get('filter')) $('#tFilter').value=P.get('filter'); pickRepo(P.get('repo'),true); }
  })();
});
tick(); setInterval(tick,1200);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    server_version = "imgweb/2.0"

    def log_message(self, *a):
        pass

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
                return self._send(200, PAGE, "text/html; charset=utf-8")
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
                    js = [dict(JOBS[i]) for i in JOB_ORDER[:14]]
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
                for it in items:
                    repo = it["repo"].strip().lstrip("/")
                    host = it.pop("host_override", "") or None
                    if host is None:                       # 镜像名自带 registry（含 . 或 :）→ 默认保持原样
                        parts = repo.split("/")
                        if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
                            host = parts[0]
                            repo = "/".join(parts[1:])
                    if host is None or CFG.get("registry_mode") == "all":
                        host = CFG["registry"]              # Docker Hub 镜像 → 走配置的镜像站
                    fixed.append({**it, "repo_full": repo, "host": host})
                ids = enqueue([{"repo": it["repo_full"], "tag": it["tag"],
                                "display": it.get("display") or it["repo_full"],
                                "os": it.get("os") or CFG["os"], "arch": it.get("arch") or CFG["arch"],
                                "variant": it.get("variant") or "", "host": it["host"]}
                               for it in fixed], cfg_snapshot=snap)
                hosts = sorted({it["host"] for it in fixed})
                return self._send(200, {"ids": ids, "note": "拉取源: " + ", ".join(hosts)})
            if u.path == "/api/delete":
                f = os.path.basename(body.get("file") or "")
                p = os.path.join(CFG.get("outdir") or DEFAULT_OUT, f)
                if f and os.path.isfile(p):
                    os.remove(p)
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
            cli = F.Client(cfg.get("registry") or "registry-1.docker.io", proxy=cfg.get("proxy") or None,
                           insecure=bool(cfg.get("insecure")), timeout=25, proxy_scope=scope)
            data, digest, _ = cli.manifest("library/alpine", "latest")
            n = len(data.get("manifests", []))
            out.append(f"registry {cfg.get('registry')} OK（{n} 个平台清单，{(digest or '')[:19]}…）" if n
                       else f"registry {cfg.get('registry')} OK（单平台镜像）")
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

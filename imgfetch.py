#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
imgfetch.py —— 零依赖（仅标准库）的容器镜像拉取/搜索/打包模块。

特点：
  * 不需要本机安装 docker（直接跟 registry v2 API 说话）
  * 可以直接使用 Docker Hub 镜像站（docker.1ms.run / *.daocloud.io / ...）
  * 支持 HTTP 代理（国内直连 Docker Hub 通常不通）
  * 明确的平台（架构）选择：linux/amd64、linux/arm64/v8 ...
  * 输出 docker-archive（兼容老版本 docker load）或 oci-archive，可 gzip
  * 每层下载后校验 sha256，并校验解压后的 diff_id 与 config.rootfs.diff_ids 一致

CLI 自测：
  python3 imgfetch.py search nginx
  python3 imgfetch.py tags library/nginx 1.27
  python3 imgfetch.py pull alpine:3.20 --arch arm64 --out /tmp/x.tar.gz
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import socket
import ssl
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ACCEPT_ALL = ", ".join([
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])
UA = "imgfetch/2.0 (+stdlib)"


# ------------------------------------------------------------------ errors
class FetchError(Exception):
    pass


class PermanentError(FetchError):
    """重试也没意义的错误（权限/不存在）"""
    pass


class Cancelled(Exception):
    """调用方主动取消（界面上的"取消"按钮）"""
    pass


def kill_response(r):
    """让正在阻塞读取的响应立刻失败：先 shutdown 底层 socket（在 macOS 上 close() 唤不醒阻塞中的
    recv()，shutdown 可以），再 close。找不到 socket 就只 close —— 上层的取消标志仍会在下一次
    读到数据或超时后生效。"""
    if r is None:
        return
    sock = None
    for path in (("fp", "raw", "_sock"), ("fp", "raw"), ("_sock",), ("fp",)):
        obj = r
        try:
            for k in path:
                obj = getattr(obj, k)
        except Exception:
            continue
        if hasattr(obj, "shutdown") and hasattr(obj, "close"):
            sock = obj
            break
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
    try:
        r.close()
    except Exception:
        pass


# ------------------------------------------------------------------ helpers
def parse_image_ref(ref: str, default_host="registry-1.docker.io"):
    """nginx:1.27 / library/nginx / ghcr.io/a/b:c / docker.1ms.run/nginx:1.27
    返回 (host, repository, tag)。Docker Hub 官方镜像补 library/。"""
    ref = ref.strip().rstrip("/")
    tag = "latest"
    if ":" in ref.split("/")[-1]:
        ref, tag = ref.rsplit(":", 1)
    parts = ref.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        host = parts[0]
        repo = "/".join(parts[1:])
    else:
        host = default_host
        repo = ref
        if "/" not in repo:
            repo = "library/" + repo
    return host, repo, tag


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0


def _sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def _ssl_ctx(insecure=False):
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi  # 有就用
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


HUB_HOSTS = ("registry-1.docker.io", "auth.docker.io", "index.docker.io", "hub.docker.com",
             "docker.io", "production.cloudflare.docker.com")


class Client:
    """一个 registry 的连接（含 token 缓存）。

    proxy_scope:
      'auto'  Docker Hub 的域名走代理，其它（镜像站 / ghcr / lscr …）先直连，连不上再自动走代理
      'all'   所有请求都走代理
      'off'   完全直连
    """

    def __init__(self, host, proxy=None, username=None, password=None,
                 insecure=False, timeout=60, log=None, proxy_scope="auto"):
        self.host = host
        self.proxy = proxy or None
        self.proxy_scope = proxy_scope if proxy else "off"
        self.username = username or None
        self.password = password or None
        self.timeout = timeout
        self.log = log or (lambda *a: None)
        ctx = _ssl_ctx(insecure)
        self.direct = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx), urllib.request.ProxyHandler({}))
        if self.proxy:
            self.viaproxy = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ctx),
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        else:
            self.viaproxy = self.direct
        self.on_response = None       # 可选：下载中的响应对象回调（取消时直接 close，让阻塞读立刻中断）
        self._tokens = {}

    def _is_hub(self):
        return self.host in HUB_HOSTS or self.host.endswith(".docker.io")

    def _open(self, url, headers, method="GET"):
        """按 proxy_scope 选 opener；非 Hub 域名直连失败时自动回退代理。"""
        req = urllib.request.Request(url, headers=headers, method=method)
        if self.proxy_scope == "all":
            return self.viaproxy.open(req, timeout=self.timeout)
        if self.proxy_scope == "auto" and self._is_hub():
            return self.viaproxy.open(req, timeout=self.timeout)
        try:
            return self.direct.open(req, timeout=self.timeout)
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            if self.proxy_scope == "auto" and self.proxy:
                self.log(f"直连 {self.host} 失败（{e}），自动改用代理 {self.proxy}")
                return self.viaproxy.open(req, timeout=self.timeout)
            raise

    # -- low level
    def _basic(self):
        if not (self.username and self.password):
            return None
        raw = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _token_for(self, www_auth, scope):
        m = re.match(r'Bearer\s+(.*)', www_auth or "", re.I)
        if not m:
            return None
        params = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        realm = params.get("realm")
        if not realm:
            return None
        q = {k: v for k, v in params.items() if k != "realm"}
        q.setdefault("scope", scope)
        url = realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(q)
        hdr = {"User-Agent": UA}
        b = self._basic()
        if b:
            hdr["Authorization"] = b
        try:
            with self._open(url, hdr) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            cls = PermanentError if e.code in (401, 403, 404) else FetchError
            raise cls(f"取 token 失败 {e.code} ({realm}): {e.read()[:200]!r}")
        tok = data.get("token") or data.get("access_token")
        if not tok:
            raise FetchError(f"token 响应里没有 token: {list(data)[:6]}")
        return tok

    def get(self, path, accept=None, scope=None, retry=2):
        """GET /v2/... ，自动处理 401 Bearer 挑战。返回 (响应对象, bytes)。"""
        url = f"https://{self.host}{path}"
        last = None
        for attempt in range(retry + 1):
            hdr = {"User-Agent": UA}
            if accept:
                hdr["Accept"] = accept
            tok = self._tokens.get(scope)
            if tok:
                hdr["Authorization"] = "Bearer " + tok
            try:
                r = self._open(url, hdr)
                return r, r.read()
            except urllib.error.HTTPError as e:
                if e.code == 401 and scope:
                    www = e.headers.get("WWW-Authenticate", "")
                    last = www
                    if re.match(r"Basic", www or "", re.I):
                        b = self._basic()
                        if b:
                            hdr2 = dict(hdr, Authorization=b)
                            try:
                                r = self._open(url, hdr2)
                                return r, r.read()
                            except urllib.error.HTTPError as e2:
                                raise FetchError(f"HTTP {e2.code} {url} ({e2.read()[:160]!r})")
                        raise FetchError(f"该仓库需要登录（Basic 认证），请在设置里填用户名/密码: {url}")
                    tok = self._token_for(www, scope)
                    if not tok:
                        raise FetchError(f"无法完成认证: {www[:200]}")
                    self._tokens[scope] = tok
                    continue
                cls = PermanentError if e.code in (400, 403, 404) else FetchError
                raise cls(f"HTTP {e.code} {url}: {e.read()[:200]!r}")
            except urllib.error.URLError as e:
                raise FetchError(f"连不上 {self.host}: {e.reason}（可能需要配置代理或换镜像站）")
        raise FetchError(f"认证重试失败: {last}")

    # -- high level
    def manifest(self, repo, ref):
        scope = f"repository:{repo}:pull"
        path = f"/v2/{repo}/manifests/{ref}"
        r, body = self.get(path, accept=ACCEPT_ALL, scope=scope)
        try:
            data = json.loads(body)
        except Exception:
            raise FetchError(f"manifest 不是 JSON（{self.host}/{repo}:{ref}）")
        digest = r.headers.get("Docker-Content-Digest") or "sha256:" + hashlib.sha256(body).hexdigest()
        return data, digest, r.headers.get("Content-Type", "")

    def resolve(self, repo, tag, os_="linux", arch="amd64", variant=None):
        """把 tag 解析成某个平台的 image manifest。返回 dict:
        {manifest, digest, platform, platforms(可选项列表), config_digest}"""
        data, digest, ctype = self.manifest(repo, tag)
        if "manifests" in data:  # manifest list / index
            plats = []
            for m in data["manifests"]:
                p = m.get("platform") or {}
                if p.get("os") == "unknown" or p.get("architecture") == "unknown":
                    continue  # attestation / 证明清单
                plats.append({"os": p.get("os"), "arch": p.get("architecture"),
                              "variant": p.get("variant"), "digest": m["digest"],
                              "size": sum(l.get("size", 0) for l in [m])})
            want = [p for p in plats if p["os"] == os_ and p["arch"] == arch
                    and (not variant or p["variant"] == variant)]
            if not want:
                have = ", ".join(f"{p['os']}/{p['arch']}" + (f"/{p['variant']}" if p["variant"] else "") for p in plats)
                raise PermanentError(f"该 tag 没有 {os_}/{arch}{'/' + variant if variant else ''}；可用平台: {have}")
            # variant 未指定时优先 v8/v7 这类标准变体
            want.sort(key=lambda p: (p["variant"] not in (None, "v8", "v7"), p["variant"] or ""))
            pick = want[0]
            sub, subdigest, _ = self.manifest(repo, pick["digest"])
            return {"manifest": sub, "digest": subdigest, "platform": pick,
                    "platforms": plats, "config_digest": sub.get("config", {}).get("digest")}
        return {"manifest": data, "digest": digest,
                "platform": {"os": data.get("os") or os_, "arch": data.get("architecture") or arch,
                             "variant": data.get("variant")},
                "platforms": None, "config_digest": data.get("config", {}).get("digest")}

    def blob(self, repo, digest, dest_path, progress=None, total=None, retries=3, retry_delay=3.0,
             should_stop=None):
        """下载 blob 到文件并校验 sha256；网络类错误自动重试，支持断点续传，可被取消。"""
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._blob_once(repo, digest, dest_path, progress, total, should_stop)
            except (PermanentError, Cancelled):
                raise
            except Exception as e:
                if attempt > retries:
                    raise FetchError(f"下载失败（共尝试 {retries + 1} 次）{digest[:19]}: {e}")
                wait = max(0.0, retry_delay) * attempt
                if should_stop and should_stop():
                    raise Cancelled("用户取消")
                size = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
                self.log(f"  下载中断（{type(e).__name__}: {str(e)[:70]}）；{wait:.1f}s 后自动重试 "
                         f"[{attempt}/{retries}]" + (f"，从 {human(size)} 处续传" if size else ""))
                time.sleep(wait)

    def _blob_once(self, repo, digest, dest_path, progress=None, total=None, should_stop=None):
        scope = f"repository:{repo}:pull"
        url = f"https://{self.host}/v2/{repo}/blobs/{digest}"
        resume = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
        h = hashlib.sha256()
        if resume:                                     # 已下载部分先计入校验
            with open(dest_path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        hdr = {"User-Agent": UA}
        tok = self._tokens.get(scope)
        if tok:
            hdr["Authorization"] = "Bearer " + tok
        if resume:
            hdr["Range"] = f"bytes={resume}-"
        try:
            r = self._open(url, hdr)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                www = e.headers.get("WWW-Authenticate", "")
                tok = self._token_for(www, scope)
                if not tok:
                    raise PermanentError(f"blob 认证失败: {www[:160]}")
                self._tokens[scope] = tok
                hdr["Authorization"] = "Bearer " + tok
                r = self._open(url, hdr)
            elif e.code == 416:
                if resume:
                    os.remove(dest_path)
                    raise IOError("断点信息失效，重新下载")
                raise FetchError(f"HTTP 416 下载 {digest[:19]}")
            else:
                msg = f"HTTP {e.code} 下载 {digest[:19]}"
                if e.code in (400, 401, 403, 404):     # 权限/不存在 → 重试也没用，直接失败
                    raise PermanentError(msg)
                raise IOError(msg)
        if self.on_response:
            self.on_response(r)                        # 交给调用方，取消时可直接 close 掉
        status = getattr(r, "status", 200)
        if resume and status != 206:                   # 服务端不支持 Range，忽略已下载部分
            self.log("  服务端不支持断点续传，从头下载")
            h = hashlib.sha256()
            resume = 0
        done = resume
        with open(dest_path, "ab" if resume else "wb") as f:
            while True:
                if should_stop and should_stop():
                    raise Cancelled("用户取消")
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if self.on_response:
            self.on_response(None)
        got = "sha256:" + h.hexdigest()
        if got != digest:
            if os.path.exists(dest_path):
                os.remove(dest_path)                   # 内容不对，丢掉重来
            raise IOError(f"层校验失败（将重新下载）期望 {digest[:19]} 实际 {got[:19]}")
        return done


# ------------------------------------------------------------------ 搜索 / 标签（Docker Hub API，走 HTTP 代理）
def _api_get(url, proxy=None, timeout=25, insecure=False, proxy_scope="auto"):
    """Docker Hub API 请求：auto/all 走代理，失败自动回退直连（off 则纯直连）。"""
    ctx = _ssl_ctx(insecure)
    direct = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                         urllib.request.ProxyHandler({}))
    via = direct
    if proxy:
        via = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                          urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    order = [direct] if (proxy_scope == "off" or not proxy) else ([via, direct] if proxy_scope == "auto" else [via])
    last = None
    for op in order:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with op.open(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
    raise last


def search_repos(query, page=1, page_size=25, api_base="https://hub.docker.com", proxy=None,
                 insecure=False, proxy_scope="auto"):
    url = (f"{api_base.rstrip('/')}/v2/search/repositories/?query={urllib.parse.quote(query)}"
           f"&page={page}&page_size={page_size}")
    d = _api_get(url, proxy=proxy, insecure=insecure, proxy_scope=proxy_scope)
    out = []
    for r in d.get("results", []):
        name = r.get("repo_name", "")
        out.append({
            "name": name,
            "pull_ref": name if "/" in name else "library/" + name,
            "official": bool(r.get("is_official")),
            "desc": (r.get("short_description") or "").strip(),
            "stars": r.get("star_count", 0),
            "pulls": r.get("pull_count", 0),
        })
    return {"count": d.get("count", 0), "page": page, "results": out}


def repo_tags(repo_full, name_filter="", page=1, page_size=50, ordering="last_updated",
              api_base="https://hub.docker.com", proxy=None, insecure=False, proxy_scope="auto"):
    parts = repo_full.split("/", 1)
    ns, name = (parts[0], parts[1]) if len(parts) > 1 else ("library", parts[0])
    q = {"page": page, "page_size": page_size, "ordering": ordering}
    if name_filter:
        q["name"] = name_filter
    url = (f"{api_base.rstrip('/')}/v2/namespaces/{urllib.parse.quote(ns)}/repositories/"
           f"{urllib.parse.quote(name)}/tags/?" + urllib.parse.urlencode(q))
    d = _api_get(url, proxy=proxy, insecure=insecure, proxy_scope=proxy_scope)
    out = []
    for t in d.get("results", []):
        plats = []
        for i in t.get("images", []):
            a = i.get("architecture")
            if not a or a == "unknown":
                continue
            plats.append({"os": i.get("os", "linux"), "arch": a, "variant": i.get("variant"),
                          "size": i.get("size", 0)})
        plats.sort(key=lambda p: (p["arch"], p["variant"] or ""))
        out.append({"tag": t.get("name"), "updated": (t.get("last_updated") or "")[:19],
                    "size": t.get("full_size", 0), "platforms": plats})
    return {"count": d.get("count", 0), "page": page, "results": out}


# ------------------------------------------------------------------ 落盘成 tar
def _write_tar(out_path, entries, gz):
    """entries: 有序列表 [(arcname, kind, payload)]
       kind='file' payload=磁盘路径 / kind='bytes' payload=bytes"""
    mode = "w:gz" if gz else "w"
    tf = tarfile.open(out_path, mode, format=tarfile.GNU_FORMAT)
    try:
        for arcname, kind, payload in entries:
            if kind == "bytes":
                info = tarfile.TarInfo(arcname)
                info.size = len(payload)
                info.mtime = int(time.time())
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(payload))
            else:
                size = os.path.getsize(payload)
                info = tarfile.TarInfo(arcname)
                info.size = size
                info.mtime = int(os.path.getmtime(payload))
                info.mode = 0o644
                with open(payload, "rb") as f:
                    tf.addfile(info, f)
    finally:
        tf.close()


def pull(repo_full, tag, out_path, platform_os="linux", platform_arch="amd64", variant=None,
         host="registry-1.docker.io", proxy=None, username=None, password=None, insecure=False,
         repo_tag=None, display_repo=None, gz=True, log=None, progress=None,
         timeout=60, keep_tar=False, proxy_scope="auto", retries=3, retry_delay=3.0, should_stop=None,
         on_response=None):
    """拉取远端镜像并落成 tar/tar.gz（本地无需 docker）。

    display_repo: 归档里 RepoTags 用的仓库名（默认去掉 Docker Hub 的 library/ 前缀，
                  与 docker pull 的显示名一致；compose 里写的也是这个名字）。
    始终输出 docker-archive（老 docker / podman / containerd 都能 load）。
    返回 dict(file, bytes, sha256, platform, layers, config, digest)
    """
    log = log or (lambda *a: None)

    def ck(where=""):
        if should_stop and should_stop():
            raise Cancelled("用户取消" + (f"（{where}）" if where else ""))

    ck("开始")
    cli = Client(host, proxy=proxy, username=username, password=password,
                 insecure=insecure, timeout=timeout, log=log, proxy_scope=proxy_scope)
    cli.on_response = on_response
    log(f"registry = {host}   镜像 = {repo_full}:{tag}")
    res = None
    for _a in range(1, retries + 2):                      # 解析 manifest 也重试（网络抖动很常见）
        try:
            res = cli.resolve(repo_full, tag, os_=platform_os, arch=platform_arch, variant=variant)
            break
        except PermanentError:
            raise
        except Exception as e:
            if _a > retries:
                raise
            log(f"解析 manifest 失败（{type(e).__name__}: {str(e)[:70]}）；{retry_delay * _a:.1f}s 后重试 [{_a}/{retries}]")
            time.sleep(retry_delay * _a)
    man, plat = res["manifest"], res["platform"]
    log(f"选中平台 = {plat['os']}/{plat['arch']}" + (f"/{plat['variant']}" if plat["variant"] else "")
        + f"   digest = {res['digest']}")
    if not gz:
        out_path = out_path

    with tempfile.TemporaryDirectory(prefix="imgfetch-") as tmp:
        # 1) config blob
        cfg_digest = man["config"]["digest"]
        cfg_path = os.path.join(tmp, "config.json")
        log(f"下载 config {cfg_digest[:19]}...")
        ck("config")
        cli.blob(repo_full, cfg_digest, cfg_path, progress=None, total=man["config"].get("size"),
                 retries=retries, retry_delay=retry_delay, should_stop=should_stop)
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        diff_ids = cfg.get("rootfs", {}).get("diff_ids", [])
        arch_in_cfg = (cfg.get("architecture"), cfg.get("os"))
        log(f"config 声明平台 = {arch_in_cfg[1]}/{arch_in_cfg[0]}   层数 = {len(man['layers'])}")
        if arch_in_cfg[0] and arch_in_cfg[0] != platform_arch:
            log(f"注意：config 里的架构({arch_in_cfg[0]})与所选({platform_arch})不一致")

        # 2) 逐层下载 + 校验 + 解压
        entries_layer = []   # (arcname, path)
        layer_ids = []
        total_bytes = sum(l.get("size", 0) for l in man["layers"])
        log(f"开始下载 {len(man['layers'])} 层，约 {human(total_bytes)}（压缩后）")
        for idx, layer in enumerate(man["layers"], 1):
            ck(f"第 {idx}/{len(man['layers'])} 层")
            dg = layer["digest"]
            mt = layer.get("mediaType", "")
            blob_file = os.path.join(tmp, f"blob{idx}")
            t0 = time.time()
            last = [0.0]

            def cb(done, total, idx=idx, last=last, t0=t0):
                now = time.time()
                if progress and (now - last[0] > 0.25 or done == total):
                    last[0] = now
                    sp = done / max(1e-6, now - t0)
                    progress(idx, len(man["layers"]), done, layer.get("size", 0), sp)

            cli.blob(repo_full, dg, blob_file, progress=cb, total=layer.get("size"),
                     retries=retries, retry_delay=retry_delay, should_stop=should_stop)
            is_gz = mt.endswith("gzip") or open(blob_file, "rb").read(2) == b"\x1f\x8b"
            if is_gz:
                out_l = os.path.join(tmp, f"layer{idx}.tar")
                h = hashlib.sha256()
                with gzip.open(blob_file, "rb") as fi, open(out_l, "wb") as fo:
                    while True:
                        ck("解压")
                        b = fi.read(1 << 20)
                        if not b:
                            break
                        h.update(b)
                        fo.write(b)
                os.remove(blob_file)
                diff = "sha256:" + h.hexdigest()
            else:
                out_l = blob_file
                diff = dg
            if idx - 1 < len(diff_ids) and diff_ids[idx - 1] != diff:
                raise FetchError(f"第 {idx} 层 diff_id 不匹配：config={diff_ids[idx-1]} 实际={diff}")
            lid = diff.split(":")[1]
            layer_ids.append(lid)
            entries_layer.append((f"{lid}/layer.tar", "file", out_l))
            log(f"  [{idx}/{len(man['layers'])}] {dg[7:19]}  {human(layer.get('size', 0))}  "
                f"{time.time()-t0:.1f}s  解压后 diff={diff[7:19]}")

        # 3) 组装 docker-archive（老 docker / podman / containerd 都能 load）
        ck("打包")
        tag_out = repo_tag or tag
        name_out = display_repo or (repo_full[8:] if repo_full.startswith("library/") else repo_full)
        log(f"打包 -> {os.path.basename(out_path)}（docker-archive{'，gzip' if gz else ''}），镜像名 {name_out}:{tag_out}")
        cfg_id = cfg_digest.split(":")[1]
        manifest_json = json.dumps([{
            "Config": f"{cfg_id}.json",
            "RepoTags": [f"{name_out}:{tag_out}"],
            "Layers": [f"{lid}/layer.tar" for lid in layer_ids],
        }], ensure_ascii=False).encode()
        repositories = json.dumps({name_out: {tag_out: layer_ids[-1] if layer_ids else ""}}).encode()
        entries = entries_layer + [
            (f"{cfg_id}.json", "file", cfg_path),
            ("manifest.json", "bytes", manifest_json),
            ("repositories", "bytes", repositories),
        ]
        _write_tar(out_path, entries, gz)

    size = os.path.getsize(out_path)
    digest = _sha256_file(out_path)
    log(f"完成 {os.path.basename(out_path)}  {human(size)}  sha256={digest[:16]}...")
    return {"file": out_path, "bytes": size, "sha256": digest,
            "platform": f"{plat['os']}/{plat['arch']}" + (f"/{plat['variant']}" if plat["variant"] else ""),
            "images": [f"{repo_full}:{repo_tag or tag}"], "layers": len(layer_ids),
            "config": cfg_id}


# ------------------------------------------------------------------ CLI（自测用）
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    proxy = os.environ.get("https_proxy") or os.environ.get("http_proxy")
    if cmd == "search" and len(sys.argv) > 2:
        d = search_repos(sys.argv[2], proxy=proxy)
        print(f"命中 {d['count']}，前 {len(d['results'])}:")
        for r in d["results"]:
            print(f"  {r['pull_ref']:<32} ★{r['stars']:<7} {'[官方]' if r['official'] else '      '} {r['desc'][:60]}")
    elif cmd == "tags" and len(sys.argv) > 2:
        repo = sys.argv[2]
        flt = sys.argv[3] if len(sys.argv) > 3 else ""
        d = repo_tags(repo, name_filter=flt, page_size=15, proxy=proxy)
        print(f"{repo} 匹配 {d['count']} 个 tag:")
        for t in d["results"]:
            ps = ", ".join(f"{p['arch']}{'/'+p['variant'] if p['variant'] else ''}:{p['size']/1048576:.0f}MB" for p in t["platforms"])
            print(f"  {t['tag']:<24} {t['updated'][:10]}  {ps}")
    elif cmd == "pull" and len(sys.argv) > 2:
        ref = sys.argv[2]
        host = "registry-1.docker.io"
        arch = "amd64"
        out = None
        args = sys.argv[3:]
        for i, a in enumerate(args):
            if a == "--arch":
                arch = args[i + 1]
            if a == "--host":
                host = args[i + 1]
            if a == "--out":
                out = args[i + 1]
        repo, tag = ref.rsplit(":", 1) if ":" in ref.split("/")[-1] else (ref, "latest")
        if "/" not in repo:
            repo = "library/" + repo
        out = out or f"/tmp/{repo.replace('/', '_')}_{tag}_{arch}.tar.gz"
        pull(repo, tag, out, platform_arch=arch, host=host, proxy=proxy, log=lambda *a: print(*a, flush=True))
    else:
        print(__doc__)

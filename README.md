# 镜像搜索 / 拉取 / 打包工具

给内网/离线部署用：**搜镜像 → 选版本 → 选架构 → 导出 tar.gz**，最后在内网 `docker load`。
**本机不需要安装 docker**（直接跟 registry v2 说话），只用 Python 标准库，无第三方依赖。

## 文件结构

    app.py           后端：HTTP 服务 + 接口（/api/*）+ 任务队列；页面只做静态文件转发
    imgfetch.py      核心：registry v2 客户端（搜索/标签/选平台/下载/校验/打包）
    static/          前端（改样式只动这里，不用碰 Python）
      index.html     页面骨架
      app.css        样式（含防外部 CSS 覆盖的加固）
      app.js         交互逻辑
    start.command    macOS 双击启动
    start.bat        Windows 双击启动
    config.json      本机设置（代理/镜像源/平台…；已 gitignore，不要提交）
    output/          导出的 tar.gz + SHA256SUMS（已 gitignore）

static/ 下的文件每次请求都重新读取，**改 HTML/CSS/JS 不需要重启服务**（改了 app.py 才要重启）；
页面里的 `?v=__V__` 由后端按 css/js 内容哈希动态替换，且响应带 `Cache-Control: no-store`，
所以不会再出现"改了样式却看到旧页面"。

## 启动

| 系统 | 怎么启动 |
|---|---|
| macOS | 终端里 `python3 ~/docker-image-tool/app.py`，或 Finder 双击 `start.command` |
| Windows | 双击 `start.bat`（先装 Python 3.9+，安装时勾选 **Add python.exe to PATH**） |
| 通用 | `python3 app.py --port 8799`，浏览器开 http://127.0.0.1:8799 |

参数：`--port 8799`（换端口）、`--host 127.0.0.1`、`--open`（启动后自动开浏览器）。
停止：关掉窗口，或 `kill $(lsof -t -iTCP:8799 -sTCP:LISTEN)`。

## 怎么用

**最快的路径**：知道镜像名就直接在顶部「直接拉取」框里粘贴（`nginx:1.27.3` 每行一个，或填 compose 路径点「读取 compose」），
点「拉取」即按当前平台入队。不知道确切版本才走下面的搜索。

1. **搜索**：输入 `nginx`、`mysql`、`siyuan`…（走 Docker Hub 搜索 API，见设置里的 API 地址）
2. **看版本**：点搜索结果里的「选」→ 列出该仓库所有 tag，可按 tag 关键字过滤、可翻页
3. **选架构**：每个 tag 后面直接标出可用架构与大小（amd64 / arm64/v8 / arm/v7 …）。
   设置里的「默认平台」决定「拉取」按钮导哪个架构；该 tag 没有这个架构时按钮置灰。
4. **导出**：点「拉取 amd64」入队，后台下载并打包 → 右侧「已生成的包」里下载，
   文件名如 `nginx_1.27.3_linux-amd64.tar.gz`，同时追加到 `output/SHA256SUMS`。
5. **批量**：把多行镜像（`nginx:1.27.3` 每行一个）粘进批量框，或填 `docker-compose.yml`
   路径点「读取 compose 里的镜像」，再「按当前平台全部入队」。

同一 tag 一次导多个架构时，归档内的镜像名会自动加架构后缀（`alpine:3.20-amd64`），
避免 load 时互相覆盖；不想要后缀可在设置里关掉「归档内标签加架构后缀」。

## 任务管理（重试 / 取消）

- **重试**：失败的任务卡片上有「重试这个任务」，任务区标题有「重试失败的全部」。设置里的
  「下载失败自动重试次数」默认 2（0 = 只用手动按钮），间隔按次数递增；中断后**断点续传**
  （HTTP Range），不会重下已完成的字节；tag 不存在、无权限（404/403）会立刻失败、不做无谓重试。
- **取消**：排队中或运行中的任务都有「取消」按钮，任务区标题还有「全部取消」。排队中的立即移除；
  运行中的会在下一个检查点中断（层与层之间、下载读循环、解压循环都会检查），半成品 tar 会被删掉，
  output/ 与 SHA256SUMS 不留垃圾。取消时直接掐掉底层 socket，卡住的下载也能秒停。

## 设置（存在 config.json，界面里改完自动保存）

- **拉取来源**：二选一——「镜像站（国内加速，默认 `docker.1ms.run`）」或「官方 registry（需代理）」。
  切换立即生效，顶部会一直显示当前拉取源，不会再出现「以为切了官方结果还是镜像站」。
- **换源范围**：只给 Docker Hub 镜像换源（第三方 registry 保持原样）/ 全部走该地址。
- **搜索 / 标签 API 地址**：默认 `https://hub.docker.com`。搜索只对 Docker Hub 有效。
- **HTTP 代理 + 代理用在哪**：默认「自动」= Docker Hub 域名走代理，镜像站/ghcr 直连，
  直连失败再回退代理。**重要**：镜像站只服务国内 IP，走代理访问会 404，别设成「全部走代理」。
- **私有仓库用户名/密码**：Basic + Bearer token，Harbor 一类私有库用得上。
- **跳过 TLS 校验**：自建 registry 自签证书时用。
- **默认平台 / 压缩 / 输出目录**：默认 `linux/amd64`、`tar.gz`、`./output`。
  导出格式固定为 `docker-archive`（老 docker / podman / containerd 都能 load）。

## 拷贝到另一台机器（比如 Windows）

只拷 `app.py`、`imgfetch.py` 两个文件就够（`config.json`、`output/` 会自动生成）。

- **别直接拷 `config.json`**：里面的代理是原机器的地址（如 `127.0.0.1:18080`），
  在新机器上不存在。删掉让它重新自动探测，或在新机器界面上改。
- Windows 上如果搜索一直转圈/失败：检查「搜索 API 地址」能不能访问（Docker Hub 国内需要代理），
  拉取镜像本身走镜像站不需要代理。

## 在内网导入（目标机）

```bash
# 校验（macOS: shasum -a 256 -c SHA256SUMS）
sha256sum -c SHA256SUMS
# 导入（docker 的 load 会自动识别 .tar.gz）
docker load -i nginx_1.27.3_linux-amd64.tar.gz
# Windows PowerShell 一样：docker load -i .\nginx_1.27.3_linux-amd64.tar.gz
```
compose 里给每个 service 加 `pull_policy: never`，离线时镜像缺失会立刻报错而不是傻等。
注意：导出的只是镜像，**不含数据卷数据**，数据要另外备份。

## 局限

- 搜索/版本列表只对 Docker Hub 有效（ghcr/lscr 没有统一搜索 API，用批量框直接填镜像名）。
- 私有仓库目前是 Basic + Bearer token 方式，OAuth 型（老 ECR 等）可能需手动处理。
- 服务无鉴权，只绑 127.0.0.1；不要 `--host 0.0.0.0` 暴露到网上。

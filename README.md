# 镜像搜索 / 拉取 / 打包工具

给内网/离线部署用：**搜镜像 → 选版本 → 选架构 → 导出 tar.gz**，最后在内网 `docker load`。
**本机不需要安装 docker**（直接跟 registry v2 说话），只用 Python 标准库，无第三方依赖。

## 启动

| 系统 | 怎么启动 |
|---|---|
| macOS | 终端里 `python3 ~/docker-image-tool/app.py`，或 Finder 双击 `start.command` |
| Windows | 双击 `start.bat`（先装 Python 3.9+，安装时勾选 **Add python.exe to PATH**） |
| 通用 | `python3 app.py --port 8799`，浏览器开 http://127.0.0.1:8799 |

参数：`--port 8799`（换端口）、`--host 127.0.0.1`、`--open`（启动后自动开浏览器）。
停止：关掉窗口，或 `kill $(lsof -t -iTCP:8799 -sTCP:LISTEN)`。

## 怎么用

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

## 设置（存在 config.json，界面里改完点保存）

- **搜索 / 标签 API 地址**：默认 `https://hub.docker.com`。搜索只对 Docker Hub 有效。
- **拉取 registry（镜像站）**：默认 `docker.1ms.run`。国内直连 Docker Hub 基本不通，
  用镜像站；`ghcr.io` / `lscr.io` / 私有 Harbor 直接填对应地址。
- **换源范围**：只给 Docker Hub 镜像换源（第三方 registry 保持原样）/ 全部走该地址。
- **HTTP 代理 + 代理用在哪**：默认「自动」= Docker Hub 域名走代理，镜像站/ghcr 直连，
  直连失败再回退代理。**重要**：镜像站只服务国内 IP，走代理访问会 404，别设成「全部走代理」。
- **私有仓库用户名/密码**：Basic + Bearer token，Harbor 一类私有库用得上。
- **跳过 TLS 校验**：自建 registry 自签证书时用。
- **默认平台 / 归档格式 / 输出 / 输出目录**：默认 `linux/amd64`、`docker-archive`（老 docker
  也能 load；`oci-archive` 需要 docker 25+）、`tar.gz`、`./output`。

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

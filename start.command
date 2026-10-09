#!/bin/bash
# macOS / Linux：双击即可（Finder 里双击会在终端里启动服务）
cd "$(dirname "$0")"
exec python3 app.py --port 8799 --open

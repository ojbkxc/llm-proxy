#!/bin/bash
# 合并版代理启动/重启脚本（天翼云，双区统一）
# 用法：bash /opt/llm-proxy/start_proxy.sh
cd /opt/llm-proxy
pkill -f 'qoder_proxy.py --serve' 2>/dev/null
sleep 1
QODER_PROXY_CHECKIN_AT=10:30 \
QODER_PROXY_ADMIN_PASSWORD='mzyxc8520#' \
setsid nohup /usr/bin/python3 -u qoder_proxy.py \
  --serve --log /var/log/llm-proxy.log </dev/null >/dev/null 2>&1 &
echo "started proxy (8791, cn+intl)"
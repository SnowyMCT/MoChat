#!/usr/bin/env bash
# ============================================================================
#  MChat 服务端 —— Debian 12 加固部署脚本（幂等，可重复执行）
#
#  在服务器上，先把开发机的 server/ 目录整个传上来（含本脚本），然后：
#
#      cd ~/mchat-server
#      sudo DOMAIN=chat.example.com EMAIL=you@example.com bash deploy-debian12.sh
#
#  可覆盖的变量（全部有默认，可省略）：
#      DOMAIN             你的域名。留空 = 只装 HTTP 反向代理，
#                         仅适用于「内网」或「前面已有别的 HTTPS 终结/Tailscale」的场景。
#      EMAIL              Let's Encrypt 证书到期通知邮箱（有 DOMAIN 时使用）。
#      PORT               服务端在本机的监听端口，默认 18765（不对外暴露）。
#      DATA_DIR           数据目录（库 + 附件），默认 /var/lib/mchat。
#      ENABLE_ENCRYPTION  1 = 开启磁盘加密（默认），0 = 关闭。
#
#  重要：本脚本由人工编写，未在真实 Debian 12 上跑过 CI。执行前请通读一遍；
#        关键步骤都做了回显和失败即停（set -e），出错不会半途静默继续。
#        它不会修改 SSH 配置（避免把你锁在外面）—— 见脚本末尾的「还该手工做的」。
# ============================================================================
set -euo pipefail

DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
PORT="${PORT:-18765}"
DATA_DIR="${DATA_DIR:-/var/lib/mchat}"
APP_DIR="${APP_DIR:-/opt/mchat}"
KEY_DIR="${KEY_DIR:-/etc/mchat}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/mchat}"
ENABLE_ENCRYPTION="${ENABLE_ENCRYPTION:-1}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

log()  { printf '\033[1;32m[mchat-deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[mchat-deploy]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[mchat-deploy] 错误：%s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash $0"
[ -f "$SRC_DIR/mchat_server.py" ] || die "当前目录找不到 mchat_server.py，请在 server/ 目录里运行本脚本"

SERVER_NAME="${DOMAIN:-_}"
KEYARG=""
if [ "$ENABLE_ENCRYPTION" = "1" ]; then
  KEYARG="--storage-key-file ${KEY_DIR}/storage.key"
fi

# ---------------------------------------------------------------------------
log "1/9 安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 sqlite3 rsync nginx ufw fail2ban \
  ca-certificates curl unattended-upgrades
if [ "$ENABLE_ENCRYPTION" = "1" ]; then
  apt-get install -y -qq python3-cryptography
fi
python3 - <<'PY' || die "Python 版本太老，MChat 需要 >= 3.9"
import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY
log "   python3 = $(python3 -V 2>&1)"

# ---------------------------------------------------------------------------
log "2/9 建系统账号与目录"
id mchat >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin mchat
mkdir -p "$APP_DIR" "$DATA_DIR" "$KEY_DIR" "$BACKUP_DIR"
for f in mchat_server.py selftest.py PROTOCOL.md README.md DEPLOY.md; do
  [ -f "$SRC_DIR/$f" ] && cp -f "$SRC_DIR/$f" "$APP_DIR/"
done
chmod 755 "$APP_DIR"
chown -R mchat:mchat "$DATA_DIR"
chmod 750 "$DATA_DIR"
chown root:root "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
python3 -m py_compile "$APP_DIR/mchat_server.py" || die "mchat_server.py 传坏了（py_compile 失败）"
log "   程序 -> $APP_DIR    数据 -> $DATA_DIR"

# ---------------------------------------------------------------------------
if [ "$ENABLE_ENCRYPTION" = "1" ]; then
  log "3/9 磁盘加密密钥（消息/附件密文落盘）"
  if [ ! -s "$KEY_DIR/storage.key" ]; then
    head -c 32 /dev/urandom > "$KEY_DIR/storage.key"
    log "   已生成新密钥 $KEY_DIR/storage.key —— 请立即与备份分开另存一份！丢了数据解不开"
  else
    log "   已存在密钥，保持不变"
  fi
  chown root:mchat "$KEY_DIR/storage.key"
  chmod 640 "$KEY_DIR/storage.key"
else
  log "3/9 跳过磁盘加密（ENABLE_ENCRYPTION=0）—— 磁盘上是明文，请自行确认风险"
fi

# ---------------------------------------------------------------------------
log "4/9 跑端到端自检（226 条断言，不碰你的数据目录）"
if sudo -u mchat python3 "$APP_DIR/selftest.py" 2>&1 | tail -3; then
  log "   自检已执行（上一行须为 226/226 通过）"
else
  warn "   自检非零退出/非全绿，先排查再继续上线"
fi

# ---------------------------------------------------------------------------
log "5/9 安装 systemd 单元（含沙箱加固）"
cat > /etc/systemd/system/mchat-server.service <<EOF
[Unit]
Description=MChat server (zero-dep HTTP/JSON chat backend + long-poll)
Documentation=file://${APP_DIR}/PROTOCOL.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mchat
Group=mchat
WorkingDirectory=${APP_DIR}
Environment=MCHAT_DATA=${DATA_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 ${APP_DIR}/mchat_server.py \\
          --host 127.0.0.1 \\
          --port ${PORT} \\
          --data \${MCHAT_DATA} \\
          --token-ttl-days 30 \\
          --max-upload-mb 32 \\
          --max-connections 150 \\
          --login-rate 12 \\
          --lock-after 5 \\
          --lock-seconds 60 \\
          --trust-proxy ${KEYARG}

Restart=always
RestartSec=2
KillSignal=SIGTERM
TimeoutStopSec=20
KillMode=mixed
LimitNOFILE=8192

StandardOutput=journal
StandardError=journal
SyslogIdentifier=mchat-server

# --- 沙箱加固 ---
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictNamespaces=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
ReadWritePaths=${DATA_DIR}

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

# ---------------------------------------------------------------------------
log "6/9 配置 nginx 反向代理 + 限流"
mkdir -p /etc/nginx/snippets
cat > /etc/nginx/snippets/mchat-proxy.conf <<EOF
proxy_pass http://127.0.0.1:${PORT};
proxy_http_version 1.1;
proxy_set_header Host \$host;
proxy_set_header X-Real-IP \$remote_addr;
proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
# ★ 长轮询：读超时必须 > 挂起时间（客户端 20s / 服务端 30s），否则 /api/sync 全 504
proxy_read_timeout 120s;
proxy_send_timeout 120s;
proxy_connect_timeout 10s;
proxy_buffering off;
EOF

cat > /etc/nginx/conf.d/mchat.conf <<EOF
# 登录/注册按 IP 限速（服务端自身也有 --login-rate，这里是反代层再叠一层）
limit_req_zone \$binary_remote_addr zone=mchat_auth:10m rate=10r/m;

server {
    listen 80;
    server_name ${SERVER_NAME};
    client_max_body_size 40m;          # 必须 >= 服务端 --max-upload-mb

    location = /api/login    { limit_req zone=mchat_auth burst=5 nodelay; include /etc/nginx/snippets/mchat-proxy.conf; }
    location = /api/register { limit_req zone=mchat_auth burst=5 nodelay; include /etc/nginx/snippets/mchat-proxy.conf; }
    location /               { include /etc/nginx/snippets/mchat-proxy.conf; }
}
EOF

# 默认站点若占用 80 且 server_name 冲突，先关掉它
if [ -e /etc/nginx/sites-enabled/default ]; then
  rm -f /etc/nginx/sites-enabled/default
  log "   已停用 nginx 默认站点（避免抢占 80 端口）"
fi
nginx -t || die "nginx 配置检查失败"
systemctl reload nginx

# ---------------------------------------------------------------------------
log "7/9 防火墙（ufw）：只开 22/80/443"
ufw allow OpenSSH >/dev/null || true
ufw allow 80/tcp   >/dev/null || true
ufw allow 443/tcp  >/dev/null || true
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw --force enable >/dev/null
ufw status verbose | sed 's/^/   /'

# ---------------------------------------------------------------------------
log "8/9 fail2ban（针对 nginx 上的登录爆破）"
cat > /etc/fail2ban/filter.d/mchat-nginx.conf <<'EOF'
[Definition]
failregex = ^<HOST> - - \[[^\]]*\] "POST /api/(login|register) HTTP/[0-9.]+" (401|403|429) 
ignoreregex =
EOF
cat > /etc/fail2ban/jail.d/mchat.local <<'EOF'
[mchat-nginx]
enabled  = true
filter   = mchat-nginx
port     = http,https
logpath  = /var/log/nginx/access.log
maxretry = 10
findtime = 600
bantime  = 3600
EOF
systemctl enable fail2ban >/dev/null 2>&1 || true
systemctl restart fail2ban || warn "fail2ban 重启失败，检查 systemctl status fail2ban"

# ---------------------------------------------------------------------------
log "9/9 自动安全更新 + 每日备份"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

cat > /usr/local/bin/mchat-backup.sh <<'EOF'
#!/bin/sh
set -eu
DB=/var/lib/mchat/mchat.db
FB=/var/lib/mchat/files
DEST=/var/backups/mchat
mkdir -p "$DEST"
sqlite3 "$DB" ".backup '$DEST/mchat-$(date +%F).db'"
rsync -a --delete "$FB/" "$DEST/files/"
find "$DEST" -name 'mchat-*.db' -mtime +14 -delete
EOF
chmod 700 /usr/local/bin/mchat-backup.sh

cat > /etc/cron.d/mchat-backup <<'EOF'
30 3 * * * root /usr/local/bin/mchat-backup.sh
EOF
chmod 644 /etc/cron.d/mchat-backup

# ---------------------------------------------------------------------------
log "启动服务"
systemctl enable --now mchat-server
sleep 1
systemctl --no-pager --lines=0 status mchat-server || true

echo
log "本机健康检查："
curl -s "http://127.0.0.1:${PORT}/api/health" || warn "health 没通，看 journalctl -u mchat-server -n 50"
echo

if [ -n "$DOMAIN" ]; then
  log "申请 Let's Encrypt 证书（$DOMAIN）"
  apt-get install -y -qq certbot python3-certbot-nginx
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
          -m "${EMAIL:-admin@$DOMAIN}" --redirect || \
    warn "certbot 失败（DNS 是否已解析到本机？80 端口是否放行？）；HTTP 反代仍在跑"
  systemctl reload nginx || true
  curl -sI "https://$DOMAIN/api/health" | head -1 || true
else
  warn "未提供 DOMAIN —— 只装了 HTTP。公网明文传输下密码/token 是可被嗅探的！"
  warn "请改用：有域名（跑本脚本带 DOMAIN=）／Tailscale 等私有网络／前面另有 HTTPS 终结。"
fi

# ---------------------------------------------------------------------------
echo
log "================ 上线后要做的事 ================"
cat <<'EOM'
1) 拿注册邀请码（★ 那一行）：
     journalctl -u mchat-server --no-pager | grep 邀请码

2) 从外部验证端口没裸奔（应连不上）：
     curl --max-time 5 http://<公网IP>:PORT/api/health     # 期望失败
     curl -sI https://<域名>/api/health | head -1           # 期望 200

3) 建账号后，建议彻底关掉注册（编辑单元 ExecStart 追加 --no-register）：
     systemctl edit mchat-server        # 或用 sed 追加后 daemon-reload + restart

4) 【手工，脚本故意不动】加固 SSH：
     - 先用密钥确认能登，再关密码登录；
     - /etc/ssh/sshd_config: PermitRootLogin no / PasswordAuthentication no / KbdInteractiveAuthentication no
     - systemctl reload ssh；改完务必另开一个窗口验证还能登，别把自己锁在外面。

5) 加密密钥 /etc/mchat/storage.key 与备份分开保管；备份里有令牌与消息，按机密对待。

6) 客户端「服务器地址」只填根地址：https://chat.example.com
     （不要带 /api、不要带路径；非本机用 http:// 时客户端会红字警告明文）
EOM
log "完成。日志：journalctl -u mchat-server -f"

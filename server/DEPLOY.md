# MChat 部署 runbook（照着敲一遍就能上线）

这份是**动手清单**：一台干净的 Linux 服务器 → 能被外网的客户端连上并聊天。
原理和参数解释在 `README.md`（部署与运维）/ `PROTOCOL.md`（协议），这里只给命令。

假设：域名 `chat.example.com` 已经解析到这台服务器；服务器能出网（装 nginx、签证书）。
把下面所有 `<...>` 换成你自己的值。**全程不用 pip，不用 venv。**

---

## 0. 一句话版的架构

```
客户端 ──https://chat.example.com──> nginx ──http://127.0.0.1:8765──> mchat_server.py
                                                                            │
                                                             /var/lib/mchat/mchat.db
                                                             /var/lib/mchat/files/<附件id>
```

服务端只监听 127.0.0.1（不暴露 8765），HTTPS 由 nginx 终结 —— 这是最关键的一条：
**裸 HTTP 时密码和令牌在网线上是明文。**

---

## 1. 传代码

开发机上（`server/` 目录就是全部）：

```bash
rsync -av server/{mchat_server.py,PROTOCOL.md,selftest.py,mchat-server.service,DEPLOY.md,README.md} \
      user@chat.example.com:/tmp/mchat/
```

也可以直接 scp 整个 `server/`（会多带 `data/`，注意别把本地测试库一起传上去）。

## 2. 建账号 + 目录

```bash
sudo apt update && sudo apt install -y python3 sqlite3 rsync     # Debian/Ubuntu；RHEL 用 dnf
python3 -V                                                       # 需要 >= 3.9

sudo mkdir -p /opt/mchat /var/lib/mchat
sudo cp /tmp/mchat/*.py /tmp/mchat/*.md /tmp/mchat/*.service /opt/mchat/
sudo useradd --system --home /var/lib/mchat --shell /usr/sbin/nologin mchat || true
sudo chown -R mchat:mchat /var/lib/mchat
sudo chmod 755 /opt/mchat
python3 -m py_compile /opt/mchat/mchat_server.py                 # 确认没传坏
```

## 3. 先手动跑一次，把自检和协议都验一遍

```bash
# 自检：自己挑端口起真服务端跑 226 条断言，跑完删临时目录，不碰你的数据
sudo -u mchat python3 /opt/mchat/selftest.py | tail -3          # 必须是 "226/226 通过"

# 手动起一次，看看启动日志（★ 那一行就是注册邀请码）
sudo -u mchat python3 /opt/mchat/mchat_server.py \
     --host 127.0.0.1 --port 8765 --data /var/lib/mchat
# 另开一个终端：
curl -s http://127.0.0.1:8765/api/health
# Ctrl+C 退出，应看到 "[mchat] 已退出，数据已落盘（/var/lib/mchat）"
```

## 4. systemd 托管

```bash
sudo cp /tmp/mchat/mchat-server.service /etc/systemd/system/
sudo nano /etc/systemd/system/mchat-server.service     # 按文件里的「改这里」核对 3 处
sudo systemctl daemon-reload
sudo systemctl enable --now mchat-server
systemctl status mchat-server --no-pager
journalctl -u mchat-server -n 30 --no-pager            # 看启动日志 + ★ 邀请码
```

单元文件里已经给好的默认参数（可直接用）：

```ini
ExecStart=/usr/bin/python3 /opt/mchat/mchat_server.py
          --host 127.0.0.1 --port 8765 --data /var/lib/mchat
          --token-ttl-days 30 --max-upload-mb 32 --max-connections 150
          --login-rate 12 --lock-after 5 --trust-proxy
```

* `--trust-proxy` 只有在**反代后面**才对；直接对公网监听时把它删掉。
* 1GB 内存的小机器：`--max-connections 100 --max-upload-mb 16`
  （上传是整段进内存的，服务端启动时会自己算给你看）。

## 5. nginx + HTTPS

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

`/etc/nginx/conf.d/mchat.conf`：

```nginx
server {
    listen 443 ssl http2;
    server_name chat.example.com;

    ssl_certificate     /etc/letsencrypt/live/chat.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/chat.example.com/privkey.pem;

    client_max_body_size 40m;              # ≥ 服务端 --max-upload-mb（32），否则大附件被反代先拦

    location / {
        proxy_pass         http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;      # ★ 服务端 --trust-proxy 就靠它

        # ★ 长轮询必须放行：读超时要大于客户端 20s / 服务端 30s，否则 /api/sync 全 504
        #   （表现就是"能发消息但永远收不到别人的消息"）
        proxy_read_timeout    120s;
        proxy_send_timeout    120s;
        proxy_connect_timeout 10s;
        proxy_buffering       off;
    }
}
server { listen 80; server_name chat.example.com; return 301 https://$host$request_uri; }
```

```bash
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d chat.example.com            # 首次签证书
systemctl list-timers | grep certbot                # 续期是自动的
curl -sI https://chat.example.com/api/health | head -1     # 期望 200
```

## 6. 防火墙 / 安全组

```bash
sudo ufw default deny incoming
sudo ufw allow 22/tcp
sudo ufw allow 80,443/tcp
sudo ufw enable
```

云服务器还要在**安全组**里做同样的事。`8765` 必须是"只有本机能连"。

## 7. 拿邀请码 / 建第一个账号

```bash
# 默认就是"注册要邀请码"，码在启动日志里（自动生成、落库、重启不变）
journalctl -u mchat-server --no-pager | grep 邀请码

# 方式 A：把码发给用户，让他在客户端「注册」页填进「邀请码」
# 方式 B：自己用 curl 建号（适合一次性建好几个）
INVITE=$(journalctl -u mchat-server --no-pager | grep 邀请码 | tail -1 | awk -F'：' '{print $2}' | awk '{print $1}')
curl -s -X POST https://chat.example.com/api/register \
     -H 'Content-Type: application/json' \
     -d "{\"username\":\"alice\",\"password\":\"强一点的密码\",\"display_name\":\"爱丽丝\",\"invite\":\"$INVITE\"}"

# 建完想彻底关掉注册：在 ExecStart 里加 --no-register 再 restart
# 想换一个自己记得住的码：加 --invite-code=你的码
```

## 7.5 磁盘加密（可选，强烈建议公网部署开）

服务端默认是**明文**落盘（消息、附件）。两条正经做法，二选一：

**A. 应用层磁盘加密（配一个密钥文件）**

```bash
# 1) 生成密钥：32 字节随机，放在数据目录**外面**（systemd 可以 LoadCredential 喂进来）
sudo mkdir -p /etc/mchat
head -c 32 /dev/urandom | sudo tee /etc/mchat/storage.key > /dev/null
sudo chmod 600 /etc/mchat/storage.key && sudo chown root:root /etc/mchat/storage.key

# 2) 装一个 AEAD 后端（服务端本体零依赖，这个是可选增强）
sudo apt install -y python3-cryptography        # 或者 pip install cryptography

# 3) 起服务端时带上它（写进 ExecStart 或用环境变量）
python3 /opt/mchat/mchat_server.py --data /var/lib/mchat \
        --storage-key-file /etc/mchat/storage.key
# 等价：Environment=MCHAT_STORAGE_KEY_FILE=/etc/mchat/storage.key
```

启动日志会明说加密状态 —— 没开就是这句，别忽略它：

```
[mchat] 磁盘加密：**关** —— 消息与附件在磁盘上是明文（没有配置磁盘加密密钥）
[mchat] 磁盘加密：开（AES-256-GCM，密钥来自外部密钥文件）
```

* 第一次带密钥启动时，**以前明文存的内容会被就地改写成密文**并 VACUUM 一次
  （日志里会打"已把 N 处旧的明文内容改写成密文"）—— 也就是说不需要手动迁移。
* 密钥**丢了 = 数据解不开**（这是加密的本意）。把 `/etc/mchat/storage.key` 和备份一起
  保管，但要**分开存**（密钥跟数据放一个包里等于没加密）。
* 配了密钥却没有加密后端 → **服务端拒绝启动**（宁可起不来，也不要"以为加密了其实没有"）。

**B. 整盘加密（不想装 Python 包就用这个）**

Linux 上 LUKS 加密 `/var/lib/mchat` 所在分区，或 Windows 上 BitLocker —— 效果更彻底
（连 SQLite 的 WAL、临时文件、`files/` 一起盖上），代价是开机要解锁那块盘。
`--storage-key-file` 和整盘加密**可以同时用**，不冲突。

> 升级提醒：令牌表改成"只存哈希"之后，**所有人需要重新登录一次**（老令牌无法换算成哈希，
> 直接作废是最安全的做法）。数据库结构在启动时自动迁移，无需人工操作。

## 8. 备份（上线当天就配上）

```bash
sudo crontab -u root -e
```

```cron
30 3 * * * sqlite3 /var/lib/mchat/mchat.db ".backup '/backup/mchat-$(date +\%F).db'"
40 3 * * * rsync -a /var/lib/mchat/files/ /backup/mchat-files/
50 3 * * * find /backup -name 'mchat-*.db' -mtime +14 -delete
```

**备份文件本身要当机密对待**（里面有令牌、消息和附件）。别只备份 `mchat.db` 而丢掉 `files/`
—— 那样所有附件都会变成 404。

## 9. 客户端怎么填

客户端登录窗口「服务器地址」填**根地址**，不要带 `/api`、不要带路径：

| 场景 | 填什么 |
|---|---|
| 公网 + 域名 + HTTPS（推荐） | `https://chat.example.com` |
| 局域网 | `http://192.168.1.20:8765`（明文，但流量不出网） |
| 服务端和客户端同机 | `http://127.0.0.1:8765` |

地址不是本机又用 `http://` 时，登录窗口会**红字警告"明文 HTTP"** —— 那不是 bug，是提醒。
点「测试连接」打的是 `/api/health`，通了再登录能省一半排查。

Windows 客户端分发：`python tools/build_exe.py` 出来的 `dist/main.exe`（未签名，SmartScreen
会拦一下，选"仍要运行"）。

## 10. 升级流程

```bash
# 开发机上：改了 server/ 里的东西，先跑自检
.venv/Scripts/python.exe server/selftest.py          # 226/226 才往下走

# 服务器上
sudo systemctl stop mchat-server
sudo cp /tmp/mchat/mchat_server.py /opt/mchat/
sudo -u mchat python3 /opt/mchat/selftest.py | tail -3
sudo systemctl start mchat-server
journalctl -u mchat-server -n 20 --no-pager
```

数据库结构是**自动迁移**的（`CREATE TABLE IF NOT EXISTS` + 按列名补列），直接换文件即可；
但**升级前先备份 `data/` 目录**，这是唯一的回滚手段。

---

## 上线自检清单（每条都打过勾再睡）

- [ ] `curl -sI https://chat.example.com/api/health` 返回 200，且**只能**从 443 访问
- [ ] `curl -s http://<服务器公网IP>:8765/api/health` 连不上（端口没暴露）
- [ ] 注册必须带邀请码才能成功（不带 → 403 `invalid_invite`）
- [ ] 连续错密码 6 次 → 429（限流/锁定生效）
- [ ] 两个客户端互发消息**实时**到达（不通就查 `proxy_read_timeout`）
- [ ] 发一张图，接收方能自动下到本地（不通就查内存/连接数/`--max-upload-mb`）
- [ ] `journalctl -u mchat-server` 里没有成片的 traceback
- [ ] 备份 cron 跑过一次（`ls -l /backup` 有今天的文件）
- [ ] 反代后面加了 `--trust-proxy`；直接暴露公网时**没有**加

## 常见坑（都踩过）

| 现象 | 原因 / 处理 |
|---|---|
| 能发消息，收不到别人的 | nginx `proxy_read_timeout` < 长轮询挂起时间（客户端 20s / 服务端 30s） |
| 全体 429 | 反代后面忘了 `--trust-proxy`，服务端只看到 127.0.0.1，共用一个限流桶 |
| 偶发 503 `too_many_requests` | 同时连接数到 `--max-connections`：调大，或查是谁在刷 |
| 上传 413 | nginx `client_max_body_size` 或 `--max-upload-mb` 太小；也可能是 `--user-quota-mb` 用满 |
| 服务被 OOM kill | `--max-connections × --max-upload-mb` 太大（上传整段进内存） |
| 附件"文件已丢失" | `files/` 里的字节丢了（只备份了 mchat.db），或孤儿附件被 `--orphan-days` 回收 |
| 所有人被踢回登录 | `--token-ttl-days` 到期，正常；要长期在线就设 `0`（永不过期） |
| `database is locked` | 数据目录放在 NFS/网络盘上了 —— 必须本地盘 |
| 客户端一直"连不上" | 地址里带了 `/api`；或服务端只听 127.0.0.1 而你没配反代 |

## 还**没有**的（上线前心里有数，README §9 有完整清单）

* 消息**不是端到端加密**：服务端库里是明文（本地库也是）。工具箱里的 AES 是独立小工具。
* 没有改密码 / 踢其它设备 / 好友与拉黑；任何注册用户都能搜到所有人并直接发消息。
* 本地数据不加密，靠系统账号权限保护；共用电脑请开磁盘加密。
* 没有审计日志与告警，只有 `--verbose` 的逐条请求日志。

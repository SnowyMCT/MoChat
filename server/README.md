# MChat 服务端 —— 部署与运维

零第三方依赖的聊天服务端，把 `server/` 整个目录拷到服务器上敲一条命令就能跑。
协议契约见同目录的 **`PROTOCOL.md`**（本文档只讲怎么部署，不改协议）。

| 文件 | 作用 |
|---|---|
| `mchat_server.py` | 服务端本体（单文件，只用 Python 标准库） |
| `PROTOCOL.md` | 客户端 ⇄ 服务端契约，**不要改**（客户端按同一份实现） |
| `DEPLOY.md` | **部署 runbook**：照着敲一遍就能上线（从传代码到备份 cron） |
| `selftest.py` | 端到端自检：起真服务端 + 走完整协议流程，全绿才敢上线 |
| `mchat-server.service` | systemd 单元文件 |
| `run.bat` | Windows 上双击启动 |
| `data/` | 运行时自动生成的**数据和附件**（可 `--data` 改到别处） |

> 只想尽快上线：直接看 **`DEPLOY.md`**（可复制的命令清单 + 上线自检清单 + 常见坑）。
> 本文档偏"为什么"和参数解释。

---

## 1. 最快跑起来（本机 / 局域网试水）

```bash
# 本机
python3 mchat_server.py
# → http://127.0.0.1:8765

# 局域网里给同事连（监听所有网卡）
python3 mchat_server.py --host 0.0.0.0 --port 8765
```

Windows 直接双击 `run.bat`（等价于 `python mchat_server.py --host 0.0.0.0 --port 8765 --data "%~dp0data"`）。
第一次启动 Windows 防火墙会弹窗，选“允许访问”才能被局域网其它机器连上。

启动日志里会打出**注册邀请码**（没显式配置时服务端自己生成一个，存在库里，重启不变）：

```
[mchat] mchat-server v1.0.0 已启动  http://0.0.0.0:8765
[mchat] 已有账号 0 个，注册需邀请码
[mchat] ★ 注册邀请码：qQVA-TH-TUwX   （客户端注册时填在「邀请码」里；本次自动生成，已存在库里，重启不变）
[mchat] 登录 token 有效期 30 天；每 IP 每 60 秒最多 12 次登录/注册；
[mchat] 同时连接上限 200；单条消息上限 20000 字符；每账号附件配额 512 MB；typing 节流 1.0s
```

想让谁都能注册就加 `--open-register`（**别在公网上这么干**），一个都不想要就 `--no-register`。

**先自检再对外**：

```bash
python3 selftest.py            # 226 条断言，最后打印 "N/M 通过"，退出码 0 才算绿
```

自检会自己在 `127.0.0.1` 上挑一个空闲端口起真服务端（数据落在临时目录，跑完删掉），
不会碰你的 `server/data/`。

---

## 2. 生产部署（一台 Linux 服务器）

### 2.1 传上去

```bash
# 在开发机上
scp -r server/  user@your-server:/tmp/mchat-server/
# 或者用 rsync（只传这几个文件）
rsync -av server/{mchat_server.py,PROTOCOL.md,selftest.py,mchat-server.service} user@your-server:/tmp/mchat-server/
```

```bash
# 在服务器上
sudo mkdir -p /opt/mchat                        # 程序
sudo mkdir -p /var/lib/mchat                    # 数据（备份就备这一个目录）
sudo cp /tmp/mchat-server/*.py /opt/mchat/
sudo useradd --system --home /var/lib/mchat --shell /usr/sbin/nologin mchat || true
sudo chown -R mchat:mchat /var/lib/mchat
sudo chmod 755 /opt/mchat
```

只需要 **Python 3.9+** 的标准库，不用 pip、不用 venv、不用装任何东西：

```bash
python3 -V                 # 确认 ≥ 3.9
python3 -m py_compile /opt/mchat/mchat_server.py   # 确认没被传坏
```

### 2.2 手动跑一次看日志（推荐先这样验一遍）

```bash
sudo -u mchat python3 /opt/mchat/mchat_server.py \
     --host 0.0.0.0 --port 8765 --data /var/lib/mchat
```

另开一个终端自测：

```bash
curl -s http://127.0.0.1:8765/api/health
# {"ok": true, "name": "mchat-server", "version": "1.0.0", ...}
```

Ctrl+C 退出，应该看到 `[mchat] 已退出，数据已落盘（/var/lib/mchat）`。

### 2.3 systemd 托管

```bash
sudo cp /tmp/mchat-server/mchat-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mchat-server
systemctl status mchat-server            # 看是否 active (running)
journalctl -u mchat-server -f            # 跟日志
sudo systemctl stop mchat-server         # 优雅停止（SIGTERM → 落盘 → 退出）
sudo systemctl restart mchat-server      # 改完参数后重启
```

单元文件里**需要按你机器改的行**都写了注释，主要就三处：`User=` / `Group=`、
`WorkingDirectory=` 和 `ExecStart=` 里的路径、`Environment=MCHAT_DATA=` 数据目录。
数据目录改完别忘了 `chown mchat:mchat`。

### 2.4 参数表

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址。只给自己用就留着默认；对外服务用 `0.0.0.0` |
| `--port` | `8765` | 监听端口 |
| `--data` | 脚本旁边的 `data/` | **数据目录**：数据库 + 附件都在这儿，见 §3 |
| `--token-ttl-days` | `30` | 登录 token 有效期（天）。到期客户端会被踢回登录界面（`/api/me` 401）。**`<= 0` = 永不过期** |
| `--max-upload-mb` | `64` | 单个附件上限（MB），超过返回 413 `too_large` |
| `--user-quota-mb` | `512` | **每个账号的附件总容量**上限，超了 413 `quota_exceeded`（0 = 不限） |
| `--orphan-days` | `7` | 上传了却没被任何消息引用的附件，放几天后自动清掉（0 = 不清理） |
| `--max-text-chars` | `20000` | 单条消息正文上限，超了 413 `message_too_long` |
| `--max-merged` | `50` | 一条「合并转发」最多带几条 |
| `--max-reactions` | `20` | 一个人在同一条消息上最多挂几个不同表情 |
| `--max-pins` | `50` | 一个会话最多同时置顶几条（重复置顶同一条不受限） |
| `--max-group-members` | `100` | 一个群最多几个人（含自己） |
| `--typing-interval` | `1.0` | 同一会话里「正在输入」事件的最小间隔（秒），0 = 不节流 |
| `--storage-key-file` | 空（不加密） | **磁盘加密**的密钥文件（32 字节随机，放数据目录**外面**）。给了它，消息正文 / 附件文件名 / 附件字节在磁盘上就是密文；不给就是明文（启动日志会明说）。也可以用环境变量 `MCHAT_STORAGE_KEY_FILE`。需要 `cryptography` 或 `pycryptodome` 之一做后端，**没有后端就拒绝启动** |
| `--long-poll-max` | `30` | `/api/sync` 单次最长挂起秒数（客户端默认请求 20，留 10 秒余量） |
| `--max-connections` | `200` | 同时在处理的连接数上限，超了立刻回 503（挡住慢连接把线程耗光）；0 = 不限 |
| `--login-rate` | `12` | **每 IP 每 60 秒**允许的登录/注册次数，超了 429；0 = 不限流 |
| `--lock-after` | `5` | 同一账号连续登录失败几次后临时锁定，0 = 不锁 |
| `--lock-seconds` | `60` | 锁多久（秒） |
| `--trust-proxy` | 关 | 部署在 nginx/caddy 后面时**打开**：按 `X-Real-IP` / `X-Forwarded-For` 认客户端 IP。**直接暴露在公网时不要开**（否则伪造这个头就能白拿限流配额） |
| `--open-register` | 关 | 明确允许任何人自助注册（不设邀请码）—— **只适合内网/自测** |
| `--invite-code CODE` | 自动生成 | 注册必须带 `{"invite": "CODE"}`，否则 403 `invalid_invite`。不指定时服务端自己生成一个并打在启动日志里 |
| `--no-register` | 关 | 关闭自助注册：只留已有账号登录，返回 403 `registration_disabled` |
| `--verbose` | 关 | 打印每条请求的访问日志（排查问题用；平时别开） |
| `--version` | | 打印版本 |

`--no-register` / `--invite-code` / `--login-rate` / `--lock-*` / `--user-quota-mb` / `--max-*`
这些是**本实现的部署加固开关**（PROTOCOL.md 的错误表末尾列了对应错误码，格式一致：
`{"error":{"code":…,"message":…}}`，老客户端收到就是一句提示，不会被搞崩）。

---

## 3. 数据落在哪、怎么备份

```
<data>/                     ← 默认 server/data/，生产建议 /var/lib/mchat
├── mchat.db                ← SQLite：账号、token、会话、成员、消息、附件元信息
├── mchat.db-wal            ← 运行时存在（WAL 预写日志），正常退出会合并进 mchat.db
├── mchat.db-shm
└── files/<blob>            ← 上传的附件原始字节，**按内容存一份**：<blob> 是内容的
                               sha256（"b" + 64 位 hex），同一份文件谁传几次都只占这一份。
                               老数据仍是 files/<附件id>，服务端启动时自动归位（不改名）。
                               原文件名存在数据库里（加密），磁盘上看不出来。
```

**备份只需要备份整个 `<data>` 目录**（`mchat.db*` + `files/`）。两种做法：

```bash
# A. 在线备份（不用停服，推荐）：用 sqlite3 的 .backup，附件再 rsync 一份
sudo -u mchat sqlite3 /var/lib/mchat/mchat.db ".backup '/var/lib/mchat/backup-$(date +%F).db'"
sudo rsync -a /var/lib/mchat/files/ /backup/mchat/files/

# B. 停服冷备（最省事，最不容易出错）
sudo systemctl stop mchat-server
sudo tar czf /backup/mchat-$(date +%F).tar.gz -C /var/lib mchat
sudo systemctl start mchat-server
```

**别只拷 `mchat.db` 而丢掉 `files/`** —— 那样附件全变“已丢失”（下载回 404 `not_found`）。

数据库是 **WAL 模式**，删库重来就是 `rm -rf /var/lib/mchat/*`（服务停着再删）。

---

## 4. 上线前一定要做的四件事

### ① 改端口 + 用 nginx / caddy 做 HTTPS 反代

裸 HTTP 意味着**密码和 token 在网线上是明文**，公网部署绝对不能这样。放在反代后面，
只让反代监听 443，服务端自己继续只听 `127.0.0.1:8765`。

**nginx**（`/etc/nginx/conf.d/mchat.conf`）：

```nginx
server {
    listen 443 ssl http2;
    server_name chat.example.com;

    ssl_certificate     /etc/letsencrypt/live/chat.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/chat.example.com/privkey.pem;

    client_max_body_size 80m;          # 要 ≥ 服务端 --max-upload-mb，否则大附件被反代先拦

    location / {
        proxy_pass         http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;

        # ★ 长轮询必须放行：读超时要**大于**长轮询挂起时间（客户端 20s / 服务端上限 30s），
        #   否则每个 /api/sync 都会 504，客户端表现成“消息永远收不到”。
        proxy_read_timeout    120s;
        proxy_send_timeout    120s;
        proxy_connect_timeout 10s;
        proxy_buffering       off;     # 实时推送别缓存
    }
}
# 80 端口跳 443
server { listen 80; server_name chat.example.com; return 301 https://$host$request_uri; }
```

**caddy**（一行搞定证书）：

```
chat.example.com {
    reverse_proxy 127.0.0.1:8765 {
        transport http {
            read_timeout 120s      # 同样要 > 长轮询挂起时间
        }
    }
    request_body {
        max_size 80MB
    }
}
```

> 用反代的话服务端就写 `--host 127.0.0.1`（不暴露端口），端口随便换，比如 `--port 18765`。

### ② 防火墙只开需要的端口

```bash
# 只开 HTTPS（80 留给 Let's Encrypt 续证）；8765 绝不对外
sudo ufw default deny incoming
sudo ufw allow 22/tcp
sudo ufw allow 80,443/tcp
sudo ufw enable
# 或者 firewalld
sudo firewall-cmd --permanent --add-service=https && sudo firewall-cmd --reload
```

云服务器还要在**安全组**里做同样的事。局域网部署同理：只在内网开 `8765`，
别顺手把端口映射到公网。

### ③ 注册与登录的闸门（默认已经关上了）

**出厂的默认状态就是"注册要邀请码"**：不指定 `--invite-code` 时服务端会自己生成一个，
存在库里（重启不变）并打在启动日志里。要建号就把那个码给用户，让他填在客户端注册窗口的
「邀请码」那一格；或者你自己用 curl 建号。

```bash
# 什么都不加 = 默认（要邀请码，码在启动日志的 ★ 那一行）
python3 mchat_server.py

# 想指定一个固定的码
python3 mchat_server.py --invite-code "we-love-mchat-2026"

# 完全关掉注册（账号你自己手动建：先开着注册建好号，再关掉重启）
python3 mchat_server.py --no-register

# 谁都能注册 —— 只在完全封闭的内网/自测时用
python3 mchat_server.py --open-register
```

登录侧的闸门也是内置的，不用再靠反代顶：`--login-rate`（每 IP 每 60 秒几次）、
`--lock-after` / `--lock-seconds`（同一账号连续错几次就锁一会儿）。默认值够挡住
常用爆破脚本，想更严就调小。客户端收到 429 会显示"试得太频繁了，等一下再试"。

### ④ 反代要加 `--trust-proxy`，并把连接数留给服务端管

放在 nginx/caddy 后面时，服务端看到的对端永远是 `127.0.0.1` —— 每 IP 限流会变成
"所有人共用一个桶"。这时**必须**加 `--trust-proxy`，让它按 `X-Real-IP` /
`X-Forwarded-For` 认真实客户端 IP（上面 ① 的 nginx 配置已经带了 `X-Real-IP`）。
**直接暴露在公网上时千万别开这个开关**：伪造那个头就能白拿一份新的限流配额。

`--max-connections`（默认 200）是"慢连接不许把线程耗光"的闸门：超了立刻回 503，
不排队。反代那层如果也有 `limit_conn`，两边的数量要对得上（反代的连接数 ≥ 服务端的）。

改完 systemd：在 `ExecStart=` 那一行末尾追加参数，然后 `systemctl daemon-reload && systemctl restart mchat-server`。

---

## 5. 客户端那边填什么

客户端登录窗口的「服务器地址」填**根地址**，不要带 `/api`、不要带路径：

| 场景 | 填什么 |
|---|---|
| 服务端和客户端在同一台机器 | `http://127.0.0.1:8765` |
| 局域网（服务端内网 IP 192.168.1.20） | `http://192.168.1.20:8765` |
| 公网 + 域名 + HTTPS（**推荐**） | `https://chat.example.com` |

窗口里的「测试连接」按钮打的就是 `GET /api/health`，返回 `ok: true` 就说明地址通了、
服务端活着。填完先测连接再登录，能省掉一半“为什么登不上”的排查。

服务端地址换了（比如从 IP 换成域名）不影响老账号：账号和数据都在服务端，
客户端只是“连到哪个服务器”，换地址就是另一台服务器上的账号。

---

## 6. 两种典型部署

### 6.1 局域网（团队内网，最省事）

- 一台常开的机器（NAS / 小主机 / 办公电脑）当服务端，固定内网 IP 或 DHCP 保留地址。
- 直接 `python3 mchat_server.py --host 0.0.0.0 --port 8765 --data <一个大点的盘>`，
  不需要 nginx、不需要证书、不需要域名。
- 客户端填 `http://<内网IP>:8765`。
- 注意：**内网也是明文**，但流量不出网，风险可接受；机器别做端口映射到公网。
- 开机自启：Linux 用 `mchat-server.service`；Windows 用任务计划程序调用 `run.bat`
  （触发器选“计算机启动时”，勾“不管用户是否登录都运行”）。
- 备份：把 `<data>` 目录拷到 NAS 的另一块盘上就行。

### 6.2 公网（给外部的人用）

- 服务端只监听 `127.0.0.1`，全站交给 nginx/caddy 走 **HTTPS + 域名**（§4 ①）。
- 防火墙只开 22/80/443（§4 ②），**`--no-register` 或 `--invite-code` 必须开**（§4 ③）。
- 数据目录放 `/var/lib/mchat`，用 systemd 托管，`Restart=always`（服务端崩了自动拉起）；
  `systemctl enable` 保证开机自启。
- 磁盘：附件吃空间，`--max-upload-mb` 按需要调小；定期清 `files/` 里没人引用的附件。
- 建议再叠一层防护（nginx `limit_req` / fail2ban）挡住扫描：服务端内置的
  `--login-rate` 只认单机视角，反代那层能看到更多（多个后端、更大范围）；token 有效期别设太长
  （`--token-ttl-days 14` 之类，或设 0 表示永不过期然后靠 `--no-register` + 强密码兜底）。
- **备份 cron**（§3 的 A 方案）：

  ```cron
  30 3 * * * sqlite3 /var/lib/mchat/mchat.db ".backup '/backup/mchat-$(date +\%F).db'"
  40 3 * * * rsync -a /var/lib/mchat/files/ /backup/mchat-files/ && find /backup -name 'mchat-*.db' -mtime +14 -delete
  ```

---

## 7. 排查速查

| 现象 | 多半是 |
|---|---|
| 启动就报 `Address already in use` | 端口被占：`ss -ltnp \| grep 8765`，换个 `--port` 或杀掉老进程 |
| 客户端一直“连不上/超时” | 地址填错（带了 `/api`）、服务端只听 `127.0.0.1`、防火墙没开、云安全组没放行 |
| 能登录能发消息，但**消息不实时** | 反代 `proxy_read_timeout` 小于长轮询挂起时间（§4 ① 的 ★）；或中间设备掐长连接 |
| 上传大附件 413 | `--max-upload-mb` 太小，或 nginx `client_max_body_size` 太小 |
| 上传报 413 `quota_exceeded` | 这个账号的附件总量到了 `--user-quota-mb`（默认 512MB） |
| 发消息报 413 `message_too_long` | 正文超过 `--max-text-chars`（默认 20000 字符） |
| 注册报 403 `invalid_invite` | 服务端要邀请码：把启动日志 ★ 那一行的码填进客户端「邀请码」 |
| 登录/注册报 429 | 撞到 `--login-rate` 或 `--lock-after` 了；等一会儿，或按需调大 |
| 反代后面所有人都被 429 | 忘了加 `--trust-proxy`（服务端只能看到 127.0.0.1，所有人共用一个桶） |
| 偶发 503 `too_many_requests` | 同时连接数到了 `--max-connections`（默认 200）：多半是有人刷或者长轮询堆积，调大或查来源 |
| 几小时后所有人被踢回登录 | `--token-ttl-days` 到了，属正常；想久一点就调大（0 = 永不过期） |
| 日志出现 `database is locked` | 数据目录放在了 NFS/网络盘上 —— 换成本地盘（WAL + 本地盘才靠谱） |
| 附件下载 404 `not_found` | `files/` 里的字节丢了（只备份了 `mchat.db`？），或孤儿附件被 `--orphan-days` 清掉了 |
| 客户端说“明文 HTTP”是红的 | 那是提醒：非本机地址走 http 时密码/令牌是明文，配 HTTPS 后就不提示了 |

服务端日志：`journalctl -u mchat-server -f`（systemd）或前台跑时直接看。
加 `--verbose` 能看到每条请求。

## 8. curl 速查（部署后自测）

```bash
BASE=http://127.0.0.1:8765
curl -s $BASE/api/health

# 注册：默认要邀请码 —— 先从服务端日志里把 ★ 那一行的码抄下来
INVITE=qQVA-TH-TUwX
TOKEN=$(curl -s -X POST $BASE/api/register \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"alice\",\"password\":\"secret123\",\"display_name\":\"爱丽丝\",\"invite\":\"$INVITE\"}" \
        | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')

# 令牌只走 Authorization 头（?token= 会被 401）
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/conversations"
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/sync?cursor=0&timeout=5"
```

---

## 9. 安全清单（现在保护住了什么 / 还该注意什么）

### 已经做进去的（不用你再配）

| 面 | 做法 |
|---|---|
| 密码 | PBKDF2-SHA256 20 万轮 + 每账号随机 salt，**不存明文**；用户不存在时也空跑一次 PBKDF2，不泄露"这个人有没有" |
| 令牌 | `secrets.token_urlsafe(32)`；库里**只存哈希**（拿到库文件或备份也冒充不了谁）；**只认 `Authorization` 头**（URL 里带 token 一律 401）；`--token-ttl-days` 到期自动失效（`<= 0` = 永不过期） |
| 存储 | 配 `--storage-key-file` 时：SQLite 里的消息正文/引用/转发/附件名 + 附件字节**全部密文落盘**（AES-256-GCM），启用前留下的明文会在第一次启动时**就地改写**并 VACUUM 清干净；没配就是明文，启动日志里明说。删改过的旧数据开 `secure_delete` |
| 注册 | 默认要邀请码（自动生成、落库、打在启动日志里）；可 `--no-register` 彻底关闭 |
| 爆破 | `--login-rate`（每 IP 频率）+ `--lock-after` / `--lock-seconds`（账号连续失败就临时锁）→ 429 |
| 资源 | `--max-connections`（超了立刻 503，慢连接吃不光线程）/ `--max-text-chars` / `--max-merged` / `--max-upload-mb` / `--user-quota-mb`；孤儿附件按 `--orphan-days` 自动回收 |
| 越权 | 会话/附件成员校验（非成员 403）；**消息只能引用自己上传的附件**（否则"猜一个附件 id 拉进自己会话再下载"就是读别人文件）；id 是 8 字节随机，猜不动 |
| 放大 | typing 双向节流（状态变了立刻放行）；已读回执只推"这次新读到的"、没前进就什么都不推；表情/置顶/群人数都有上限 —— 这些都是"一次请求触发 N 次推送"的地方 |
| 文件名 | `?name=` 只留最后一段、去 CR/LF、截 120 字符 —— 既防 `Content-Disposition` 响应拆分，也防**接收方**被写到下载目录外 |
| 报错 | 500 只回一句"服务端内部错误"，异常类型/路径/SQL 只进服务端日志 |
| SQL | 全参数化；LIKE 有 ESCAPE；表名只用内部常量（不接受外部输入） |
| 客户端 | 附件落地路径统一走 `mchat/core/paths.py`（清洗 + 目录包含性校验）；超过 16MB 的附件不自动下载（只显示"点一下再下"，`auto_download_mb` 可调）；非本机地址走 http 时登录窗口明确警告 |

### 部署时你自己要做的

1. **HTTPS**（§4 ①）：裸 HTTP 时密码和令牌在网线上是明文，公网必须上反代 + 证书。
2. **防火墙/安全组**（§4 ②）：只开 22/80/443，`8765` 不对外。
3. **反代加 `--trust-proxy`**（§4 ④）：否则每 IP 限流会退化成"所有人共用一个桶"。
4. **关掉注册或守住邀请码**（§4 ③）：默认已经是关的，别顺手加 `--open-register` 放公网。
5. 备份 `data/` 整个目录（§3）：令牌、消息、附件都在里面 —— **备份文件本身要当机密对待**。

### 还**没有**做的（自己心里有数）

* **不是端到端加密**：配了 `--storage-key-file` 只是"磁盘上是密文"，**服务端进程本身
  能看到明文**（它要处理消息），所以"服务端管理员也能读"这件事没变。真要防服务端，
  得上端到端加密（密钥只在客户端），那是另一套设计，现在没有。
  工具条里那个 AES 工具箱是"手动加密一段文本"的独立小工具，和聊天信道无关。
* **没有改密码 / 踢其它设备**：账号被盗只能删号重建；令牌是多设备各自一份、没有集中吊销入口。
* **没有好友/拉黑**：任何注册用户都能搜到所有人并直接给你发消息（附件自动下载已按大小设限，
  文件名已清洗，但"陌生人能找你"这件事本身是设计如此）。
* **没有审计日志/告警**：谁在什么时候登录过、有没有被扫，只有 `--verbose` 的逐条请求日志。

### 客户端那份本地数据（Windows 上）

客户端**默认就是加密存的**（`mchat/core/atrest.py`）：历史库里的每句话、附件缓存里的
每个字节、令牌缓存里的令牌，落盘前一律 AES-256-GCM；数据密钥存在数据目录的
`storage.key` 里，Windows 上用 **DPAPI** 包住（**绑定当前 Windows 账号 + 本机**）。
换句话说：把 `%APPDATA%\MChat\MChat` 整个拷到别的机器/别的账号下，**解不开**。

* 换机器/重装系统前想保住本地历史：连着 Windows 账号一起迁移（或先导出聊天记录）。
* 想关掉加密对照排查：设环境变量 `MCHAT_NO_AT_REST=1`（启动时界面会显示"未加密"）。
* 点开附件时会在 `%TEMP%\mchat-view\` 留一份明文（系统程序只认明文文件），退出时自动清。
* 搜索：加密之后 FTS 索引里是密文，搜索改成"取最近 2 万条解密再找"（见 `db.search`），
  结果一样，只是不再用倒排索引加速。

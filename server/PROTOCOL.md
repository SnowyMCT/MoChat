# MChat 服务端 ⇄ 客户端协议 v1

这份文档是**唯一契约**：`server/mchat_server.py`（服务端）和 `mchat/core/api.py`（客户端）
都按它实现，改协议必须同时改两处 + 这份文档。

* 传输：**纯 HTTP/1.1 + JSON**（长轮询做实时推送），没有 WebSocket、没有第三方依赖。
* 服务端只依赖 Python 标准库（3.9+），把 `server/` 目录拷到服务器上 `python3 mchat_server.py` 就能跑。
* 编码：请求/响应 body 一律 UTF-8 JSON（`Content-Type: application/json; charset=utf-8`），
  唯一例外是上传/下载文件（原始字节）。
* 鉴权：除 `/api/health` `/api/register` `/api/login` 外，所有接口都要
  `Authorization: Bearer <token>`。

## 1. 错误格式

任何失败都返回合适的 HTTP 状态码 + 统一 JSON：

```json
{"error": {"code": "bad_credentials", "message": "用户名或密码不对"}}
```

| code | HTTP | 含义 |
|---|---|---|
| `invalid_request` | 400 | 字段缺失/类型不对/JSON 坏了 |
| `invalid_username` | 400 | 用户名不合法（3–24 位，字母数字下划线点减号） |
| `weak_password` | 400 | 密码太短（< 6 位） |
| `username_taken` | 409 | 用户名已被注册 |
| `bad_credentials` | 401 | 用户名或密码错 |
| `unauthorized` | 401 | token 缺失/失效 |
| `not_found` | 404 | 会话/用户/附件不存在 |
| `not_member` | 403 | 不在这个会话里 |
| `duplicate_id` | 409 | 同一条消息 id 已被别的会话/人占用（正常重试不会出现） |
| `too_large` | 413 | 上传超过 `--max-upload-mb` |
| `server_error` | 500 | 服务端内部错误（**body 里不带内部细节**，只一句"服务端内部错误"） |
| `registration_disabled` | 403 | 服务端关闭了自助注册（`--no-register`） |
| `invalid_invite` | 403 | 注册要邀请码，没带或不对 |
| `too_many_requests` | 429 | 登录/注册太频繁，或这个账号被临时锁定 |
| `message_too_long` | 413 | 单条消息正文超过 `--max-text-chars` |
| `quota_exceeded` | 413 | 附件占用超过账号配额（`--user-quota-mb`） |

> 最后 5 条是**部署加固扩展**（不在最早的 v1 表里，但格式完全一致）：老客户端收到
> 它们只会显示一句错误提示，不会被搞崩；`server/mchat_server.py --help` 里有对应开关。

## 2. 对象

```jsonc
User = {"id": "u_a1b2c3d4e5f60718", "username": "alice", "display_name": "爱丽丝", "online": true}

Attachment = {"id": "f_9f3c11aa22bb33cc", "name": "配色预览.png", "size": 20480}
// 服务端生成的 id 一律是 <前缀>_ + 16 位 hex（8 字节随机）—— 短 id 是能被枚举的，
// 而附件 id 会出现在 URL / 消息体里，所以给足随机性。

Message = {
  "id": "b1c9…",              // 客户端生成的 32 位十六进制 id（见 §4 幂等），服务端原样保存
  "conversation_id": "c_77",
  "sender_id": "u_ab12",
  "sender_name": "爱丽丝",
  "text": "在吗",
  "ts": 1759123456.123,       // 服务端收到消息时的 Unix 时间戳（秒，浮点）
  "attachment": null,          // 或 Attachment
  "reply_to": null,            // 或 {"id": "…", "author": "我", "preview": "周五那份材料"}
  "forwarded_from": "",        // 转发自哪个会话的名字，空 = 不是转发
  "merged": [],                // 合并转发：[{"author","text","attachment","clock"}]
  "status": "sent",            // "sent" | "read"：对我发出去的消息而言，别人读没读
  "reactions": [               // 回应（表情）：计数是服务端算的，mine 按请求者算
    {"emoji": "👍", "count": 2, "mine": true}
  ]
}

Conversation = {
  "id": "c_77",
  "kind": "direct",            // "direct" | "group"
  "title": "爱丽丝",            // 单聊=对方显示名，群聊=群名（服务端算好，客户端直接用）
  "members": [User, ...],      // 含自己
  "last_message": null,         // 或 Message（没有消息就是 null）
  "last_ts": 0.0,
  "unread": 0,                  // 还没读的条数（只算别人发的）
  "updated_at": 0.0,
  "pins": [                     // 会话内置顶：只含"双方可见"的那些（"仅我可见"是纯客户端的事）
    {"message_id": "b1c9…", "pinned_by": "u_ab12", "pinned_by_name": "爱丽丝",
     "pinned_at": 1759123456.1, "for_both": true,
     "message": Message}        // 被置顶那条的快照：让对方不用先拉到那条历史也能画出横幅
  ]
}

Friend = {"user": User, "since": 1759123456.1}          // since = 成为好友的时间（服务端给）
// 好友申请的两种形态（incoming / outgoing）：
FriendRequest(in)  = {"user": User, "note": "你好，在吗", "ts": 1759123456.1}   // user = 申请人
FriendRequest(out) = {"user": User, "note": "你好，在吗", "ts": 1759123456.1}   // user = 被申请人
// note 是申请附带的留言（≤500 字符，超了截断）。**从门禁来的申请**，note 就是
// 那条发不出去的消息的正文 —— 同意之后它会变成会话里的第一条消息。
```

## 3. 接口

### 3.1 健康检查（免鉴权）

```
GET /api/health
→ 200 {"ok": true, "name": "mchat-server", "version": "1.0.0",
       "time": 1759123456.1, "users": 3, "messages": 42}
```

登录窗口的「测试连接」用它。

### 3.2 注册 / 登录 / 退出

```
POST /api/register   {"username": "alice", "password": "secret123", "display_name": "爱丽丝",
                      "invite": "服务端给的邀请码"}
→ 200 {"token": "…", "user": User, "server_time": 1759123456.1}
   用户名重复 → 409 username_taken
   服务端要邀请码而没带/不对 → 403 invalid_invite
   同一 IP 短时间内注册太多次 → 429 too_many_requests

POST /api/login      {"username": "alice", "password": "secret123"}
→ 200 {"token": "…", "user": User, "server_time": 1759123456.1}
   密码错 → 401 bad_credentials
   撞太多（每 IP 频率 / 该账号连续失败被锁）→ 429 too_many_requests

GET  /api/me         (auth)
→ 200 {"user": User, "server_time": 1759123456.1}
   token 失效 → 401 unauthorized      ← 客户端靠它判断"缓存的登录还能不能用"

POST /api/logout     (auth)
→ 200 {"ok": true}
```

**服务端只存令牌的哈希**（``sha256``）：库文件/备份被拿走也不能直接拿去冒充某个账号。
客户端手里那串令牌照旧是唯一凭据，协议层没有任何变化。

**注册默认要邀请码**：服务端没显式配置时（既没 `--invite-code`，也没 `--no-register`，
也没 `--open-register`）会**自己生成一个邀请码打在启动日志里**，所以出厂的默认状态不是
"谁都能注册"。客户端注册时把码放在 body 的 `invite` 字段（登录窗口有「邀请码」那一格）。

密码用 `hashlib.pbkdf2_hmac("sha256", pw, salt, 200_000)` 存哈希（salt 每账号随机 16 字节），
**绝不存明文**。token 是 `secrets.token_urlsafe(32)`，存服务器库里，`--token-ttl-days` 到期失效
（**`<= 0` 表示永不过期**，`expires_at` 存 NULL）。
`display_name` 为空时就用 `username`。

**token 只能放在 `Authorization: Bearer <token>` 头里** —— 任何 `?token=` 形式一律 401：
URL 会进反代的 access log、浏览器历史和报错日志，长效令牌不该出现在那些地方。

### 3.3 找人 / 建会话

```
GET  /api/users?query=ali        (auth)          # 按 username / display_name 子串搜，排除自己
→ 200 {"users": [User, ...]}                      # 最多 20 个，无 query 时返回最近活跃的几个

GET  /api/conversations          (auth)
→ 200 {"conversations": [Conversation, ...]}      # 按 last_ts 倒序

POST /api/conversations/direct   (auth)  {"username": "bob"}
→ 200 {"conversation": Conversation}              # **幂等**：已经有单聊就返回同一个 id
   对方不存在 → 404 not_found

POST /api/conversations/group    (auth)  {"title": "评审组", "usernames": ["bob", "carol"]}
→ 200 {"conversation": Conversation}              # 自己自动进群；usernames 里的不存在 → 404
                                                   # 人数超过 --max-group-members（默认 100，含自己）→ 400
```

### 3.4 消息

```
GET  /api/conversations/<cid>/messages?before=<ts>&limit=50   (auth)
→ 200 {"messages": [Message, ...], "has_more": false}
   * 按 ts **升序**返回（界面上是由上到下），最多 limit 条（默认 50，最大 200）；
   * 不给 before = 最新的一批；给了 before = ts < before 的上一批（往上翻历史）；
   * 不是成员 → 403 not_member

POST /api/conversations/<cid>/messages   (auth)
{
  "client_id": "8f1c…",          // 必填，客户端生成的 32 位 hex，**就是这条消息的 id**
  "text": "在吗",                 // 没有附件时不能是空串
  "attachment_id": null,          // 先 POST /api/upload 拿到的 id
  "reply_to": null,               // {"id","author","preview"} 原样存
  "forwarded_from": "",
  "merged": []
}
→ 200 {"message": Message}         // ts / sender_name 由服务端补
→ 200 {"message": null, "friend_request": {"user": User, "note": "在吗", "ts": 1759123456.1}}
    // ↑ **好友门禁**：单聊里"对方"（这个 direct 会话成员里除我之外的那个人）还不是
    //   我的好友时，这条消息不落库，而是变成一条好友申请推给对方（见 §3.6）。
```

* **好友门禁（单聊专用）**：`kind=="direct"` 且对方不在我的好友列表里时，服务端
  **不写 messages 表**，而是把 (我, 对方) 的申请 upsert 成一条 `friend_requests`
  （`note` = 这条消息的正文，截到 500 字符），给对端推 `friend_request` 事件，
  然后回 200 `{"message": null, "friend_request": …}`。
  已经是好友、或者群聊 → 行为**完全不变**（始终回 `{"message": Message}`）。
  参数本身有问题（正文超长、附件不是自己的、reply_to/merged 格式不对）仍然先按老规矩报错 ——
  别让"发不出去"把真正的格式错误盖掉。

* **上限**（服务端挡着，别指望客户端守规矩）：`text` 超过 `--max-text-chars`（默认 20000 字符）
  → 413 `message_too_long`；`merged` 超过 `--max-merged`（默认 50 条）→ 400 `invalid_request`。
  `merged` 里每条只保留 `author` / `text` / `attachment` / `clock` 四个字符串字段
  （多塞的结构会被丢掉）；`reply_to.preview` 截到 300 字符、`forwarded_from` 截到 128 字符。
* **`attachment_id` 只能是自己上传的**：引用别人的附件 → 403 `not_member`。
  （下载鉴权的规则是"引用它的消息所在会话的成员都能下"，如果谁都能引用，就等于
    "猜到一个附件 id 就能把它拉进自己的会话再下载" —— 所以这道校验必须在。）
* **幂等**：同一个 `client_id` 重复 POST（网络重试）只入库一次，第二次返回同一条消息，
  不会变成两条。id 被**别人**占用才返回 409 `duplicate_id`。
* 服务端把这条消息作为 `message` 事件推给**所有成员（包括发送者自己的其它设备）**。

```
POST /api/conversations/<cid>/read     (auth)  {"up_to_ts": 1759123456.1}
→ 200 {"ok": true}                   // 已经在"看到这儿"了：{"ok": true, "unchanged": true}
   把"别人发的、ts <= up_to_ts"的消息都标成我读过了；同时把我的未读清零，
   并给那些消息的发送者推 read 事件。
   * **只推"这次新读到的"那一批**（ts 在"上次已读点"和 up_to_ts 之间），
     而且 up_to_ts 没往前推时**什么都不推** —— 否则循环调这个接口就能给每个人
     各推 200 条 read 事件，是个现成的放大器。

POST /api/conversations/<cid>/typing   (auth)  {"typing": true}
→ 200 {"ok": true, "throttled": true?}
   给**其它成员**推 typing 事件（不落库、不占 cursor）。客户端建议 3 秒节流一次；
   服务端也有一道闸（`--typing-interval`，默认 1 秒）：同一个状态刷得太快就不再推，
   返回里带 `"throttled": true`。**两个方向都节流**（以前 `false` 永远放行，循环发
   一样能把全体成员吵醒），但**只要状态变了就立刻放行** —— 所以"我不打字了"不会被
   压掉，对方不会一直看到"正在输入…"。

POST /api/conversations/<cid>/reactions (auth) {"message_id": "b1c9…", "emoji": "👍", "added": true}
→ 200 {"ok": true, "message_id": "b1c9…", "emoji": "👍", "added": true,
       "reactions": [{"emoji": "👍", "count": 2, "mine": true}]}
   消息回应（表情）。**计数由服务端算**（一人一条记录），客户端不许自己加一减一 ——
   两个人同时点同一个表情才不会算成 1。`added` 不传就是"开关"；一般传明确值（重试幂等）。
   然后给**所有成员**推 `reaction` 事件（`mine` 按收件人各算一份）。
   `emoji` 空/带空白/超过 16 字符 → 400 invalid_request；消息不在这个会话里 → 404。
   一个人在同一条消息上最多挂 `--max-reactions` 个不同表情（默认 20）→ 400
   （每次加/撤都会给全体成员推事件，不设顶就是个刷屏/放大手段）。

POST /api/conversations/<cid>/pin      (auth)  {"message_id": "b1c9…", "pinned": true}
→ 200 {"ok": true, "pinned": true, "message_id": "b1c9…"}
   会话内置顶（"双方置顶"）。**只有"也为对方置顶"才调这个接口** ——
   "仅我可见"是纯客户端的事（只写本地库，服务端根本不该知道）。
   服务端记下 (会话, 消息) 这一条，然后给**所有成员（包括发起人自己的其它设备）**
   推一条 `pin` 事件；`pinned=false` 是取消置顶。
   消息不在这个会话里 → 404 not_found；不是成员 → 403 not_member。
   一个会话最多同时置顶 `--max-pins` 条（默认 50）；重复置顶同一条是幂等的，不受上限影响。
```

### 3.5 实时推送（长轮询）

```
GET /api/sync?cursor=<int>&timeout=<sec>   (auth)
→ 200 {"events": [Event, ...], "cursor": 12, "server_time": 1759123456.1}
```

* `cursor` 是**每个用户自己的事件序号**（单调递增整数）。首次不带 cursor = 0，只拿"从现在起"的新事件。
* 有事件立刻返回；没有就挂住最多 `timeout` 秒（默认 20，服务端上限 30）再返回空 events。
  客户端一次连接拿到东西就立刻再发起下一次 —— 这就是"实时"。
* `cursor` 太旧（服务端已清理）时返回 `{"events": [], "cursor": <最新>}`，
  事件列表里会有一个 `{"kind": "resync"}`：客户端收到就重新拉一次 `/api/conversations`。

Event 的四种形态：

```jsonc
{"kind": "message", "conversation_id": "c_77", "message": Message}
{"kind": "read",    "conversation_id": "c_77", "message_id": "8f1c…", "reader_id": "u_b2", "ts": 1.0}
{"kind": "typing",  "conversation_id": "c_77", "user_id": "u_b2", "user_name": "鲍勃", "typing": true}
{"kind": "conversation", "conversation_id": "c_91", "conversation": Conversation}   // 别人拉我进群/新建会话
{"kind": "reaction", "conversation_id": "c_77", "message_id": "b1c9…", "emoji": "👍",
 "added": true, "by_id": "u_b2", "by_name": "鲍勃",
 "reactions": [{"emoji": "👍", "count": 2, "mine": false}], "ts": 1.0}
// 有人加了/撤了表情回应：reactions 是**服务端算好的整份清单**，收到直接覆盖本地，
// 不要自己加一减一（两个人同时点会算错）；mine 按收件人算，所以每人收到的不一样。
{"kind": "presence", "user_id": "u_b2", "online": false, "ts": 1.0}
// 某人上线/下线了（服务端每 --presence-interval 秒扫一次，变化才推）。
// 只推给"跟他有共同会话的人"。客户端的 header 小绿点、侧栏副标题据此更新 ——
// 否则对方挂了半小时，我这边还亮着。
{"kind": "pin", "conversation_id": "c_77", "message_id": "b1c9…",
 "message": Message, "pinned": true, "pinned_by": "u_b2", "pinned_by_name": "鲍勃", "ts": 1.0}
// 会话内置顶变了（对方点了「也为…置顶」或者取消了）：pinned=false 表示取消。
// 带 message 快照是为了让收到的一方即使还没拉过那条历史，也能直接画出置顶横幅。
{"kind": "friend_request", "user": User, "note": "你好，在吗", "ts": 1.0}
// 有人申请加我（或者我在单聊里发消息被门禁拦成了申请）：user = **申请人**，
// note = 留言（门禁来的申请就是那条发不出去的消息正文）。收到就刷新 /api/friends。
{"kind": "friend_accept", "user": User, "ts": 1.0}
// 我发出去的申请被同意了：user = 同意的那个人（也就是新好友）。
// 同一时刻还会收到一条 message 事件 —— 如果申请带着留言，它会变成会话里的第一条消息。
{"kind": "friend_reject", "user": User, "ts": 1.0}
// 我发出去的申请被拒了：user = 拒绝的那个人。我的 outgoing 里这条申请已经没了；
// 如果两个人之间只有一个"一条消息都没有"的单聊，它也会被服务端顺手删掉。
{"kind": "friend_remove", "user": User, "ts": 1.0}
// 对方把我删了（或者我自己在别的设备上删的）：user = 删人的那一方。
// **历史会话和消息都还在**，只是从此刻起对方再给我发消息会重新变成好友申请。
```

好友事件都只推给"相关的那个人"，外加发起者自己的其它设备（多设备同步用，和 message/reaction
的惯例一致）—— 别人不会收到跟我有关的好友事件。

### 3.6 好友

```
GET  /api/friends                                   (auth)
→ 200 {"friends":  [{"user": User, "since": 1.0}, ...],       // 已经是好友的
       "incoming": [{"user": User, "note": "…", "ts": 1.0}, …], // 别人申请加我（user = 申请人）
       "outgoing": [{"user": User, "note": "…", "ts": 1.0}, …]} // 我申请加别人（user = 被申请人）

POST /api/friends/request   (auth)  {"username": "bob", "note": "你好"}
→ 200 {"status": "already_friends"}                        // 已经是好友（什么都不用做）
→ 200 {"status": "pending",  "request": {"user": User, "note": "你好", "ts": 1.0}}
→ 200 {"status": "accepted", "friend": {"user": User, "since": 1.0}, "conversation": Conversation}
    // ↑ 对方**已经申请过我**：两个人都伸手了就不用再点一次同意，直接互为好友
    //   对方存在 → 否则 404 not_found；加自己 → 400 invalid_request

POST /api/friends/accept    (auth)  {"username": "bob"}
→ 200 {"status": "accepted", "friend": {"user": User, "since": 1.0}, "conversation": Conversation}
    // 没有"对方发给我、还没处理"的申请 → 404 not_found

POST /api/friends/reject    (auth)  {"username": "bob"}
→ 200 {"ok": true}                 // 没申请 → 404 not_found

POST /api/friends/remove    (auth)  {"username": "bob"}
→ 200 {"ok": true}                 // 本来就不是好友也回 ok（幂等）
```

* **关系是双向存的**：`friends` 表里互为好友插两行，谁查都是同一份真相（客户端不用自己
  拼"对方有没有加我"）。`since` 是成为好友的时间（服务端给，客户端别自己造）。
* **申请只有一条**：同一对 (requester, target) 只有一行；反复发（比如门禁反复拦消息）
  只刷新 `note` 和 `updated_at`，不会堆出一串申请。留言上限 500 字符（超了截断）。
* **`accept` 会把留言落成消息**：同意时如果申请带着 `note`，服务端把它作为
  **申请人发出的**一条消息写进双方的单聊（`ts` / `sender_id` 服务端补），并给双方推
  `message` 事件 —— 所以客户端不用自己造这条消息，靠事件显示就行。
* **`accept` / `request` 自动成好友时都会确保"双方有单聊"**：没有就按
  `POST /api/conversations/direct` 的老逻辑建一个（已经有就复用，不会冒出第二个单聊）。
* **`reject` 会顺手收掉空壳会话**：如果两个人之间只剩下一个**一条消息都没有**的
  `direct` 会话（申请人就是在里面发消息被拦成申请的），连它的成员行一起删掉；
  只要会话里有历史，就一律保留。
* **`remove` 只删关系**：历史会话和消息一条不动。删掉之后双方再互发消息会**重新变成
  好友申请**（门禁只看 `friends` 表）—— 这是预期行为，不是 bug。
* 这些接口都没带合法 token 时一律 401 `unauthorized`（和现有一致）；没有新增错误码。

**存量迁移（服务端启动时自动做，客户端无感）**：升级前就存在的 `direct` 会话，
服务端首次启动（建表之后、开始服务之前）会把每对成员追认成互为好友，
所以老用户不会被这道新门禁冻结。这段迁移只对"老库"跑一次（库里记了标记位），
**用户主动删掉的好友不会在重启后又自己长回来**。

### 3.7 附件

```
POST /api/upload?name=<urlencoded 文件名>      (auth, body = 原始字节)
→ 200 {"attachment": Attachment, "deduped": false}
                                                 // 超过限制 → 413 too_large
                                                 // 超过账号配额 → 413 quota_exceeded

GET  /api/files/<attachment_id>                (auth: Authorization 头)
→ 200 原始字节（Content-Type 按后缀猜，Content-Disposition: attachment; filename*=UTF-8''…）
   * 只有"上传者本人"或"引用它的那条消息所在会话的成员"能下载，否则 403 not_member
```

**同一份内容在服务端只存一份**（内容寻址去重，`deduped: true` 就是"这次传的字节
服务端早就有了，没再占空间"）。要点：

* 每次上传仍然**各返回一个新的附件 id**，各自带自己那次的文件名 —— 去重的是磁盘上的
  字节，不是"附件记录"：名字、属主、上传时间都是每次各自的，"只能引用自己上传的附件"
  那道校验（§3.4）和"这份算谁的配额"因此照旧成立。客户端**不需要为它改任何代码**
  （多出来的 `deduped` 字段可以无视）。
* 配额（`--user-quota-mb`）按**内容**算：同一份内容重复上传不重复计费，也不会因为
  "服务端已经有这份内容"而把你卡住。
* 老库/老数据在服务端第一次启动时自动归位（算出内容哈希、登记去重表），
  老文件不改名 —— 对客户端完全透明。
* 回收规则：没有任何消息引用的附件放够 `--orphan-days` 天会被删；**只有这份内容再
  没有任何附件引用时**才连磁盘字节一起删（别人还在用的那份绝不会被误删）。

**文件名会被服务端洗一遍**（`?name=` 是发送者说了算的）：只留最后一段（去掉 `/` 和 `\`）、
去掉 CR/LF 等控制字符、截到 120 字符。因为这个名字会出现在两个危险的地方 ——
`Content-Disposition` 响应头（带 CRLF 就是响应拆分）和**接收方**的本地落地路径
（带 `..` 就是任意文件写入）。客户端在 `mchat/core/paths.py` 里再钉一次：落地路径必须是
`<下载目录>/<附件id>-<安全文件名>`，且校验过"真的在下载目录里"。

**不再支持 `?token=` 下载**（以前留的"把 URL 扔给浏览器"后门）：令牌只认
`Authorization` 头。真要做浏览器直链，应该由服务端签发一次性短链，而不是复用会话令牌。

## 4. 消息 id 与幂等（客户端要注意的）

* 客户端在**本地就生成**消息 id（`uuid.uuid4().hex`，32 位 hex），显示时立刻用它；
  发到服务端时放在 `client_id`，服务端原样当消息 id 存 —— 所以**本地和服务端 id 一致**，
  不需要"临时 id → 真 id"的映射。
* 网络失败重试时重发同一个 `client_id`，服务端只入库一次。
* 服务端会把这条消息也推回给发送者（其它设备要看到），客户端收到时**按 id 去重**即可。

## 5. 时间与顺序

* 消息 ts 由**服务端**给（发送者本地时间只用于乐观显示，服务端返回的 `ts` 覆盖它）。
* 历史消息按 ts 升序；同一 ts 再按 id 排，保证两端顺序一致。

## 6. curl 速查（部署后自测）

```bash
BASE=http://127.0.0.1:8765
curl -s $BASE/api/health
TOKEN=$(curl -s -X POST $BASE/api/register -d '{"username":"alice","password":"secret123","display_name":"爱丽丝"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/conversations"
```

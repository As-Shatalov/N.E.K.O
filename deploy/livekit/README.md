# 猫娘串门 · 海外 LiveKit 自建节点

本目录是串门功能海外传输（LiveKit）的自建部署模板：一台机器上跑 Caddy（带 caddy-l4 插件）+ `livekit-server`。大陆走 TRTC 托管，不需要部署任何东西。设计依据见 `docs/design/visit-infrastructure.md`（OD-07 v2、§4.7、§5 PR-16），Servers 侧接口见 `docs/design/visit-servers-contract.md`。

| 文件 | 作用 |
|---|---|
| `docker-compose.yml` | 两个服务：`caddy`（现场用 xcaddy 构建 caddy-l4）与 `livekit`（`livekit/livekit-server`），都用 host 网络 |
| `Caddyfile` | 443 上的四层 SNI 分流：TURN 域名 → LiveKit TURN/TLS（带 PROXY v2 头，配合 `turn.proxy_protocol: true` 保住客户端地址；需 livekit-server ≥ v1.13.7），信令域名 → LiveKit HTTP / WebSocket |
| `livekit.yaml` | LiveKit 配置：`auto_create:false`、`max_participants:2`、`departure_timeout:60` 等 |
| `.env`（自己建，不入库） | 域名、证书邮箱、API key / secret |

**本目录不含任何真实密钥或域名**：`livekit.yaml` 里的 `REPLACE_ME_*` 是占位，密钥一律经 `.env` → 环境变量注入。

## 上线期用 LiveKit Cloud 还是自建：取决于 T15

- **T15**（设计稿 §3.12）：LiveKit Cloud 能否关闭自动建房——`DeleteRoom` 之后用一张未过期的 JWT 再连，房间会不会被重建。
- **T15 通过**（Cloud 能关自动建房、删房后旧 JWT 连接失败）→ 上线期可以用 LiveKit Cloud Ship（$50/月、1,000 并发），零运维；之后月房·小时持续 >≈2,500 两个月，再切到本目录的 GCP 自建。
- **T15 不通过**（Cloud 关不掉自动建房）→ 海外首发**直接用本目录的 GCP 自建**（`room.auto_create:false`），不经过 Cloud。原因：拿着未过期 JWT 的客户端能在 `DeleteRoom` 之后立刻重建房间，服务端硬顶与配额就不成立。
- 切换只换 Servers 下发的 `{url, token}` 与签发密钥，客户端零改动；客户端只接受 `config/visit_settings.py::VISIT_LIVEKIT_HOSTS` 里的主机名，新域名要随发版加进去。

## 规格、容量与成本（估算）

- **起步规格**：GCP 东京 `e2-standard-4`（4 vCPU / 16 GB），按设计稿引用的报价约 $125.51/月；第二区（法兰克福 / 俄勒冈）按用户分布再加。
- **容量**：LiveKit 默认按 400 轨/CPU 估，`e2-standard-4` 名义 1,600 轨，500 房 × 2 轨 = 1,000 轨刚够——**必须压测**，见下面验收清单第 5 条。
- **流量一律按十进制 MB / GB 算**：每房·小时出站 600 kbps × 3,600 s = 270 MB = 0.27 GB。GCP 出站报价按 GiB 计，引用时换算：0.27 GB ≈ 0.251 GiB（1 GiB = 1.073741824 GB）。走 TURN 中继的房出站翻倍。
- **盈亏点**：Cloud ≈$0.09/房·小时（参与者分钟 + 流量），自建 ≈$0.030/房·小时流量 + VM 固定成本，交叉点约 **2,500 房·小时/月**；持续两个月超过它再从 Cloud 切自建。

## 前置条件

1. 两个**真域名**，A 记录都指向这台机器的公网 IP：信令域名（如 `livekit.example.com`）与 TURN 独立域名（如 `turn.example.com`）。两张证书都由 Caddy 用 Let's Encrypt 自动签发（HTTP-01，经 80 端口）。
2. **防火墙**（GCP VPC 规则）只放行：
   - TCP 80（证书签发）、TCP 443（HTTPS 信令 + TURN/TLS）、TCP 7881（ICE/TCP）
   - UDP 3478（TURN/UDP）、UDP 50000-60000（ICE/UDP）
   - **不要**放行 7880、5349、8443：它们只给本机 Caddy 用。
3. Docker 与 Docker Compose v2。

## 构建 caddy-l4

官方 `caddy` 镜像不含 layer4 插件，普通 HTTP 反代转不了 TURN/TLS 的裸 TLS 流量，所以 `Caddyfile` 必须用带 [caddy-l4](https://github.com/mholt/caddy-l4) 的构建：

- **默认（compose 内联构建）**：`docker compose build caddy`。`docker-compose.yml` 里的内联 Dockerfile 用钉死版本的官方 `caddy:2.11.7-builder` 镜像执行 `xcaddy build --with github.com/mholt/caddy-l4@42db5690dea199f930a6f08005fe2e4aab10dcc9`，再把产物拷进 `caddy:2.11.7`。升级 Caddy 或插件时，compose 与本节两处一起改，并先 `caddy validate`。
- **手工构建二进制**（不用 Docker 时）：

  ```bash
  go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
  xcaddy build v2.11.7 --with github.com/mholt/caddy-l4@42db5690dea199f930a6f08005fe2e4aab10dcc9
  ./caddy validate --config Caddyfile --adapter caddyfile
  ```

- caddy-l4 仍在快速演进、Caddyfile 语法可能变：**换插件版本后先 `caddy validate`**。若某个版本的 Caddyfile 适配器不支持这里的写法，可改用 LiveKit 官方生成器（`docker run --rm -it -v$PWD:/output livekit/generate`）产出的 `caddy.yaml` 结构（`apps.tls.certificates.automate` + `apps.layer4`，配 `livekit/caddyl4` 镜像），路由语义与本 `Caddyfile` 相同：TURN 域名 → `127.0.0.1:5349`，信令域名 → `127.0.0.1:7880`（ALPN 只给 `http/1.1`）。

## 部署

```bash
cd deploy/livekit
cat > .env <<'EOF'
LIVEKIT_DOMAIN=livekit.example.com
TURN_DOMAIN=turn.example.com
ACME_EMAIL=ops@example.com
LIVEKIT_API_KEY=<与 Servers 共享的 key>
LIVEKIT_API_SECRET=<与 Servers 共享的 secret>
EOF
chmod 600 .env
# 把 livekit.yaml 里的 REPLACE_ME_TURN_DOMAIN、REPLACE_ME_LIVEKIT_API_KEY、Servers webhook 地址换成真实值
docker compose build caddy
docker compose up -d
```

API key / secret 只出现在本机 `.env` 与 Servers 的密钥托管里；客户端永远拿不到，JWT 由 Servers 签（HS256、`ttl=10m`、grants 按侧位收紧，见契约文档）。

## 关键配置与理由

| 键 | 值 | 理由 |
|---|---|---|
| `room.auto_create` | `false` | 房间只由 Servers 在 host 首次领凭证时 `CreateRoom`；`DeleteRoom` 之后旧 JWT 不能重建房间——服务端硬顶与配额的前提 |
| `room.max_participants` | `2` | 每房 host + guest 各一；Servers `CreateRoom` 也显式带 `max_participants:2` |
| `room.departure_timeout` | `60` | ≥ `VISIT_PEER_REJOIN_GRACE_S=35` 加重连余量。默认 20 s 会在双方同时闪断时把房间回收，`auto_create:false` 下就再也进不去；Servers `CreateRoom` 同样显式带 `departure_timeout:60`（上 Cloud 时只能靠它） |
| `room.empty_timeout` | `600` | = `VISIT_INVITE_WAIT_S`（邀请等待 10 min）；Servers `CreateRoom` 同样显式设置 |
| `rtc.data_channel_max_buffered_amount` | `1000000`（1 MB，十进制） | 每发送方数据通道 ≤5 KB/s，1 MB 够吸收重传突发，又不让失控客户端在 SFU 上无限堆积。**注意键在 `rtc` 段**：设计稿 PR-16 写作 `limit.data_channel_max_buffered_amount`，但官方 `config-sample.yaml` 里 `limit` 段没有这个键，它在 `rtc` 段下 |
| `turn.external_tls` | `true` | TURN 的 TLS 由 Caddy 在 443 终结，LiveKit 在 `tls_port`（5349）收已解密的流 |
| `webhook` | Servers 地址 | Servers 靠 `track_published`（档位 / 轨数 / 层数约束）、`participant_joined` / `participant_left`（入房证据 `joined_at`）、`room_finished`（释放并发名额）执行服务端约束 |

## 验收清单（上线前全部通过）

1. **只放行 TCP 443 的客户端能经 TURN/TLS 入房**：在一台出站只允许 TCP 443 的机器（或防火墙规则模拟）上用浏览器入房，`chrome://webrtc-internals` 里选中的 candidate pair 的本地候选是 `relay`、协议 `tls`，视频与数据通道都正常；再用 Firefox 测一次（Caddy 的 PROXY v2 头与 `turn.proxy_protocol: true` 缺一边时，TURN 会把 `127.0.0.1` 当映射地址回给客户端，Firefox 直接拒绝）。
2. **`DeleteRoom` 后未过期的 JWT 连接必须失败**：Servers `CreateRoom` → 签一张 10 min JWT → 入房成功后退出 → `DeleteRoom` → 用同一张 JWT 再连，必须失败、且服务端没有新建房间（`ListRooms` 里没有它）。
3. **未 `CreateRoom` 的房间名不能入房**：用合法签名、指向不存在房间的 JWT 连接必须失败（`auto_create:false` 生效）。
4. **第三人进不来**：同一房间第三个参与者入房被拒（`max_participants:2`）。
5. **压测是上线前置**：用 `livekit-cli load-test`（新版 CLI 命令名为 `lk load-test`）模拟 500 房、每房 1 路视频发布 + 1 个订阅者（共 1,000 轨），观察 CPU、丢包与 `qualityLimitationReason`；具体参数以所用 CLI 版本的 `--help` 为准。不达标 → 2 节点 + Redis，或换规格后重测。
6. **双方同时闪断能回来**：两端同时断网 20 s 再恢复，35 s 内都回到同一房间（房间未被 `departure_timeout` 回收）。

## N.E.K.O. 本体部署对串门的限制

这些限制针对的是用户运行的 N.E.K.O. 后端（不是本目录的 LiveKit 节点）：

- **反向代理部署不支持串门**，包括默认 `NEKO_BEHIND_PROXY=true` 的官方 Docker 镜像（`docker/entrypoint.sh` 用 Nginx 做前置代理）：代理头会改写 `client.host`，本机来源校验不可信，串门整体不可用（403 `VISIT_E_UNAUTHORIZED`），设置页隐藏「串门」分组并显示原因。
- **Docker / 局域网等非回环访问默认不支持串门**：串门的主闸是回环地址（浏览器与后端在同一台机器、经 `127.0.0.1` / `::1` 访问）。浏览器与后端不在同一台机器、或经容器网络访问时，设置页「串门」分组对非回环访问显示原因。
- **逃生开关 `NEKO_VISIT_ALLOW_NONLOCAL=1`**：打开后回环检查、代理模式拒绝、转发头拒绝都不再生效。**运维须在代理层自行鉴权，并覆盖（而不是追加）`X-Forwarded-For`，风险自负**——风险是任何能连上后端的客户端都能读邀请码与转录、代发串门台词。

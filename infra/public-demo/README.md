# 公网演示隧道 v0.2

更新日期：2026-10-13。

默认链路：浏览器 → HTTPS 域名 → Cloudflare 边缘 → 出站隧道 → 本机转发层（127.0.0.1:8011）→ VeriMind 后端（127.0.0.1:8000）。

**（2026-10-13 更新）`verimind.gym-flow.xyz` 已可用。** 此前判断"校园网/当前网络封禁 Cloudflare Tunnel 7844 端口"是**错的**，实测结论如下：

- 到边缘 `region1/region2.v2.argotunnel.com:7844` 的 **TCP 三次握手能成功**（`netstat` 可见 `ESTABLISHED`），但**不经代理时 TLS 数据会被丢弃**，表现为 `TLS handshake with edge error: i/o timeout`。
- 经 Clash 代理（`127.0.0.1:7897`）时，同一端口的 **TLS 1.3 握手完全正常**，能拿到 `CloudFlare Origin Certificate`（`CN=CloudFlare Origin Certificate`，`O=CloudFlare, Inc.`）。
- **关键限制：`cloudflared` 不读取 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量**（2026.7.3 实测：注入后仍直连真实 IP，且 precheck 反而更差）。因此不能靠给 cloudflared 设代理变量解决。
- **唯一可行方案：打开 Clash Verge 的「虚拟网卡模式」（TUN）。** 开启后出现 `Meta Tunnel` 网卡、默认路由指向 `198.18.0.2`，系统层接管全部流量，cloudflared 无需任何配置即可连上边缘。实测日志：

  ```
  INF Registered tunnel connection connIndex=0 ... location=lax01 protocol=http2
  INF Registered tunnel connection connIndex=1 ... location=lax10 protocol=http2
  precheck complete hard_fail=false suggested_protocol=http2
  ```

`start.ps1` 已加入 TUN 前置检查：未开 TUN 时会直接报错，不再默默重试。

域名为 `verimind.gym-flow.xyz`。隧道使用独立配置；原 `local.gym-flow.xyz` 的配置无需改动。电脑需要保持开机、联网，后端与隧道进程均需运行。

**登录方式（2026-10-13 最终确认）：共享口令已废弃，认证由 FastAPI 的账号体系负责。**

- `login_gateway.py` 已被重写为**纯转发代理**（文件头注释：`Loopback-only compatibility proxy; account authentication belongs to FastAPI.`），它**不再提供任何登录页**，只把请求转给 `127.0.0.1:8000`。
- 登录界面在**前端**：`web/src/components/AuthGate.vue`（标题「登录知证」、副标题「SECURE WORKSPACE」），走 `/api/auth/login`、`/api/auth/me`、`/api/auth/logout`（前端 store 见 `web/src/stores/auth.ts`，cookie 名 `verimind_session`）。
- 后端由 `app/main.py` 的 `require_authenticated_api` 中间件强制校验；`Caddyfile.example` 里也注明「Account authentication is enforced by VeriMind FastAPI」。
- **踩过的坑**：磁盘上的文件改了，但**已经在跑的进程仍是旧代码**。本次排查时 8011 上跑的还是旧版共享口令网关（进程启动于 19:58，而文件在之后被重写），8000 上的后端同样没有认证路由（后端启动于 19:58:11，而 `app/main.py` 改于 20:47:18、`app/auth_store.py` 改于 20:44:29）。**改完这些文件必须重启 8011 代理和 8000 后端。**

## 账号初始化与权限

- 账号库默认位于 `%LOCALAPPDATA%\VeriMind\app\accounts.sqlite3`，不写入 Chinook 或 AdventureWorks。可用环境变量 `VERIMIND_AUTH_DB_PATH` 指定独立位置。
- 首次只创建管理员，不设默认账号或密码。在仓库根目录用同一个 Windows 用户运行：

  ```powershell
  D:\python\python.exe -m app.auth_cli create-admin --username admin
  ```

  按提示输入至少 12 位密码。应用只保存加盐密码校验值。若后端以另一个 Windows 账号运行，需保持相同的 `VERIMIND_AUTH_DB_PATH`，否则它会看到另一份空账号库。
- 管理员忘记密码时，在同一账号和数据库路径下执行 `D:\python\python.exe -m app.auth_cli reset-admin-password --username admin`。重置会撤销该账号现有登录会话。
- 管理员可在网页右上角“账号”面板中创建普通用户或其他管理员、停用账号、重置密码。普通用户只有问答和来源查看权限；评测、项目证据、数据库结构与模型密钥配置仅限管理员。账号不开放自行注册。
- HTTPS 入口默认启用 `HttpOnly`、`Secure`、`SameSite=Lax` 会话 Cookie；登录有效期 12 小时。只在本机 HTTP 开发时，可在启动后端的当前 PowerShell 临时设置 `$env:VERIMIND_AUTH_COOKIE_SECURE='false'`。

运行配置和日志位于 `%LOCALAPPDATA%\VeriMind\public-demo`；隧道凭据保存在该目录。旧的共享演示密码不再用于账号认证。

## 启动与停止

### 首选：Cloudflare Tunnel（自有域名）

1. **先在 Clash Verge 里打开「虚拟网卡模式」（TUN）** —— 这是硬前提，不开则隧道必然连不上。
2. 先启动项目后端，使 `http://127.0.0.1:8000/health` 返回 200。
3. 执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\86133\WorkBuddy\多模态数据驱动的可解释精准问数问答智能体\infra\public-demo\start.ps1"
```

入口为 `https://verimind.gym-flow.xyz`，**实测全程可用**（登录页 200、口令登录 302、静态资源 200、`/api/database/status` 返回 `connected`）。

隧道注册是否成功，看运行目录 `tunnel.err.log`（或 `cf*.err.log`）里有没有 `Registered tunnel connection`。

### 不打开 TUN 时的备用出口：localtunnel

若不方便开 TUN，可用 localtunnel 作为兜底出口。先 `start.ps1 -GatewayOnly`，再在普通 PowerShell 执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\86133\WorkBuddy\多模态数据驱动的可解释精准问数问答智能体\infra\public-demo\start-localtunnel.ps1"
```

**但要知道 localtunnel 有两个坑（实测）：**

1. 客户端存活时间不长，公网会变成 `503 Tunnel Unavailable`（子域被服务端摘掉），需要重启。
2. **localtunnel 对浏览器 UA 会强制插入「隧道密码提示页」（返回 `511`）**，只对 `curl` 等非浏览器 UA 放行。因此**不能把它当作 Cloudflare Worker 反代的源站**（Worker 用非浏览器 UA 取到 302，浏览器侧仍被 511 拦住，两者打架）。

判断隧道是否健康，**要看公网出口的 HTTP 码，而不是进程在不在**：`302`=健康，`503`=客户端已被摘掉。

### 已废弃的做法

- **`hosts.ps1` 把边缘域名映射到本机**：那是为「本地代理桥」方案准备的。该方案在 TUN 可用后已无必要，**开着反而有害**——cloudflared 会去连本机（日志里 `ip=127.0.0.1`），报 `TLS handshake with edge error: EOF` 并无限重试。现在应当保持 `hosts.ps1 -Action Disable` 的状态。
- **Cloudflare Worker 反向代理到 localtunnel**：因上面第 2 条坑而不通，Worker 侧会返回 `530`。

停止本轮演示入口：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\86133\WorkBuddy\多模态数据驱动的可解释精准问数问答智能体\infra\public-demo\stop.ps1"
```

停止脚本仅结束记录中且可执行路径匹配的隧道和密码入口进程。网站后端单独管理。

## 验证

未登录访问域名会只收到独立登录页，不会下载工作台代码。

**`verimind.gym-flow.xyz` 的实测记录（2026-10-13 20:45，隧道注册成功后）：**

| 检查项 | 结果 |
|---|---|
| 公网访问首页 | `302` → `/login` |
| `/login` 页面 | `200`，HTML 含「知证」 |
| `POST /login`（正确口令） | `302`（写 cookie 放行） |
| `/assets/index-*.js` / `index-*.css` / logo | 均 `200` |
| `/api/database/status` | `{"status":"connected","database":"chinook","dialect":"postgres","table_count":14}` |

响应头含 `Server: cloudflare` 与 `CF-RAY: ...-LAX`，证明流量确实经过 Cloudflare 边缘。

**公网客户端的连通性必须单独实测，隧道进程存在不等于域名可用。** 实时注册连接见运行目录的 `tunnel.err.log`。

配置模板见 `Caddyfile.example`。使用其他电脑时需先安装 Caddy 和 cloudflared，并配置独立隧道凭据；账号库需独立备份，不能把真实密码或隧道凭据复制进源码。

# 微光域 LumiSphere

> 给 AI 一片可以自由浏览、又不会打扰你主机的专属空间。

[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![release](https://img.shields.io/github/v/release/ya-shua/lumisphere)](https://github.com/ya-shua/lumisphere/releases)
[![privacy](https://github.com/ya-shua/lumisphere/actions/workflows/privacy.yml/badge.svg)](https://github.com/ya-shua/lumisphere/actions/workflows/privacy.yml)

**仓库**：<https://github.com/ya-shua/lumisphere>

独立运行的浏览器沙箱（模式 B：本机浏览器模式）。
启动后右下角出现托盘图标，同时弹出一个**完全独立**的浏览器窗口——
它有自己的数据目录、自己的登录态，和你日常用的浏览器互不相干。

---

## 一、安装（只做一次）

双击 **`安装环境.bat`**，它做三件事：

1. 用 Python 3.11 创建虚拟环境 `.venv`
2. 安装依赖（playwright / pystray / pywin32 / pyyaml / pillow）
3. 下载 Chromium 内核（约 200MB）

> 需要系统里已装 Python 3.11。装在哪里都行，只要 `py -3.11` 能跑。

---

## 二、启动

双击 **`启动.bat`** 就行（不弹黑框，直接进托盘）。

**想让它开机自己起来**：双击一次 `设置开机自启.bat`，以后登录 Windows 它就自动在托盘里候着。
不想要了：双击 `取消开机自启.bat`。

启动后会看到：

- 右下角出现一颗蓝色小图标（托盘）
- 弹出一个浏览器窗口，打开 `config.yaml` 里配置的首页

**关掉浏览器窗口 ≠ 退出程序。** 窗口关掉后：

- 程序还在后台，托盘图标还在（会变成灰色，并弹一句提示）
- 右键 → 「启动浏览器」，2~3 秒重新开来，**并且回到你关掉之前那一页**

> 技术上 Chromium 在最后一个窗口关闭时会整个退出，外部程序拦不住这个动作。
> 所以这里的做法是"察觉 + 恢复"：记下你最后停的网址，重新打开时跳回去，用起来跟隐藏差不多。

---

## 三、托盘菜单

| 菜单项 | 作用 |
|---|---|
| **显示浏览器 / 启动浏览器** | 窗口在就切到前台；窗口被关掉了就重新启动它（双击图标也是这个） |
| **隐藏浏览器** | 窗口消失，但浏览器进程还在跑，AI 照样能操作、能截图 |
| **重启浏览器** | 关掉重开，用来解决卡死/页面异常 |
| **截图发到 QQ** | 把当前页面截个图发到你 QQ（要扫码登录时点这个最快） |
| **打开数据目录** | 直接打开 `data/profile`，可以看/删里面的东西 |
| **检查更新（vX.Y.Z）** | 问 GitHub 有没有新版；有就弹窗确认，一键覆盖代码 |
| **完全退出** | 关浏览器 + 退程序 + 托盘图标消失 |

> 「隐藏」和「关窗口」的区别：隐藏时浏览器进程一直在，恢复是瞬间的；
> 关窗口会让浏览器退出，恢复要重新启动（2~3 秒），但仍然是同一个登录态。

### 扫码登录怎么办

有些站（抖音、B站）登录态掉了会卡在扫码页。这时：

1. 右键托盘图标 → **「截图发到 QQ」**
2. 手机上收到图，扫一下就登录了
3. 登录态直接写进 `data/profile`，以后不用再扫

发出去的是**整页**；如果页面上找得到二维码，会改成**只发二维码那一块**（一般 30KB 左右，一秒到）。

判断"要不要扫码"的标准是**网址本身是不是登录页**（含 `login` / `passport` / `signin` 之类）。
不看页面上有没有二维码——抖音首页正常浏览时也挂着一个"扫一扫"的引导图，光看二维码会误判。

### AI 自己该怎么走这个流程

发图只是第一步，AI 得知道自己发完该干嘛：

```bash
# 1. 先看是不是卡在登录页
curl http://127.0.0.1:8765/login/detect
# → {"need_login": true, "has_qr": true, ...}

# 2. 把二维码推到 QQ（只发图，不带文字）
curl -X POST http://127.0.0.1:8765/login/notify
# → {"ok": true, "bytes": 30468, "hint": "图已发出，等对方扫码..."}

# 3. 阻塞等对方扫码（最多等 120 秒）
curl -X POST http://127.0.0.1:8765/login/wait -d '{"timeout":120}'
# → {"ok": true, "logged_in": true,  "waited": 6.0, "url": "..."}
# → {"ok": true, "logged_in": false, "waited": 120, "reason": "超时..."}
```

`logged_in: true` 就可以接着干活；`false` 说明没扫成（多半是二维码过期了），重新 `notify` 一次即可。

---

## 四、登录态怎么保持

`data/profile/` 就是这个浏览器的"硬盘"。

- 第一次：在里面登录抖音 / B 站
- 之后：关掉程序、下次启动，**还是登录状态，不用重新扫码**
- 想清空：托盘 →「打开数据目录」→ 把整个 `profile` 文件夹删掉（或直接删 `data/profile`）

什么时候需要重新登录：手动删了目录、平台自己让登录过期、换了网络环境。

---

## 五、给插件调用的本地接口

配置在 `config.yaml` 的 `server` 段，默认 **`http://127.0.0.1:8765`**，只监听本机。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/status` | 返回运行状态、当前网址、标题；`tabs` 是标签页数量 |
| GET | `/text?max_chars=20000` | 读取当前页面的正文（纯文字） |
| GET | `/tabs` | 列出当前所有标签页的网址和标题 |
| GET | `/screenshot` | 返回当前页面截图（PNG） |
| GET | `/screenshot/base64` | 同上，但包成 JSON |
| POST | `/open` | `{"url": "https://..."}` 打开一个网址 |
| POST | `/search` | `{"keyword": "...", "num": 10, "engine": "bing"}` 搜索网页 |
| POST | `/fetch` | `{"url": "...", "max_chars": 20000}` 抓网页正文 |
| POST | `/tabs/clean` | 立刻手动清理多余的标签页，返回关了几个 |
| GET | `/login/detect` | 判断当前页是不是要扫码登录（不发送任何东西） |
| POST | `/login/notify` | 把当前页二维码/截图发到 QQ，返回发没发成功 |
| POST | `/login/wait` | `{"timeout": 120}` 阻塞等扫码完成，返回 `logged_in` 是 true 还是 false |
| POST | `/browser/start` | 浏览器被关掉了就重新启动它（恢复到上次的页面） |
| POST | `/window/hide` | 隐藏浏览器窗口 |
| POST | `/window/show` | 显示浏览器窗口 |
| POST | `/quit` | 让微光域退出 |

不想要这个端口：把 `config.yaml` 里 `server.enabled` 改成 `false`。

### 联网能力（搜索 / 抓正文）

和 AstrBot 那类"联网插件"的作用一样，但走的是**真实浏览器**：

- 页面是真的渲染过的，JS 站点也能拿到内容
- 带着你自己的登录态，登录后才可见的内容也能读
- 抓正文时会先剔掉 script / style / nav / header / footer / 广告位

```bash
# 搜索结果：{title, url, snippet}
curl -X POST http://127.0.0.1:8765/search \
     -H "Content-Type: application/json" \
     -d '{"keyword":"DeepSeek Harness","num":5,"engine":"bing"}'

# 抓正文：{title, url, text, chars}
curl -X POST http://127.0.0.1:8765/fetch \
     -H "Content-Type: application/json" \
     -d '{"url":"https://example.com","max_chars":20000}'

# 读当前正在看的那一页
curl "http://127.0.0.1:8765/text?max_chars=5000"
```

`engine` 支持 `bing`（默认）和 `baidu`。

> **推荐用 bing**：返回的是真实网址，可以直接丢给 `/fetch` 用。
> 百度返回的是 `baidu.com/link?url=...` 跳转链，还得再跳一次才能拿到真实地址，而且广告位结构经常变。
> 两个引擎都会自动跳过推广位。

> 搜索和抓取用的是一个**专用的后台页面**，不会盖掉你正在看的页面，也不会被标签页限额当成多余标签关掉。

---

## 六、配置说明

`config.yaml`：

```yaml
browser:
  profile_dir: data/profile   # 浏览器数据目录（登录态在这）
  channel: ""                 # "" = 自带 Chromium；"chrome" / "msedge" = 系统浏览器
  start_url: "https://www.bilibili.com"
  width: 1366
  height: 860

server:
  enabled: true
  host: 127.0.0.1
  port: 8765
```

### 关于 `channel`（用哪个内核）

| 值 | 用的是什么 | 说明 |
|---|---|---|
| `""` | Playwright 自带 Chromium | **推荐**。专为自动化构建，实测最稳 |
| `"chrome"` | 系统安装的 Google Chrome | 稳定，但要求本机装了 Chrome |
| `"msedge"` | 系统安装的 Edge | 免下载，但**自动化下不稳定**：打开视频站约 30~45 秒后页面会被关闭（Edge 自身的节能/挂起策略），不推荐 |

### 关于 `limits`（标签页限额）

防止 AI 一口气开几十个视频页把电脑拖死。默认每 2 秒检查一次，超了就自动关掉多余的。

```yaml
limits:
  max_tabs: 6           # 总共最多几个标签页
  one_per_domain: true  # 同一个域名只留一个（抖音开第二个就把旧的关掉）
  max_per_domain: 2     # one_per_domain 关掉时才看这个
```

规则细节：

- **同一个域名只保留最新的那一个**。抖音的每个视频网址虽然不同，但域名都是 `douyin.com`，所以开第二个就把前一个关掉。
- **正在用的那个标签页永远不会被关**。
- `www.douyin.com` 和 `douyin.com` 算同一个站。

实测：一次开 10 个标签（5 个抖音页），2 秒内收敛到 5 个（4 个不同网站各 1 个 + 空白页）；再补开 9 个，稳定仍是 5 个。

> 如果访客只是"同一个视频换个网址"，也不会堆——抖音所有视频页共享一个名额。

> 实测记录：用 `msedge` 打开 B 站，页面会在 30~45 秒后自己关掉；换成自带 Chromium 后连续运行 2 分钟以上无异常。

---

## 七、目录结构

```
微光域/
├─ main.py              入口
├─ config.yaml          配置
├─ 启动.bat             双击启动（不弹黑框）
├─ 安装环境.bat         双击安装（换电脑才用）
├─ 设置开机自启.bat     让它开机自动进托盘
├─ 取消开机自启.bat     取消上面的设置
├─ core/
│   ├─ browser.py       浏览器生命周期（独立 profile）
│   ├─ window.py        窗口显示/隐藏（Win32）
│   ├─ tray.py          托盘图标与菜单
│   └─ server.py        本地控制端口
├─ data/profile/        浏览器数据（登录态、缓存）
└─ logs/lumisphere.log  运行日志
```

---

## 八、常见问题

**Q：启动报「浏览器启动失败」？**
程序启动时会**自动清理**上次留下的 `SingletonLock` / `SingletonSocket` / `SingletonCookie`，所以直接关机、强杀进程之后一般都能自己恢复。
如果还是失败：先确认 Chromium 装好了（`.venv\Scripts\playwright.exe install chromium`）；
再不行就把整个 `data/profile` 删掉重建（登录态会丢，重新登一次即可）。

**Q：浏览器跑着跑着自己关掉了？**
检查 `config.yaml` 里的 `channel`。用 `msedge` 时 Edge 会在几十秒后关掉页面，改成 `""`（自带 Chromium）即可。

**Q：浏览器窗口关掉后，怎么叫回来？**
右键托盘图标 →「启动浏览器」（或双击图标），2~3 秒重新开来，**并回到你关掉之前那一页**。
关窗口时托盘图标会变灰并弹一句提示，程序本身一直在。

**Q：关机重启之后还要重新设置吗？**
不用。登录态在 `data/profile` 里，重启后照旧。
如果设过开机自启，登录 Windows 后它会自己出现在托盘里。

**Q：会不会开太多标签页把电脑卡死？**
不会。`config.yaml` 里有 `limits` 限额，默认每 2 秒检查一次：
同域名只留一个、总数不超过 6 个，超了自动关掉最旧的。想更严就把 `max_tabs` 调小。

**Q：它会不会和我的 Chrome 合并？**
不会。它用独立的数据目录，Chromium 认目录，不认人。两边同时开也互不影响。

**Q：怎么彻底删掉它？**
关程序 → 删掉整个「微光域」文件夹。系统里没留任何东西（没有系统服务、没有注册表项）。

---

## 九、MCP 工具（让 AI 直接调）

`mcp_server.py` 把上面那些 HTTP 接口转成了 MCP 工具，这样 AI 不用写代码请求端口，
而是像调 `bili_search` 一样直接调。

已经注册进 DSH 的 `cordis.patch.yml`（`mcp-lumisphere` 条目），**改完要重启一次 DSH 才生效**。

| 工具 | 干什么 |
|---|---|
| `lumisphere_status` | 看现在开着什么页面 |
| `lumisphere_open` | 打开网址 |
| `lumisphere_text` | 读当前页正文 |
| `lumisphere_search` | 在域里搜索（带登录态） |
| `lumisphere_tabs` | 看标签页 |
| `lumisphere_shot` | 截当前页，直接把画面给模型 |
| `lumisphere_video_probe` | 探当前页有没有视频、多长 |
| `lumisphere_video_frames` | 按时长比例抽 1-6 帧，画面直接给模型 |
| `lumisphere_login_check` | 看是不是卡在登录页 |
| `lumisphere_login_notify` | 把二维码发到 QQ |
| `lumisphere_login_wait` | 阻塞等扫码完成 |
| `lumisphere_send_qq` | 把当前页截图发到 QQ |

### 看视频的完整链路

```
bili_search("关键词")                    → 拿到 BV 号
lumisphere_open("https://bilibili.com/video/BV...")
lumisphere_video_frames(count=3)         → 画面
lumisphere_text()                        → 简介/置顶评论
bili_subtitle(bvid)                      → 台词
                                         → 画面 + 台词一起判断
```

**字幕说它讲什么，抽帧说它拍什么。** 两个都有才算真看过。

### 前提

- 微光域得在跑（双击 `启动.bat`，或者开机自启）
- 域没启动时调工具会直接报「连不上微光域」，不会卡住

---

## 十、更新

程序启动时会在后台问一次 GitHub 有没有新版本（不耽误启动）。
有的话托盘弹个通知，右键图标 →「检查更新」就能看到。

点下去会：

1. 下载最新 release 的源码包
2. 只覆盖代码文件（`main.py`、`core/`、`scripts/` 等）
3. `config.yaml`、`data/`、`logs/` 一律不动
4. 提示你重启程序生效

也可以自己去 releases 页面下载：
<https://github.com/ya-shua/lumisphere/releases>

**为什么只覆盖代码**：`data/profile` 里是你的登录态，丢了就得重新扫码；
`config.yaml` 里是你的网关地址和 QQ 号。更新不该碰这些。

---

## 十一、隐私门禁

仓库里有两道门，防止把不该出门的东西推上去。

**第一道：本地 pre-commit**

```bash
git config core.hooksPath .githooks   # 每个克隆只做一次
```

之后每次 `git commit` 都会先扫一遍暂存区，命中就拒绝提交。

**第二道：GitHub Actions**

`.github/workflows/privacy.yml`，推上去之后自动跑
`python scripts/check_privacy.py --all`。本地 hook 被 `--no-verify` 跳过也躲不过。

**它查什么**

| 类型 | 例子 |
|---|---|
| 路径 | `config.yaml`、`data/`、`logs/`、`.venv/`、`*.db`、`*.log` |
| 密钥 | `access_token:`、`password:`、`ghp_...`、`sk-...`、私钥 |
| 身份 | 手机号、配置里的 QQ 号 |
| 网络 | 内网 IP（10.x / 172.16–31.x / 192.168.x） |
| 已知值 | 真实 token / QQ 号 / 昵称 —— 存在本地 `scripts/.private_values.json`，不进仓库 |

**误报了怎么办**

把那个值换成占位符（比如 `access_token: ""`），
或者在 `scripts/check_privacy.py` 的 `ALLOW_PATTERNS` 里加白名单。
**别用 `git add -f` 或 `--no-verify` 绕过去** —— CI 那边还会红。

---

## 十二、运行模式与安全

### 三种跑法

`config.yaml` 里的 `runtime.mode` 决定浏览器跑在哪、端口给谁看：

| mode | 浏览器在哪 | 端口 | token |
|---|---|---|---|
| `local`（默认） | 本机 | 只绑 `127.0.0.1` | 可不填 |
| `bridge` | 本机 | 只绑 `127.0.0.1`，靠隧道接给云端 | **必填** |
| `cloud` | 云服务器 | 对外监听 | **必填** |

**bridge 怎么用**：云端那台机器能连到你电脑，就靠一条 SSH 反向隧道。

在你自己的电脑上执行：

```bash
ssh -N -R 8765:127.0.0.1:8765 用户名@你的云服务器
```

之后云端访问 `127.0.0.1:8765`，连到的就是你这台电脑上的浏览器。
隧道是加密的，端口也不会暴露到公网。

为什么这么设计：Chromium 很吃内存和 CPU，自己的电脑跑得动，
就不必为了「让云端 AI 用」花钱买一台高配云服务器。

### 四道安全闸门

这个端口的权限很大 —— 能开网页、截屏、读页面正文。所以：

1. **危险配置拒绝启动**：监听地址不是本机、又没配 token，程序直接不开端口，
   并写清楚原因。不会默默把浏览器交给别人。
2. **token 鉴权**：除纯本机模式外，所有请求必须带 `X-Auth-Token` 请求头。
   比较用的是恒定时间比较，防止被试探。
3. **Host / Origin 校验**：防 DNS rebinding ——
   恶意网页把自己的域名解析到 `127.0.0.1` 想读本地服务，会被 403 挡掉。
   这一条在纯本机模式下也生效。
4. **限流**：每个 IP 每分钟有请求上限，防止端口被当成跳板刷。

### 更新完整性

一键更新不是下下来就覆盖。它会去 GitHub 取每个文件的指纹，
逐个和下载的包比对，**任何一处对不上就中止，一个文件都不写**。
所以网络被劫持、或者包下到一半，都不会把坏代码装进来。

### 云上还要多做一步

`cloud` 模式的端口是对外的，**一定要套 HTTPS 反向代理**（Nginx + 证书）。
否则 token 在网络上是明文传输的，等于没设。

---

## 十三、下一步（还没做）

- 模式 A：虚拟机隔离（WSL2），接口不变，换执行后端
- 域名白名单生效
- LumiCabin 壁纸联动（Webhook）
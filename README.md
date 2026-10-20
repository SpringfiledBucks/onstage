> 本轮补充：PostgreSQL-only；PWA增加按浏览器/房间主动开启的后台消息通知，维护者需配置VAPID Secret并验证真实设备通道。赞踩同局唯一、跨局防刷限额不向普通用户显示。详见docs/SELF_HOSTED.md及docs/API_CONTRACT.md。

部署接手请先读 [给 Neboer 的部署升级说明](docs/NEBOER_DEPLOY_UPGRADE_20261020.md)：包括新PG/既有PG迁移、VAPID Secret、PWA启用、真手机验收、实际CI阻塞与回退边界。

# Onstage · 少女乐队对戏

## 0.9 账户与社区候选

本分支实现用户名/密码账户、可信前端与独立管理前端、千级房间分页/排序、私密邀请或密码入场、角色偏好和招募、场景切换、全演员离线10分钟自动结束、匿名账户唯一评分、私密成员赞踩/举报、作者授权的记录分享、开发反馈与监控。**访客仅保留公开目录、场景、关于和主动分享的低风险读取；入房及写操作需要登录。** 登录名仅本人和管理员可见，房间内使用独立化名。下文0.8数据是历史验收，不代表这些新功能已上线。

用户端与管理端为独立 React/TypeScript/Vite 静态构建；实时聊天暂复用成熟聊天渲染器，保持发送对账、草稿、贴纸和历史兼容。后端继续 FastAPI 模块化单体与独立 PostgreSQL，不同时进行语言重写。SQLAlchemy短事务保留，新增功能使用 Alembic 显式追加迁移；生产启动不改库。当前服务与运维入口仅支持 PostgreSQL，缺少 DATABASE_URL 会明确失败，不再创建、打开、迁移或导入旧 SQLite 数据库。旧文件保留原处，不导入旧历史；历史 Store 算法及 Sites/D1/离线适配仅保留回归用途，不作为新账户服务部署路径。

```sh
npm ci --ignore-scripts
npm run check:frontends
npm run test:frontends
npm run build:frontends
# build/web、build/admin 分别部署；通过各自 config.js 配置 API/WS Origin。
python tools/database.py migrate
```

隔离 Compose 可运行 `app web admin`，用户端 `http://localhost:8080`、管理端 `http://localhost:8081`、API `http://localhost:8765`，均仅供本机开发。实际外部部署、白名单、Cookie、数据库备份和回退见 [SELF_HOSTED](docs/SELF_HOSTED.md)。首次管理员在注册后由获准维护者通过 `python tools/accounts.py --help` 所示命令授予，不自动创建管理账户。未发布新镜像digest前不要把源码候选当作已部署版本。

公开部署必须配置 `ONSTAGE_FRONTEND_ORIGINS` 和 `ONSTAGE_ADMIN_ORIGINS` 的准确 HTTPS Origin。`*.neboer.site` 只对公开读接口开放且不消费其Cookie；账户读写和WS仅白名单。生产使用 `__Host-onstage_session`，本机HTTP使用另一个Cookie名；旧Cookie不自动迁移，也不按昵称认领历史。

0.9当前证据与限制以 [acceptance](docs/acceptance.json) 为准。Python功能与接口稳定后再依据性能实测评估其他后端语言；1000个累计房间本身不构成重写或拆服务的依据。

## 0.8 历史基线

免注册昵称入场的 MyGO!!!!! / Ave Mujica 真人文字对戏空间。十个角色、自由场景、文字和贴纸、舞台/后台、旁观、房主管理、举报与历史恢复；没有 AI 代演或假在线。

历史开发版本 **0.8.0**（以下功能记录保留，当前启动方式见本地开发节）。公开协作仓库：[SpringfiledBucks/onstage](https://github.com/SpringfiledBucks/onstage)。自托管使用 FastAPI、独立 PostgreSQL、WebSocket 提醒及 HTTP 游标补齐。该历史版本曾保留 SQLite 开发路径；当前入口已废弃，Sites/D1仅作历史兼容。首个支持部署为 **1 应用副本、1 Uvicorn worker**；目标集群部署由合作方负责，本仓库不宣称已经上线。

## 本轮改动

- 完整保留185张原始美术及历史路径，新增经过RGBA/ICC验证的无损派生与哈希清单，整套传输21.73→16.31 MB（减少24.91%）。哈希图片长期缓存，HTML/脚本/清单重新校验，API保持no-store，文本启用gzip。新增消息保留已有图片节点。
- PWA安装清单与同源美术持久缓存；使用设置可主动保存完整素材、查看进度、取消或清除本应用缓存。首次打开不预载全图，聊天/身份/举报不进入此缓存，离线不能多人对戏。需HTTPS或localhost，浏览器可能清理缓存。
- 房间列表每页24间，后端默认50/最多100，服务端筛选与稳定游标支持大量历史房间；以1107间隔离房间验证，未提高活跃房/连接容量上限。
- 结束房默认从开头回看并提供“跳到开头”；聊天提供常驻“跳到最新消息”。默认Enter发送、Shift+Enter换行，使用设置可改回Ctrl/⌘+Enter发送，保留IME保护。
- 重构评估见[ARCHITECTURE](docs/ARCHITECTURE.md)：推荐保持协议的模块化，估算12–21人日；原生客户端、语言重写与拆服务暂缓。此为后续规划估算。

- 点击发送立即在聊天流显示本人消息及小型状态圈；可靠确认后成为一条正常消息，无发送条幅或成功 toast。超时显示该条未确认状态，可使用同一 ID、原始内容重试。
- ACK 与同步使用身份范围、房间、实际成员、频道及 `client_message_id` 对账；同步先确认后，晚到错误不能倒退。POST 不推进同步游标；新文字草稿与贴纸发送互不覆盖。
- PostgreSQL 采用 SQLAlchemy/psycopg、独立 schema、显式迁移与短事务。会话限流锁、房间/举报行锁保留角色唯一、消息幂等、游标、权限和原子系统事件；不同房间不共用消息写全局锁。
- 写入确认不等待慢 WebSocket 接收者；提示按连接/房间有界合并、超时清理，断线仍从数据库补齐。日志不记录正文和凭据。
- 非 root Docker 镜像、隔离开发 Compose、真实 PostgreSQL CI、容器重启/替换持久化检查及受控 GHCR 发布。

源码来自 `onstage-v0.6.0-codex-source.zip`，原提交 `26305f3250c1fc34ab4a190a4ec429ab705a79f8`。ZIP 无 Git 历史；本仓库提交为真实接手历史，未伪造原提交。来源见 SOURCE_REVISION.json。代码许可尚未选择，第三方素材不因此取得再分发许可；素材来源与许可边界见 [OPEN_SOURCE](docs/OPEN_SOURCE.md)。

## 本地开发

需要 Python 3.12；普通 Python 服务不需要 Node 构建。

```sh
python -m venv .venv
# Windows 使用 .venv\Scripts\python.exe，Linux/macOS 使用 .venv/bin/python
.venv/bin/python -m pip install -r requirements-dev.txt
# 先通过本机受控环境设置指向隔离 PostgreSQL 的 DATABASE_URL
.venv/bin/python tools/database.py migrate
.venv/bin/python tools/run_local.py
```

该命令仅启动 loopback PostgreSQL 开发实例，必须先配置DATABASE_URL并受控迁移；不等于手机联机或目标部署。PostgreSQL 与镜像运行见 [SELF_HOSTED](docs/SELF_HOSTED.md)，生产凭据通过环境/Secret 注入，不写命令参数或源码。迁移须单独执行，应用启动只检查 PG schema。

```sh
# DATABASE_URL 已由受控环境设置，指向获准的隔离 PostgreSQL
python tools/database.py migrate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8765 --workers 1 --no-proxy-headers
```

必须设置精确 `ONSTAGE_PUBLIC_ORIGIN`；公网 `ONSTAGE_SECURE_COOKIE=1`。当前不迁入旧SQLite历史：保留旧文件并停用旧写入口，新站从迁移后的空PostgreSQL开始。PostgreSQL版本升级仍需备份和隔离恢复验证。跨域Cookie不自动迁移，不能凭同名昵称接管历史身份。

## 验证与交付

```sh
# 在受控环境设置 ONSTAGE_TEST_DATABASE_URL，指向授权的隔离 PostgreSQL。
# 全套 HTTP/数据库测试仅创建/删除随机 schema，不以生产 DATABASE_URL 作为测试授权。
python -m pytest -q
npm ci --ignore-scripts
node tests/frontend_delivery_check.cjs
node tests/frontend_navigation_check.cjs
node tests/pwa_cache_check.cjs
node tests/preview_adapter_check.cjs
node edge/freeze.test.mjs
node edge/room-pagination.test.mjs
```

本次准确结果、失败和未验证项见 [acceptance](docs/acceptance.json)。旧 Sites 0.6 检查是历史证据，不能代替当前 PG/浏览器/镜像验收。真人三人、真实手机、大陆普通网络和现场延迟由实际部署参与者补验；localhost、DOM 和 CI 均不代签。

- [API_CONTRACT](docs/API_CONTRACT.md)：消息、权限和游标协议。
- [RUNBOOK](docs/RUNBOOK.md)：维护、备份、恢复及唯一写入口。
- [ARCHITECTURE](docs/ARCHITECTURE.md)：持久化、锁和通知边界。
- [DEMO](docs/DEMO.md)：演示和实机验收。
- [SELF_HOSTED](docs/SELF_HOSTED.md)：容器、CI、Kubernetes 交付合同。


## 完整美术与部署

按仓库所有者于2026-10-18的明确要求，仓库已包含原包全部 **185 个原始美术文件**，包括角色图、乐队图、场景图和143张贴纸；恢复原始资源路径，逐文件身份见 `docs/ASSET_MANIFEST.json`。克隆即可使用完整内容，不需要再下载素材包或运行恢复工具。

```sh
git clone https://github.com/SpringfiledBucks/onstage.git
cd onstage
docker compose -f compose.dev.yaml up -d db
docker compose -f compose.dev.yaml run --build --rm migrate
docker compose -f compose.dev.yaml up --build -d app
```

此命令是隔离开发演练，打开 `http://localhost:8765`；真实多人对外部署按 [SELF_HOSTED](docs/SELF_HOSTED.md) 配置公共 Origin、独立 PostgreSQL、TLS/WS代理和单副本。素材已随Git和Docker构建上下文交付，来源信用与代码许可分别记录；没有替第三方新增许可声明。

## 当前交付状态

0.8.0开发、验证和公开推送完成，功能提交[0f6ac0e](https://github.com/SpringfiledBucks/onstage/commit/0f6ac0e75445fcd5baf14d06952244738374e509)。本轮Python 158通过/2项Windows权限跳过，真实PG17测试15通过；千房D1、21步真实HTTP/WS DOM、PWA/发送/导航回归及实际窄视口浏览器缓存/刷新检查通过。范围、带宽正文实测和限制见[本轮证据](reports/optimization-20261018.json)。Neboer已接受write协作权限。

[本提交实际Actions回执](https://github.com/SpringfiledBucks/onstage/actions/runs/39133008366)显示账户账单锁，validate没有任何执行步骤、runner未启动，不能记为CI通过；未生成实际镜像或digest。本机未装Docker，未操作账单或生产部署。账户恢复后可重跑本次run；main手动发布仍需全部验证通过。物理手机安装/软键盘/OS重启、大陆与现场网络、集群部署另行验收。原生客户端暂缓。详见docs/acceptance.json。

# 给 Neboer 的部署与升级交接

日期：2026-10-20。接收人：[@Neboer](https://github.com/Neboer)。范围：协作者环境的部署准备、受控升级及现场验收；本文不是已部署成功的证明。

## 1. 当前版本与发布边界

- 仓库：[SpringfiledBucks/onstage](https://github.com/SpringfiledBucks/onstage)；评审入口：[PR #1](https://github.com/SpringfiledBucks/onstage/pull/1)，目前为 draft，未合入 main。
- 本轮功能源码固定为 `8cb767e0c03b0a2804afcde40c5bca12e1602660`；后续 `9759c88` 只补验收回执。部署时记录准确源码 SHA，并固定实际镜像 digest，不能只写 `0.9.0` 或 `latest`。
- API：FastAPI，**一个副本、一个 Uvicorn worker**，独立 PostgreSQL。用户端和管理员端为两个独立静态前端；成熟聊天暂以用户端同源 `/play/` iframe 复用。
- 当前运行与运维入口只接受 PostgreSQL。**旧 SQLite 文件直接退出使用：不打开、不导入、不自动迁移、不删除。** 不按相同昵称接管旧房主权限；新站从注册账户开始。
- 原始美术、历史协议及来源记录仍保留。不要将公开仓库的代码许可推断为第三方美术的权利授权。

### 目前没有可交付的已验证镜像

[Actions run 39406099090](https://github.com/SpringfiledBucks/onstage/actions/runs/39406099090) 对应上述功能 SHA。GitHub 返回：

> The job was not started because your account is locked due to a billing issue.

`validate` 的 steps 为空，`publish` 跳过。因此 **Linux 容器构建、容器启动/替换、GHCR 发布和镜像 digest 均未由这次 CI 验证**；不存在本文能提供的实际 registry digest。本机也没有 Docker，未在本机安装 Docker/WSL。

仓库账户计费问题由仓库所有者处理。恢复后先让准确功能提交的 PR 验证实际成功；受控合入后，通过 main 的手动工作流或版本发布执行镜像发布。仅推送 feature 分支不会发布生产镜像。不要把更新 `imagePullPolicy` 当作镜像自动跟踪。

如需由 Neboer 在已有构建环境先验证，可以在干净 checkout 固定上述 SHA，分别构建以下目标；这是**待执行命令，不是成功记录**：

```sh
# 在干净源码目录，确认 git rev-parse HEAD 是固定的功能 SHA。
docker build --platform linux/amd64 \
  --build-arg SOURCE_REVISION=8cb767e0c03b0a2804afcde40c5bca12e1602660 \
  --build-arg SOURCE_REPOSITORY=https://github.com/SpringfiledBucks/onstage \
  -t onstage-api:review-8cb767e .
docker build --platform linux/amd64 -f Dockerfile.frontend --target web \
  --build-arg SOURCE_REVISION=8cb767e0c03b0a2804afcde40c5bca12e1602660 \
  --build-arg SOURCE_REPOSITORY=https://github.com/SpringfiledBucks/onstage \
  -t onstage-web:review-8cb767e .
docker build --platform linux/amd64 -f Dockerfile.frontend --target admin \
  --build-arg SOURCE_REVISION=8cb767e0c03b0a2804afcde40c5bca12e1602660 \
  --build-arg SOURCE_REPOSITORY=https://github.com/SpringfiledBucks/onstage \
  -t onstage-admin:review-8cb767e .
```

工作流预期发布到 `ghcr.io/springfiledbucks/onstage`、`onstage-web`、`onstage-admin`，但**目前这些地址不是本版本已发布的证据**。也可复用合作方已有 registry；先实际构建、启动、替换与持久化验证，再推送并记录 registry 返回的三个不可变 digest。源码仓库写权限不代表已有 GHCR package 写权限。不要共享个人令牌。

首个构建目标为 `linux/amd64`；ARM 节点须另行构建与验证，不能直接假设兼容。前端 Node/nginx 基础标签目前未锁 digest，正式冻结还需记录基础镜像来源与最终产物 digest。原 npm 图仍有 28 项审计告警，涉及历史 Next/Sites 等工具链，不能宣称已消除；当前静态前端不运行 Next/Node 服务，但这不能代替依赖评估。

## 2. 部署前确定的配置

域名、namespace、registry、数据库与现有 GitOps 机制尚未由 Neboer 确认。下列域名仅为**示例待替换值**：

| 组件 | 配置与端口 | 验证 |
| --- | --- | --- |
| API | `8765`；`ONSTAGE_PUBLIC_ORIGIN=https://api.neboer.site` | `/healthz` 存活；`/readyz` 实际检查数据库与迁移 |
| 用户端 web | 静态容器 `8080`；示例 `https://play.neboer.site` | `/healthz`；注册、入房及 `/play/` |
| 管理端 admin | 独立静态容器 `8080`；示例 `https://admin.neboer.site` | `/healthz`；独立登录与管理权限 |
| PostgreSQL | 现有独立持久数据库；`DATABASE_URL` 经 Secret 注入 | 备份、隔离恢复、显式迁移 |
| 通知密钥 | 持久 P-256 PEM Secret，只读挂载给 API | 进程 UID 10001 可读；通知配置接口启用 |

API 设置准确的 `ONSTAGE_FRONTEND_ORIGINS` 和独立的 `ONSTAGE_ADMIN_ORIGINS`，均为逗号分隔 Origin、不带路径；`ONSTAGE_SECURE_COOKIE=1`，不要启用 `ONSTAGE_LEGACY_GUESTS=1`。用户端/管理端分别设置 `ONSTAGE_API_ORIGIN` 与 `ONSTAGE_WS_ORIGIN`，例如 `https://api.neboer.site`、`wss://api.neboer.site`。不要为所有子域添加 credentialed CORS；泛域仅用于匿名公开读取。生产 Cookie 为 API host-only `__Host-onstage_session`，不设置 `.neboer.site` Cookie Domain。

代理保持 Origin/Cookie，允许 WebSocket Upgrade，关闭 WS 缓冲并允许至少 75 秒空闲读取。用户端 SPA 回退不能吞掉 `/static/`、`/assets/`、`/play/` 或 `/sw.js` 的真实错误；`/sw.js` 必须正确 JS MIME、根 scope。HTML、配置和 API 不长期缓存；仅成功的内容哈希美术/构建资源长期缓存，404 不缓存。不要修改无关全局代理、Headscale 或路由。

现有 [kubernetes.yaml](../deploy/kubernetes.yaml) 和 [migrate-job.yaml](../deploy/migrate-job.yaml) 是 **API 的占位模板**：尚未填入真实 namespace、digest、准确 Origin，也没有包含完整的双前端部署或 VAPID 挂载。维护者需在已有部署配置中补齐，不能直接原样上线。前端启动脚本会写 `config.js` 和 nginx 配置，不能直接照搬 API 的只读根文件系统设置；须提供合适可写路径或沿用前端默认文件系统并验证。

API 更新使用 `Recreate`；先确认旧 API/旧 Job 已退出，再启动新实例。也要排除另一 Deployment、namespace、人工 Pod 或预览环境连接同一生产数据库；共同数据库不提供跨副本 Hub、presence 或权限撤销。资源模板中的 request/limit 不是容量实测，千级累计房间分页也不代表千个活跃房并发。

## 3. PostgreSQL 新部署与升级

所有命令在准确 release 镜像/源码中执行，连接信息由受控环境或 Secret 提供，**不要将实际 DATABASE_URL、密码、私有快照放入 GitHub、聊天、命令参数或日志**。迁移是一次独立受控步骤，不能放在每个 Pod 的启动脚本/initContainer。

### A. 只有旧 SQLite，按新库重新开始

1. 保留旧文件及旧部署现场，停止旧写入口，明确新的唯一主站。不要运行 SQLite 导入、也不要自行删除旧文件。
2. 准备独立空 PostgreSQL 数据库/专用 schema 和对应 Secret；核对不能指向其他项目或生产库副本。
3. 在空目标运行：

```sh
# 此步骤的 DATABASE_URL 已由受控 Secret 指向新目标。
python tools/database.py migrate
```

成功响应应包含 `status=ready`、`schema_version=4`、`community_revision=20261020_02`。schema_version=4 是保留的业务基线，不能据此误认为未执行账户/通知迁移。

4. 启动单实例 API，再启动两个前端。用户重新注册；旧域名 Cookie 不会迁移，也不能凭相同昵称恢复权限。

### B. 已有需要保留的 PostgreSQL 数据

1. 只读核对数据库对象、现有源码/digest、Alembic revision、唯一写入口与备份恢复人。先取可恢复备份，并在**另一隔离 PostgreSQL 目标**演练恢复，记录稳定 ID、历史消息、贴纸/举报快照、账户权限、消息游标和幂等 ID。
2. 旧 PG schema 4、账户 revision `_01` 可以受控升级到 `_02`；未知 schema、部分损坏或不完整扩展应先停止，使用已有原生 PG 备份流程调查，不绕过就绪检查。
3. 演练命令如下。每组命令执行前单独核对 Secret 指向，**不能因为切换了命令名称就假设数据库已切换**：

```sh
# 备份：DATABASE_URL 指向现有 PG 源；目的目录在源码树外，文件不能已存在。
python tools/database.py backup /restricted/onstage-before-20261020.json

# 恢复演练：改用另一个隔离且空的 PG 目标的 Secret。
python tools/database.py migrate
python tools/database.py restore /restricted/onstage-before-20261020.json
python tools/database.py verify /restricted/onstage-before-20261020.json
```

4. 演练通过后进入维护窗口：暂停/冻结所有旧写入口，停止旧 API 并确认无其他写者，重新取最终一致性备份；不要让新旧两站同时接受写入。对**正式目标**执行一次 `python tools/database.py migrate`，确认输出 `_02` 后才启动新 API；启动不会自动改库。
5. 新私有快照为 `onstage-postgres-private-v3`，包括账户、评价防刷账本、通知 endpoint/keys、关注与待发队列。旧 PG v1/v2 恢复兼容；v2 恢复至新库时保留业务 ID 并补防刷账本，不把旧 Alembic revision 写回新库。私有快照含凭据，必须受限保存。工具有体积/行数限制，超出应使用维护者已有 `pg_dump/pg_restore` 流程，不删历史来满足工具限额。
6. `/readyz` 通过后，仍要实际验证抢角、文字/贴纸、后台/旁观权限、结束禁言、踢人、举报隔离、历史读取和应用替换后数据保留。

首次管理员先自行注册，再由有权限的维护者执行，准确用户名只放在受控运维命令中：

```sh
python tools/accounts.py --operator APPROVED_OPERATOR grant-admin EXACT_REGISTERED_USERNAME
```

这会留审计；不要用公开注册字段自行选择管理员。赞踩按“对局＋评价者＋被评价者”唯一；跨局双方累计限制由私有账本执行。改票不增条数，撤回保留占用，普通用户界面/API不展示阈值或剩余额度。

## 4. VAPID 密钥与 Secret

未配置 VAPID 时聊天照常运行，但通知接口报告不可用，前端禁用启用按钮。**当前交付未创建或配置生产密钥。**

1. 维护者准备源码树外的受限目录，在受控环境生成一次密钥：

```sh
python tools/push_keys.py /restricted/onstage-vapid-private.pem
```

工具不覆盖既有文件、不打印私钥；父目录权限由维护者保证，POSIX 模式不能代替 Windows ACL。妥善保存并备份该稳定密钥，不能随 Pod 重建生成。

2. 通过现有 Secret 管理器将 PEM 文件放入 Secret 的文件键，例如 `onstage-vapid/private.pem`。若使用 Kubernetes，API Pod 的合并片段可为：

```yaml
# 合并进真实 namespace 的 API Deployment，不能覆盖原有 env/volumes。
# 数据库仍从原 onstage-database / DATABASE_URL Secret 引用。
spec:
  template:
    spec:
      volumes:
        - name: vapid
          secret:
            secretName: onstage-vapid
            defaultMode: 0444
      containers:
        - name: onstage
          env:
            - name: ONSTAGE_VAPID_PRIVATE_KEY_FILE
              value: /run/secrets/onstage-vapid/private.pem
            - name: ONSTAGE_VAPID_SUBJECT
              value: mailto:REPLACE_WITH_MAINTAINER_CONTACT
          volumeMounts:
            - name: vapid
              mountPath: /run/secrets/onstage-vapid
              readOnly: true
```

`0444` 用于此非 root 容器能读取挂载 Secret；若集群使用 `0440` 等更严格模式，必须设置并验证相应组权限，不能直接导致 UID 10001 无法读取。Secret 对象和内容不进入 Git；这里只交付引用。迁移 Job 无须 VAPID，数据库 Secret 则必须与 API 指向同一正式目标。

3. `ONSTAGE_VAPID_SUBJECT` 使用实际维护者的 `mailto:` 联系方式或 HTTPS 联系地址。只设置其中一个变量、文件不可读或不是有效 P-256 PEM 会明确启动失败，不能当作健康成功。
4. 在可信用户前端登录后检查 `/api/notifications/config`：应为 `enabled:true` 且有 public_key；接口只给公钥，不能复制 Cookie、CSRF、endpoint/keys 到 issue。密钥轮换/丢失后应让浏览器关闭旧通知再重新开启；不要宣称旧订阅会无感继续。
5. API 到推送 provider 的 HTTPS 出站、手机到系统推送服务的网络均须可达。应用当前默认只接收明确的 FCM/Mozilla/Apple/Windows provider 主机；其他真实 provider 需核对后通过 `ONSTAGE_PUSH_PROVIDER_HOSTS` 添加**准确可信 host**，不能接受任意 URL、IP 或泛域。重定向被禁止。Apple 的网络说明见 [WebKit Web Push](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)。不要为测试改动全局代理或关闭证书验证。

## 5. 用户开启 PWA 通知

1. 从同一正式 HTTPS 用户端 URL 登录。iPhone/iPad 使用支持 Web Push 的系统，并先“添加到主屏幕”，再从主屏图标打开、登录并授权；不能仅用内嵌浏览器代替。该平台要求来自 [WebKit 官方说明](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)。Android 使用支持 Push API 的浏览器，记录型号/系统/浏览器及是否安装 PWA。
2. 打开“美术缓存设置”。若有“更新美术缓存服务（保留草稿）”，先手动更新。旧 active worker 未通过 `PUSH_PROTOCOL version=1` 握手时会阻止开启并提示更新，不能把等待中的新 worker 当作已接管。
3. 在“消息通知”中点“开启此浏览器消息通知”，由本人接受浏览器/系统权限；拒绝时明确保持关闭。应用不自动弹权限申请，也不因登录自动订阅。
4. 进入自己参与的房间，再在设置中开启“当前房间”通知。**浏览器订阅和房间关注是两层开关，只有一个开启还不够。** 活动成员方可开启，旁观者不会收到后台频道提醒。
5. 使用另一独立账户发送文字和贴纸，依次测试前台、切到其他应用、锁屏、关闭页面后的接收。通知只显示“你关注的房间有新消息”，点击返回同源房间，登录及权限仍由服务器检查。
6. 退出会撤销本登录设备的服务端订阅；换账号会清旧浏览器订阅。关闭浏览器总开关或当前房间开关后再发新消息，验证停止提醒。已经交给外部 provider 的提醒无法承诺瞬间撤回，但不包含正文；不要把一条在途提醒当作获得旧聊天读取权限。

Web Push 是尽力而为的异步提醒，可由系统、权限或网络抑制；不能证明跨端已读或保证即时到达，HTTP 游标同步才负责补齐记录。基本机制见 [MDN Push API](https://developer.mozilla.org/en-US/docs/Web/API/Push_API)。本实现会合并短时提醒，每个浏览器订阅最多保存一个待发提示，因此连续多条消息可能只显示一个通知，并跳转最后更新的房间；聊天记录仍应完整。

## 6. 真手机与多人验收表

至少三位独立参与者、至少一台真实手机，使用同一正式用户端 URL。各自注册/登录；普通标签页共享 Cookie 不能当成多身份。建议 A 房主桌面、B 手机演员、C 独立账户旁观/演员；开场需两名已选角演员。让发送端保持在线，避免把全员离线十分钟自动结束误当作推送失败。

| 检查 | 操作与通过证据 |
| --- | --- |
| 身份/权限 | A/B 各自选角，C 旁观；互相看不到登录用户名。并发抢同一角色只有一位成功；C 不能后台发言/收到后台提醒。 |
| 消息/幂等 | A/B 文字与贴纸、重复重试、断网后恢复；一次逻辑发送只一条，草稿保留，不虚构已读。 |
| 手机后台通知 | B 完成浏览器与房间双开关；A 发新消息。分别记录前台、切其他应用、锁屏、关闭页面后的显示与点击返回。需要等到实际新消息，不能用本地测试通知代替服务端推送。 |
| 关闭/换号 | B 关闭房间或浏览器通知、退出/换号后，A 再发新的测试消息；验证不继续收到旧账户新提醒。区分已在途通知。 |
| 权限撤销 | A 踢 B，或 B 退场/失去后台资格，再发对应频道消息；不得继续产生该权限下的新提醒或读取消息。结束后所有人不能继续发言。 |
| 数据/恢复 | 更新或替换 API 容器，账户/消息/贴纸/权限仍在，消息 ID/游标正确。在隔离 PG 完成备份恢复，不在正式库做破坏性演练。 |
| 互动隐私 | 同局改赞踩保持一条，撤回/跨局操作符合服务端限制；普通用户不见额度和其他人的评价/举报，管理员可查。 |
| 美术/PWA升级 | 首次与热缓存分别记录传输；旧 PWA 手动更新后草稿保留、通知握手可用；热缓存不反复全量下载185份高清资源。 |
| 网络与延迟 | 在大陆普通网络、演示现场分别测试；记录 Wi-Fi/蜂窝、是否代理、失败/未到达次数，以及下述分段时间。海外云端或 loopback 不代签。 |

延迟分开记录：点击→本地显示、请求→持久化 ACK、数据库耗时、服务端通知处理、接收端同步补齐/渲染、手机系统通知出现。用浏览器 Network/Server-Timing 和受控运维日志定位，通知提示没有已确认到达回执；不要把本地乐观显示当成手机到达。首次/热缓存流量也分开。与旧 Sites/现场网络对照时说明是否为同一网络和设备，不将差异直接归因于数据库。验收前约定实际延迟门槛，保留超时和未到达，不用平均值覆盖失败。

不要求强行停止系统应用后仍必达；系统强停、省电、Focus/勿扰、权限关闭等状态单列为平台限制与诊断，不冒充正常后台通过。公开回执不上传原始 HAR、Cookie、私有消息、endpoint、密钥或完整数据库。

## 7. 已验证、未验证与回退

本地工程证据见 [通知/PG-only 验收记录](../reports/notifications-20261020.json)、[账户社区记录](../reports/community-20261020.json) 和 [acceptance](acceptance.json)。本轮真实隔离 PostgreSQL 为17.11；CI目标为16，但本次 runner未启动，不能写成PG16运行验收。

- 最后全仓运行：260 passed、2 Windows 符号链接权限跳过、3次空日志的服务启动夹具超时；原样三项复验3 passed，不改代码/断言/超时，合计263项覆盖通过，**不是单次全绿运行**。初跑旧schema断言与数据库配置假设的失败也保留记录。
- 前端12项、通知专项15项、赞踩专项5项、类型检查与两前端构建通过；真实VAPID/AES加密解密测试的provider传输是模拟，不是手机到达。实际本机浏览器只验了设置及未配置禁用提示，没有接收外部Push。
- **未验证**：实际容器镜像、不可变digest、目标部署/正式迁移、VAPID生产Secret、外部provider投递、iOS/Android后台到达、三位真人、手机和大陆/现场访问延迟、Prometheus实际抓取/Grafana实际导入、合作方GitOps自动更新。

复用 Neboer 现有 GitOps/rollout 机制，由其选定三份已验证 digest、变更部署版本并触发更新；如果要自动跟踪，必须有具体镜像更新规则与触发路径。当前未配置自动 CD，装有 Argo CD/Flux 本身不表示镜像跟踪已完成。演示前冻结明确 release/digest，不跟随无关新提交切换。

回退先冻结写入并保存新数据，再由维护者选择已演练恢复点和对应旧镜像。新 `_02` 库不能只换成要求 `_01` 的旧应用，不能执行删表式 downgrade；代码回滚不等于数据库回滚。需要恢复旧库时使用另一受控目标恢复并核对数据丢失窗口与权限，再切唯一入口。VAPID稳定密钥、域名与Secret要一并保留；域名变化重新登录/订阅，不能按昵称转接身份。

请在 PR #1 回复以下脱敏回执，确认接手不等于已经验收：

```text
负责人 / 操作时间：
源码 SHA / API、web、admin 镜像与 digest / 架构：
目标 URL / namespace / 既有 rollout 或 GitOps 更新方式：
数据库 Secret 名称与键（不写值）/ 迁移前后 revision：
备份与隔离恢复结果（不附私有文件）：
VAPID Secret 引用 / 通知配置 enabled / 手机与网络：
三人、手机后台/锁屏、关闭/踢人、重启、延迟与带宽结果：
失败与未完成项 / 回退点 / 是否已冻结演示 digest：
```

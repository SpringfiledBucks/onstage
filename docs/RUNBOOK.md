## 2026-10-20 通知与数据入口更新

当前应用及运维CLI只使用PostgreSQL；旧SQLite文件不打开、不迁移、不删除。部署使用新库显式执行 `python tools/database.py migrate`，已有PostgreSQL数据先备份再升级到20261020_02；旧PG快照恢复仍受控支持。历史SQLite领域代码仅保留作回归参考，不是可部署运行模式。

PWA Web Push按浏览器主动授权、按房间关注，默认关闭；通知固定提示“你关注的房间有新消息”，不含正文、角色名、用户名或房间标题。登录设备退出/封禁撤销其订阅，踢人/退场/后台角色变化在发送前复核。推送第三方仅接收加密标识；浏览器/系统/网络仍可阻止或延迟送达，Web Push不能代替HTTP游标补齐或证明对方已读。旧已安装PWA须先手动更新缓存服务，协议握手通过后才允许开启通知。

由维护者在受限目录执行 `python tools/push_keys.py /restricted/onstage-vapid-private.pem` 生成并妥善备份稳定密钥；只读挂载为Secret，配置ONSTAGE_VAPID_PRIVATE_KEY_FILE和ONSTAGE_VAPID_SUBJECT。不将PEM、订阅endpoint/keys、私有快照上传仓库/日志/指标；密钥丢失或更换需要浏览器重新订阅。未配置时UI明确不可用，不影响聊天。API单副本有一个受控推送worker，队列与消息同事务提交，每个订阅仅保留最新提醒；总订阅与每账户/关注房间均有界，网络超时与重试有界，过期/失效订阅清除，异常重启通过租约恢复。短时通知合并可能只跳到最后更新房间；其他房间消息仍可同步完整读取。

公网使用HTTPS和准确可信前端Origin；服务端需能访问浏览器推送服务HTTPS，默认只允许FCM/Mozilla/Apple等明确主机且禁止重定向。扩展provider须维护者单独配置准确可信host，不开放任意用户URL。手机后台能力还受浏览器、系统权限、主屏安装和推送服务网络可达性影响；大陆及现场网络必须实机验证，不保证绕过系统限制。当前不宣称外部推送通道已验收。

赞踩有效记录保持“对局＋评价者＋被评价者”唯一；服务端对同一双方跨局累计设置防刷上限，改票不新增记录、撤回保留占用，相同请求重试不消耗额度。普通用户看不到额度、阈值或他人评价；管理员仍能查看评价记录。

---

# 0.9 运维优先说明

本版本新增账户、私密房、作者授权分享与管理权限，配置和迁移按SELF_HOSTED的0.9部分执行。用户与管理前端独立部署，8765为API；ONSTAGE_FRONTEND_ORIGINS/ONSTAGE_ADMIN_ORIGINS只填准确可信Origin。*.neboer.site仅公开读，不是登录白名单。

新版本账号与登录会话分离；首次管理员由维护命令授予。ban撤销所有登录，解封不恢复旧token。不要在聊天/工单/日志输出密码、Cookie、快照或真实数据库连接串。ONSTAGE_METRICS_TOKEN单独注入Prometheus Secret。

工具database.py使用同一DATABASE_URL，backup兼容完整旧schema4与新v3，restore仅空目标并验证所有新表；生产PG独立执行Alembic迁移，启动只检查。当前服务/运维入口只接受PostgreSQL，缺DATABASE_URL立即失败；旧SQLite原样保留、不再启动写入或导入历史。首次新站迁移空PG，从新账户和新房间开始。旧包/原图不删除。正式切换和回滚需完整库恢复计划，不能仅替换代码。

在线是90秒租约与有效账户会话，不是连接数。服务重启重置全离线观察期；单副本更新须Recreate。自动结束事件、场景切换、权限与消息都必须实际验证，不能用health代签。

---

# Onstage 0.8 运行与恢复

## 美术、PWA与分页更新

直接部署Git中的原图、哈希派生和资源清单，不需线上安装图像处理工具。原`docs/ASSET_MANIFEST.json`标识原始185文件，`static/assets-manifest.json`标识传输派生；目录和历史快照继续保存原路径。验证命令：

```sh
python tools/optimize_assets.py verify
# 作者/CI需要逐像素验证时，使用独立依赖，不进入运行镜像
python -m pip install -r requirements-assets.txt
python tools/optimize_assets.py verify --pixels
# 更新素材时：使用官方libwebp 1.6.0 cwebp，受控重新生成后审查差异
python tools/optimize_assets.py build --cwebp /path/to/cwebp
```

禁止覆盖已发布内容hash文件，禁止清掉仍可能被旧页面引用的原图/旧hash文件。素材或颜色配置不满足严格比较时构建报错；未变资源复用URL。清单/HTML/脚本重新校验，不可被代理改成长期immutable。不要对`/api/`启用共享缓存。

PWA需公共HTTPS；localhost仅作隔离验证。根`/sw.js`必须到达应用并返回JS和`Service-Worker-Allowed: /`，不得被代理改为HTML或要求登录。使用设置可主动保存约16.31 MB完整美术、取消或清除本应用缓存；存储不足/被清理会回源，身份与草稿不随清除操作删除。当前未提供离线聊天/历史读取、跨域身份迁移或强制更新。旧页面会在关闭后按浏览器生命周期启用新SW；发布不应强迫所有页面刷新。

PG升级前仍按本文备份、隔离恢复并显式执行`python tools/database.py migrate`；本轮仅追加三个分页索引，schema 4与旧应用兼容，不执行删列或删数据。D1维护者执行受控`0001_room_pagination`迁移；SQLite沿用既有初始化/旧库迁移路径。应用Pod不并发执行迁移。大量房间的索引建立仍需维护窗口监测，千房测试不是百万级建索引时长承诺。

前端每页24间，刷新/筛选重回第一页。游标绑定身份与筛选，过期身份应重新入场，不尝试改游标恢复权限。总房间数可超过1000，不提高默认10间活跃房准入。回滚0.7代码可保留新增索引，但需连同静态资源、清单和SW一起回退；浏览器内原美术缓存不包含业务数据，也不代替数据库备份。

本轮交付开发与公开 GitHub。目标部署由合作方负责；没有执行旧站冻结、生产导出或目标集群切换。镜像/端口/探针/Secret/单副本和 CI 契约见 [SELF_HOSTED](SELF_HOSTED.md)。所有动作只针对已核对的本项目资源，不修改全局代理、路由或其他采集器。

## 配置与启动

生产 DATABASE_URL 由 Secret 注入，公共 ONSTAGE_PUBLIC_ORIGIN 必须精确，公网 Secure Cookie=1。端口8765、一个 Uvicorn worker、一个应用副本、Recreate 更新。镜像 UID/GID10001，生产 PG 独立持久化；不要将生产连接串作为CLI参数。

```sh
python tools/database.py migrate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8765 --workers 1 --no-access-log --no-proxy-headers --ws-per-message-deflate false
```

迁移是独立受控步骤，失败不启动新应用；不能每 Pod 自动迁库。healthz 仅进程存活，readyz 实际检查DB/schema；还须使用独立会话验证消息和权限。反向代理保留精确Origin与WebSocket Upgrade，仅信任核对过的代理；当前默认不接受代理头，不能据此声称获得真实来源IP。

## 备份和隔离恢复

使用与应用相同的 DATABASE_URL。备份含会话、后台和举报，保存在源码之外受限目录，不上传Git、镜像、Actions artifact。POSIX使用0600；Windows mode位不代表NTFS ACL，维护者须实际限制目录访问。大库使用已验证的原生PG备份，业务JSON有明确体积/行数限制。

```sh
python tools/database.py backup /restricted/pre-release.json
# 在独立空PG库环境中
python tools/database.py migrate
python tools/database.py restore /restricted/pre-release.json
python tools/database.py verify /restricted/pre-release.json
```

restore在事务内拒绝覆盖已有业务数据，逐字段检查ID、外键、游标与快照，校正实际identity序列。当前不导入SQLite历史，import-sqlite已移除。历史D1快照解析保留回归用途；本轮新站从空PostgreSQL开始，不执行历史身份转接。

旧SQLite文件原样保留，tools/backup.py CLI已停用并明确报错；当前运维统一使用PostgreSQL DATABASE_URL，不再写另一份SQLite。举报运维 tools/moderate.py 读取同一连接配置；处理输出为私密材料，不能投屏或发布。

## 唯一主站切换

1. 明确旧业务主库、备份位置、最终恢复点和负责人；不合并不明来源数据。
2. 在隔离PG导入并校验历史/权限/贴纸/举报，验证并发与应用重启留存。
3. 在旧站部署 ONSTAGE_WRITE_FROZEN=1，探测真实POST确实503，维护者GET导出仍可用；仅写文档不算冻结。保留恢复路径。
4. 最终受控导出→目标恢复→逐字段核验→唯一入口切换。新站写入后旧站保持只读。
5. 新域名默认新访客进入新房；保留历史不等于浏览器身份无感迁移，不能按同名恢复房主。

回退先冻结新写入、保存新数据和故障现场。schema兼容可切旧digest；不兼容须另行恢复已核验的数据库备份，说明恢复点之后数据处理，不以回滚镜像冒充回滚数据库。不能新旧两站各自持续写入。

## CI与协作

PR/main执行回归、隔离PG与容器检查。main手动发布或版本发布验证通过后推GHCR，记录commit/tag/digest/run。GitOps由合作方现有机制接入，Deployment或imagePullPolicy不会监视更新；现场演示固定release/digest。没有集群授权时不宣称CD上线。

日程为北京时间2026-10-19 12:00冻结、18:00演示。实际手机/三人/大陆和现场检查见DEMO，状态以acceptance为准。

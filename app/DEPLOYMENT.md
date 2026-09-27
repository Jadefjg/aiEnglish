# 领智云英语（aiEnglish）技术与业务说明 · 部署手册

本文档覆盖：系统定位、技术架构、业务能力、环境准备、本地/Docker/HTTPS 部署、数据库迁移、纠音配置、验收清单与运维排障。

适用版本：应用 `2.4.x`（`app/backend/main.py`）

---

## 1. 项目定位

本项目包含两层能力：

| 层级 | 说明 | 入口 |
|------|------|------|
| 教程资源中心 | 整理学生/教师手册功能目录、演示视频与 PDF，支持检索与播放 | `/`（`index.html`） |
| 学习工作台 | 可运营的教培业务：班级、作业、纠音、家长学情 | `/learn.html` |

素材来源：`asset/` 目录中的学生/教师 PDF 手册与录屏视频；功能与媒体元数据维护在 `backend/catalog.json`。

> 说明：未配置 MySQL 时，系统仍可只读浏览教程资源；**班级 / 作业 / 登录 / 纠音历史等业务必须 MySQL**。

---

## 2. 技术架构

### 2.1 技术栈

| 组件 | 技术 | 职责 |
|------|------|------|
| 前端 | Vue 3（`vue.global.prod.js`，无构建器） | 教程中心、学习工作台 |
| 网关 | Nginx 1.27 | 静态页、反代 `/api` `/assets` `/uploads` |
| API | FastAPI + Uvicorn（Python 3.12） | 认证、业务接口、静态素材托管 |
| 数据库 | MySQL 8.4 | 用户、班级、作业、词库、题库、纠音记录 |
| 媒体处理 | ffmpeg / ffprobe（API 镜像内） | 语音转码、时长探测、Azure 评测前转 WAV |
| 编排 | Docker Compose | `mysql` → `migrate` → `api` → `web` |

### 2.2 运行时拓扑（生产 Compose）

```text
Browser
  │
  ├─ http://host:8080/          → web(Nginx) → 静态页
  ├─ /api/*                     → web → api:8000
  ├─ /assets/*                  → web → api（Range 支持视频）
  └─ /uploads/*                 → web → api（学生语音等）

api
  ├─ MySQL(aienglish)
  ├─ ASSET_DIR  → ../asset（教材视频/PDF）
  └─ UPLOAD_DIR → ./uploads（运行时上传）
```

### 2.3 主要代码结构

```text
aiEnglish/
├── asset/                      # 教程素材（mp4/pdf），需自行放置
└── app/
    ├── DEPLOYMENT.md           # 本文档
    ├── README.md
    ├── .env.example
    ├── docker-compose.yml
    ├── docker-compose.https.yml
    ├── nginx.conf / nginx.https.conf
    ├── requirements.txt
    ├── backend/
    │   ├── main.py             # 入口：认证、资源目录、挂载
    │   ├── homework.py         # 作业/词库/题库/纠音练习 API
    │   ├── classes.py          # 班级管理
    │   ├── parents.py          # 家长绑定与学情
    │   ├── pronunciation.py    # Azure / Whisper / 浏览器 ASR / 本地评分
    │   ├── storage.py          # local / S3 / MinIO 上传存储
    │   ├── notify.py           # 站内信 + Webhook + 微信模板
    │   ├── catalog.json        # 功能与媒体元数据
    │   └── Dockerfile
    ├── frontend/
    │   ├── index.html          # 教程中心
    │   ├── learn.html/js       # 学习工作台
    │   ├── auth.js             # 登录浮层
    │   └── Dockerfile
    ├── db/
    │   ├── schema.sql          # 首次初始化
    │   ├── migrate.sh
    │   └── migrations/         # 000~005 增量迁移
    ├── uploads/                # 语音上传（运行时生成）
    └── scripts/
        ├── generate-dev-cert.sh
        └── acceptance_smoke.py
```

### 2.4 数据迁移一览

| 版本 | 文件 | 内容 |
|------|------|------|
| 000 | `000_schema_migrations.sql` | 迁移版本表 |
| 001 | `001_auth_and_progress.sql` | 用户、学习进度 |
| 002 | `002_homework.sql` | 词库、题库、作业、提交；种子数据 |
| 003 | `003_classes_and_pronunciation.sql` | 班级、纠音历史 |
| 004 | `004_parent_urge_close.sql` | 家长绑定、催交字段 |
| 005 | `005_notify_storage.sql` | 站内通知、微信 openid / webhook |

Compose 每次启动会对 `migrations/*.sql` 全量重放（脚本需幂等）。

---

## 3. 业务能力说明

### 3.1 角色

| 角色 | 能力 |
|------|------|
| student | 加入班级、做作业、背单词、AI 纠音、生成家长邀请码 |
| teacher | 建班、布置作业、催交、批改、维护词库/题库 |
| parent | 用子女邀请码绑定，查看作业/纠音学情，接收推送消息 |
| admin | 可创建任意角色账号；可查看全局班级/作业 |

公开注册仅允许：`student` / `teacher` / `parent`。管理员建议通过环境变量引导创建，或由已有 admin 调用 `POST /api/admin/users`。

### 3.2 核心业务闭环

```text
教师创建班级 → 分享邀请码/添加学员
    → 布置作业（选择题 / 看视频 / 背单词 / AI纠音朗读）
    → 学生完成并交卷
    → 教师催交 / 批改
    → 教师可手动关闭；截止后自动关闭（`due_at` 按东八区与 `NOW()` 比较）
    → 发布草稿时补同步班级新成员（避免漏发）

学生生成家长邀请码 → 家长绑定 → 查看子女学情
```

### 3.3 作业题型

| 题型 | `task_type` | 学生操作 | 评分 |
|------|-------------|----------|------|
| 选择题 | `choice` | 选择选项提交 | 自动判分 |
| 看视频 | `video` | 观看并上报进度 | 达到最少秒数即完成 |
| 背单词 | `vocab` | 标记已掌握 | 按掌握比例计分 |
| AI 纠音 | `voice` | 录音上传 | 总分/准确度/流利度/完整度/逐词反馈 |

### 3.4 纠音引擎优先级（可配置，不再唯一依赖 Azure）

由 `PRONUNCIATION_PROVIDER`（默认 `auto`）控制，阶梯为：

1. **Azure Pronunciation Assessment**（`AZURE_SPEECH_KEY`，音素级）
2. **自建 Whisper ASR + 词级对齐**（`WHISPER_ASR_URL`，无需 Azure 密钥）
3. **浏览器英文语音识别 + 后端词级比对**（`browser-asr`）
4. **本地启发式**（无识别文本时回退）

页面顶部会显示当前引擎说明（`GET /api/pronunciation/status`）。`/api/health` 亦返回 `pronunciation` 字段。

### 3.5 家长消息推送

- 站内信：作业发布 / 催交 / 交卷 / 批改自动写入 `notifications`
- 可选外发：全局 `NOTIFY_WEBHOOK_URL`、用户级 webhook、微信公众号模板（`WECHAT_*` + 家长绑定 openid）
- 学习工作台「消息中心 / 推送渠道」可查看与配置

### 3.6 多副本 uploads 共享存储

- 默认 `STORAGE_BACKEND=local`（单副本或共享卷即可）
- 多副本 API：**必须**使用 `s3` / `minio` / `oss`，或把 `UPLOAD_DIR` 挂到共享文件系统
- 可选一键 MinIO：`docker compose -f docker-compose.yml -f docker-compose.minio.yml up -d`

---

## 4. 部署前准备

### 4.1 硬件与系统建议

- CPU：2 核+
- 内存：4 GB+（含 MySQL 与 ffmpeg）
- 磁盘：视 `asset/` 视频体积而定（建议预留 20 GB+）
- OS：Linux 服务器优先；Windows 需安装并启动 **Docker Desktop**
- 浏览器：Chrome / Edge（纠音依赖麦克风与 Web Speech API）

### 4.2 软件依赖

**生产（Docker）**

- Docker 24+
- Docker Compose v2+

**本地开发（非 Docker）**

- Python 3.12+
- Node.js 18+（仅用于安装 Vue 静态文件到 `frontend/node_modules`）
- MySQL 8.0+（业务必需）
- ffmpeg（纠音转码/时长探测）

### 4.3 素材目录

将 PDF / MP4 放到仓库根目录的 `asset/`（与 `app/` 同级）：

```text
aiEnglish/asset/*.mp4
aiEnglish/asset/*.pdf
```

Compose 中 API 通过 `../asset:/srv/asset` 挂载。目录不存在时 API 会自动创建空目录，但教程视频将不可用。

### 4.4 网络安全

- 开放端口：`8080`（HTTP）或 `443`（HTTPS）
- 生产环境务必启用 HTTPS
- 语音功能需要浏览器麦克风权限；Nginx 已配置 `Permissions-Policy: microphone=(self)`

---

## 5. 环境变量

复制模板并修改：

```bash
cd app
cp .env.example .env
```

| 变量 | 必填 | 说明 |
|------|------|------|
| `MYSQL_ROOT_PASSWORD` | 是（Compose） | MySQL root 密码，需足够强 |
| `JWT_SECRET` | 是（Compose） | JWT 签名密钥，**≥ 32 字符**，生产不可用默认值 |
| `ENVIRONMENT` | 建议 | `production` / `development`；生产强制校验 JWT |
| `BOOTSTRAP_ADMIN_USERNAME` | 否 | 首次启动若不存在则创建管理员 |
| `BOOTSTRAP_ADMIN_PASSWORD` | 否 | 管理员密码，**≥ 12 字符** |
| `AZURE_SPEECH_KEY` | 否 | Azure 语音密钥；空则走 Whisper / 浏览器 ASR / 本地 |
| `AZURE_SPEECH_REGION` | 否 | 默认 `eastasia` |
| `AZURE_SPEECH_LANGUAGE` | 否 | 默认 `en-US` |
| `PRONUNCIATION_PROVIDER` | 否 | `auto` / `azure` / `whisper` / `browser` / `local` |
| `WHISPER_ASR_URL` | 否 | 自建 Whisper HTTP 接口（multipart `file` → `{text}`） |
| `WHISPER_ASR_TOKEN` | 否 | Whisper 接口 Bearer Token |
| `STORAGE_BACKEND` | 否 | `local`（默认）/ `s3` / `minio` / `oss` |
| `S3_ENDPOINT` / `S3_BUCKET` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` | S3 时 | 对象存储连接 |
| `S3_PUBLIC_BASE_URL` | 否 | 公网/CDN 前缀；空则用预签名跳转 |
| `S3_PRESIGN` | 否 | 默认 `1`，无公网前缀时生成预签名 URL |
| `NOTIFY_WEBHOOK_URL` | 否 | 全局通知 Webhook（JSON POST） |
| `WECHAT_APP_ID` / `WECHAT_APP_SECRET` / `WECHAT_TEMPLATE_ID` | 否 | 微信公众号模板消息 |
| `WECHAT_DEFAULT_URL` / `WECHAT_TEMPLATE_DATA_JSON` | 否 | 模板跳转与字段映射 |
| `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` | 本地开发 | 直连外部 MySQL 时使用 |
| `ASSET_DIR` | 否 | 素材目录，默认仓库 `asset/` |
| `UPLOAD_DIR` | 否 | 本地上传目录，Compose 内为 `/srv/app/uploads` |

示例（请替换为真实密钥）：

```bash
MYSQL_ROOT_PASSWORD=ChangeMe_Root_#2026
JWT_SECRET=please-replace-with-a-long-random-secret-32+
ENVIRONMENT=production
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=ChangeMe_Admin_#2026
AZURE_SPEECH_KEY=
AZURE_SPEECH_REGION=eastasia
AZURE_SPEECH_LANGUAGE=en-US
```

---

## 6. 部署方式 A：Docker Compose（推荐生产）

### 6.1 一键启动（HTTP）

```bash
cd app
cp .env.example .env
# 编辑 .env 填入强密码与 JWT_SECRET

# 确保素材目录存在
mkdir -p ../asset uploads

docker compose up --build -d
```

访问：

- 教程中心：`http://localhost:8080/`
- 学习工作台：`http://localhost:8080/learn.html`
- 健康检查：`http://localhost:8080/api/health`

### 6.2 常用运维命令

```bash
# 查看状态
docker compose ps

# 查看日志
docker compose logs -f api
docker compose logs -f migrate
docker compose logs -f web

# 重启
docker compose restart api web

# 停止
docker compose down

# 停止并删除数据卷（危险：清空 MySQL）
docker compose down -v
```

### 6.3 服务说明

| 服务 | 作用 |
|------|------|
| `mysql` | 数据库，首次用 `db/schema.sql` 初始化 |
| `migrate` | 执行 `db/migrations/*.sql` 后退出 |
| `api` | FastAPI，内含 ffmpeg |
| `web` | Nginx 前端 + 反代 |

### 6.4 持久化

- MySQL 数据：`mysql_data` Docker volume
- 语音上传：宿主机 `app/uploads`（已挂载）
- 教程素材：宿主机 `asset/`（只读业务数据，需自行备份）

---

## 7. 部署方式 B：HTTPS（生产强烈建议）

### 7.1 开发自签名证书（仅本机调试）

```bash
cd app
chmod +x scripts/generate-dev-cert.sh
./scripts/generate-dev-cert.sh

docker compose -f docker-compose.yml -f docker-compose.https.yml up --build -d
```

访问：`https://localhost`（浏览器会提示自签名风险，可继续访问）。

> Windows 若无 `sh`，可在 Git Bash / WSL 中执行证书脚本，或自行用 OpenSSL 生成 `app/certs/fullchain.pem` 与 `app/certs/privkey.pem`。

### 7.2 公网正式证书

1. 申请可信 CA 证书（Let's Encrypt / 云厂商等）
2. 将证书放到 `app/certs/`：
   - `fullchain.pem`
   - `privkey.pem`
3. 启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.https.yml up --build -d
```

`nginx.https.conf` 已包含：

- HTTP → HTTPS 跳转
- TLS 1.2 / 1.3
- HSTS、基础安全头
- API 限流
- 麦克风权限放行（`microphone=(self)`）

---

## 8. 部署方式 C：本地开发（Uvicorn）

适合改代码热重载；**业务功能仍需本机 MySQL**。

```bash
cd app

# 1) Python 环境
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt

# 2) 前端 Vue 静态依赖
cd frontend && npm install && cd ..

# 3) 准备 MySQL 库并迁移
# 先创建库/账号，再执行：
# Linux/macOS:
MYSQL_HOST=127.0.0.1 MYSQL_USER=root MYSQL_PASSWORD=xxx MYSQL_DATABASE=aienglish ./db/migrate.sh
# 也可手动：mysql -h127.0.0.1 -uroot -p aienglish < db/schema.sql
# 然后依次执行 db/migrations/*.sql

# 4) 启动 API
set MYSQL_HOST=127.0.0.1
set MYSQL_USER=root
set MYSQL_PASSWORD=xxx
set MYSQL_DATABASE=aienglish
set JWT_SECRET=dev-only-change-this-secret-for-local-tests-32
set ASSET_DIR=..\asset
uvicorn backend.main:app --reload --port 8000
```

访问：`http://localhost:8000/` 与 `http://localhost:8000/learn.html`

离线逻辑冒烟（不依赖 MySQL）：

```bash
python scripts/acceptance_smoke.py
```

---

## 9. 数据库与迁移细则

### 9.1 首次初始化

- Compose：`mysql` 容器首次启动执行 `db/schema.sql`
- 随后 `migrate` 执行 `000`~`004`

### 9.2 已有库升级

只需保证 `migrations` 被执行（Compose 每次都会跑；本地用 `migrate.sh`）。

```bash
cd app
MYSQL_HOST=127.0.0.1 \
MYSQL_USER=root \
MYSQL_PASSWORD='你的密码' \
MYSQL_DATABASE=aienglish \
./db/migrate.sh
```

### 9.3 种子数据

`002_homework.sql` 内置示例：

- 8 个单词
- 5 道选择题

便于部署后立刻布置作业验收。

---

## 10. 纠音引擎配置（可选）

### 10.1 Azure（音素级）

```bash
PRONUNCIATION_PROVIDER=auto
AZURE_SPEECH_KEY=xxxxxxxx
AZURE_SPEECH_REGION=eastasia
AZURE_SPEECH_LANGUAGE=en-US
```

### 10.2 自建 Whisper（无需 Azure 密钥）

接口约定：`POST multipart/form-data` 字段 `file`（wav/webm），返回 JSON：`{"text":"..."}` 或 `{"transcript":"..."}`。

```bash
PRONUNCIATION_PROVIDER=auto
WHISPER_ASR_URL=http://whisper:9000/v1/audio/transcriptions
WHISPER_ASR_TOKEN=
```

### 10.3 仅浏览器 / 本地

```bash
PRONUNCIATION_PROVIDER=browser   # 或 local
```

重启 API：`docker compose up -d --force-recreate api`。学习工作台顶部与 `/api/health` 可核对当前引擎。

---

## 10A. 共享存储与家长推送

### 多副本 uploads

单机默认本地卷即可。水平扩展 API 时任选其一：

1. **对象存储（推荐）**

```bash
STORAGE_BACKEND=minio
S3_ENDPOINT=http://minio:9000
S3_BUCKET=aienglish-uploads
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_PRESIGN=1
```

```bash
docker compose -f docker-compose.yml -f docker-compose.minio.yml up -d
```

2. **共享文件系统**：多副本共用同一 `UPLOAD_DIR` 挂载（NFS/云盘），`STORAGE_BACKEND=local`。

### 家长微信 / Webhook

1. 配置 `.env` 中 `NOTIFY_WEBHOOK_URL` 和/或 `WECHAT_APP_ID` + `WECHAT_APP_SECRET` + `WECHAT_TEMPLATE_ID`
2. 家长登录学习工作台 →「推送渠道」填写 openid / 个人 webhook
3. 教师发布作业、催交、批改后，家长站内信与外发渠道会收到通知

迁移：`005_notify_storage.sql`（`notifications` 表 + `users.wechat_openid` / `notify_webhook`）。

---

## 11. 上线验收清单

### 11.1 基础健康

- [ ] `GET /api/health` 返回 `status=ok`，`database=connected`
- [ ] `/` 教程中心可打开，功能卡片与视频可访问
- [ ] `/learn.html` 可打开，右上角可注册/登录

### 11.2 教师 + 学生主流程

1. 注册教师账号、学生账号并登录  
2. 教师：班级管理 → 创建班级 → 复制邀请码  
3. 学生：加入班级  
4. 教师：布置作业（建议勾选选择题 + 背单词 + AI 纠音，设置截止时间）  
5. 学生：完成各题型 → 录音纠音 → 整份交卷  
6. 教师：查看提交 → 批改；对未交学生点「催交」  
7. 学生侧可见催交标记；截止后作业应变为「已关闭」

### 11.3 通知 / 存储 / 多引擎纠音

- [ ] `GET /api/health` 含 `storage` / `notifications` / `pronunciation`
- [ ] 发布/催交/交卷/批改后，相关角色「消息中心」有站内信
- [ ] 配置 `STORAGE_BACKEND=minio`（或 S3）后，语音上传仍可通过 `/uploads/...` 访问
- [ ] 未配 Azure、配置 `WHISPER_ASR_URL` 时，纠音 `provider` 为 `whisper+align`

### 11.4 家长学情

1. 学生打开「家长绑定」复制邀请码  
2. 注册/登录家长账号 → 绑定邀请码  
3. 查看子女作业进度、分数、纠音记录  

### 11.5 安全抽检

- [ ] 生产 `.env` 无弱口令、无默认 `JWT_SECRET`
- [ ] 公网已启用 HTTPS（非自签名）
- [ ] `uploads/` 与 MySQL 有备份策略

---

## 12. 主要 API 速查

### 认证与资源

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| POST | `/api/auth/register` | 注册 |
| POST | `/api/auth/login` | 登录拿 JWT |
| GET | `/api/features` | 功能目录 |
| GET | `/api/resources` | 媒体目录 |
| GET | `/api/audit` | 素材审计 |

### 班级 / 作业

| 方法 | 路径 | 说明 |
|------|------|------|
| POST/GET | `/api/classes` | 创建/列表 |
| POST | `/api/classes/join` | 学生加入 |
| POST | `/api/assignments` | 布置作业 |
| POST | `/api/assignments/{id}/urge` | 催交 |
| POST | `/api/assignments/{id}/close` | 手动关闭 |
| POST | `/api/assignments/{id}/publish` | 发布草稿并同步班级学员 |
| POST | `/api/assignments/{id}/submit` | 交卷 |
| POST | `/api/assignments/{id}/tasks/{task_id}/voice` | 语音纠音提交 |

### 家长 / 纠音

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/students/me/parent-invite` | 学生邀请码 |
| POST | `/api/parents/bind` | 家长绑定 |
| GET | `/api/parents/children/{id}/overview` | 学情总览 |
| GET | `/api/pronunciation/status` | 纠音引擎状态 |
| POST | `/api/pronunciation/assess` | 自由纠音练习 |
| GET | `/api/notifications` | 站内信列表 |
| POST | `/api/notifications/{id}/read` | 标记已读 |
| POST | `/api/notifications/read-all` | 全部已读 |
| PUT | `/api/notifications/channels` | 绑定微信 openid / 个人 webhook |

除公开资源接口外，业务接口请求头需：

```http
Authorization: Bearer <access_token>
```

---

## 13. 安全与容量建议

1. **密钥管理**：`.env` 勿提交仓库（已在 `.gitignore`）；生产用密钥托管/CI Secret。  
2. **登录防护**：登录每分钟限流；连续失败锁定 15 分钟。  
3. **上传限制**：语音建议 < 8MB；Nginx API 上传上限 20MB。  
4. **备份**：
   - MySQL：定期 `mysqldump aienglish`
   - `uploads/`：语音文件
   - `asset/`：原始教材媒体  
5. **水平扩展**：设置 `STORAGE_BACKEND=minio|s3|oss` 或共享 `UPLOAD_DIR`；参见 §10A。  
6. **管理员**：首次用 `BOOTSTRAP_*` 创建后，建议立刻修改密码并限制注册来源（可前置网关鉴权）。

---

## 14. 常见问题排障

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| `/api/health` 显示 `file-fallback` | 未配置/连不上 MySQL | 检查 `MYSQL_*` 与 compose `mysql` 健康状态 |
| 登录 503 | MySQL 未就绪 | `docker compose logs mysql migrate` |
| 学习工作台空白/无 Vue | 前端镜像未带 vendor | 重新 `docker compose build web --no-cache` |
| 视频无法播放 | `asset/` 为空或文件名不匹配 | 检查挂载与 `catalog.json` |
| 录音失败 | 浏览器未授权麦克风 / 非 HTTPS 限制 | 使用本机 localhost 或正式 HTTPS |
| 纠音一直 local | 未开浏览器识别或 Azure 未配 | Chrome 允许麦克风；或配置 Azure |
| 迁移报错 | 旧库缺列/权限不足 | 查看 `migrate` 日志，确认 root 密码正确 |
| Docker 连接 pipe 失败（Windows） | Docker Desktop 未启动 | 启动 Docker Desktop 后再 compose |
| 生产启动直接退出 | `JWT_SECRET` 过短 | 使用 ≥32 位随机串 |

健康检查示例：

```bash
curl -s http://localhost:8080/api/health
# 期望：{"status":"ok","database":"connected",...}
```

---

## 15. 回滚建议

1. **应用回滚**：检出上一版本代码，`docker compose up --build -d`  
2. **数据回滚**：使用部署前 `mysqldump` 备份恢复  
3. **勿随意 `down -v`**：会删除 MySQL volume  

---

## 16. 快速命令汇总

```bash
# 生产 HTTP
cd app && cp .env.example .env   # 先编辑
mkdir -p ../asset uploads
docker compose up --build -d

# 生产/联调 HTTPS
./scripts/generate-dev-cert.sh   # 或放置正式证书到 certs/
docker compose -f docker-compose.yml -f docker-compose.https.yml up --build -d

# 日志与健康
docker compose logs -f api
curl -s http://localhost:8080/api/health

# 离线冒烟
python scripts/acceptance_smoke.py
```

---

## 17. 附录：业务对象与表（简表）

| 表 | 用途 |
|----|------|
| `users` | 账号与角色；含 `parent_invite_code` |
| `classes` / `class_members` | 班级与成员 |
| `words` / `questions` | 词库 / 选择题库 |
| `assignments` / `assignment_tasks` | 作业与任务 |
| `assignment_students` | 作业指派、催交计数 |
| `assignment_submissions` / `task_answers` | 提交与作答 |
| `pronunciation_attempts` | 纠音历史 |
| `parent_student_links` | 家长-子女绑定 |
| `features` / `resources` | 教程功能与媒体元数据 |
| `learning_progress` | 手册功能学习进度 |

---

文档维护：随版本迭代请同步更新「迁移一览」「环境变量」「验收清单」三节。

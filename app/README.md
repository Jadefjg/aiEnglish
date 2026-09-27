# 领智云英语教学资源中心

这是根据 `asset/` 中两份学生/教师 PDF 手册及 17 段录屏搭建的资源管理系统，并已扩展为可实际布置/完成作业的学习工作台。

> **完整技术说明与部署手册**：见 [DEPLOYMENT.md](./DEPLOYMENT.md)（架构、环境变量、Docker/HTTPS、迁移、验收与排障）。

素材分析结果：学生手册 48 页，覆盖 11 个功能；教师手册 45 页，覆盖 8 个功能。系统将 19 个功能整理为结构化目录，并独立管理 17 个视频与 2 份 PDF，避免在没有依据时把视频错误归类。

- Python FastAPI：资源查询、账户认证、班级管理、作业布置与提交、AI 纠音评分
- Vue 3：教程中心 + `/learn.html` 学习工作台；固定版本随 Nginx 镜像打包
- MySQL：功能目录、班级、词库、题库、作业、提交与纠音历史
- AI 纠音：配置 `AZURE_SPEECH_KEY` 后走 Azure Pronunciation Assessment；未配置时使用本地启发式评分（可离线演示）
- Nginx：反向代理 `/api`、`/assets`、`/uploads`；语音题需允许麦克风（`microphone=(self)`）

## 启动

```bash
cd app
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
```

开发模式直接访问 `http://localhost:8000`。未配置 MySQL 时自动使用文件目录作为只读数据源。

生产环境使用：`docker compose up --build`，访问 `http://localhost:8080`。MySQL 初始化密码仅用于开发，请在部署前改为环境变量或 Secret。

已有 MySQL 数据库可单独执行迁移：

```bash
MYSQL_HOST=127.0.0.1 MYSQL_USER=root MYSQL_PASSWORD=... MYSQL_DATABASE=aienglish ./db/migrate.sh
```

Docker Compose 会先运行 `migrate` 服务，再启动 API，保证用户认证和学习进度表已存在。

生产安全配置：必须在 `.env` 中设置不少于 32 字符的 `JWT_SECRET` 和强 MySQL 密码；Compose 不再提供默认密码。登录接口采用 IP 级请求限制（每分钟 10 次），连续 5 次失败后锁定 15 分钟。需要 HTTPS 时，将 `nginx.https.conf` 作为站点配置挂载，并把证书挂载到 `/etc/nginx/certs/fullchain.pem` 与 `/etc/nginx/certs/privkey.pem`；该模板同时包含 HTTP→HTTPS 跳转、TLS 1.2/1.3、安全响应头和 API 限流。

本地可以生成自签名证书并启动 HTTPS：

```bash
./scripts/generate-dev-cert.sh
docker compose -f docker-compose.yml -f docker-compose.https.yml up --build
```

浏览器访问 `https://localhost` 时会提示自签名证书警告；公网部署必须替换为可信 CA 证书，不能使用该开发证书。

## API

- `GET /api/features`：功能目录，支持 `role` 和 `q`
- `GET /api/features/{id}`：功能详情及关联视频
- `GET /api/resources`：媒体目录，支持角色、类型和关键词筛选
- `GET /api/users/{user_id}/progress`：读取学习进度（需要 MySQL）
- `PUT /api/users/{user_id}/progress/{feature_id}`：写入 0-100 学习进度（需要 MySQL）
- `POST /api/auth/register`、`POST /api/auth/login`、`GET /api/auth/me`：账户注册、JWT 登录与当前身份；公开注册仅允许学生、教师和家长角色
- `POST /api/admin/users`：仅管理员可创建包含管理员在内的任意角色账户
- `GET /api/audit`：返回 2 份 PDF、93 页、17 个视频的机器可读审计报告，包括页级文本摘要、视频 SHA-256、时长、音视频流和采样时间点

### 班级 / 作业 / AI 纠音（需 MySQL + 登录）

- `POST/GET /api/classes`、`GET/PUT /api/classes/{id}`：创建与管理班级
- `POST /api/classes/join`：学生用邀请码加入
- `POST /api/classes/{id}/members`、`.../members/remove`、`.../regenerate-invite`
- `POST /api/assignments`：支持 `class_id` 按班级布置（choice / video / vocab / voice）
- `POST /api/assignments/{id}/tasks/{task_id}/voice`：上传语音并返回 AI 纠音分数（准确度/流利度/完整度/逐词反馈）
- `GET /api/pronunciation/status`、`POST /api/pronunciation/assess`、`GET /api/pronunciation/history`
- `GET/POST /api/words`、`GET/POST /api/questions`、`GET /api/my/vocab`

推荐流程：教师创建班级 → 分享邀请码 / 添加学员 → `/learn.html` 布置作业（可设截止时间）→ 学生完成并纠音 → 交卷 → 教师催交/批改；学生生成家长邀请码，家长绑定后查看学情。

### 家长学情

- `GET/POST /api/students/me/parent-invite`：学生获取/轮换家长邀请码
- `POST /api/parents/bind`：家长用邀请码绑定子女
- `GET /api/parents/children`、`GET /api/parents/children/{id}/overview`：查看作业与纠音学情

### 催交与截止

- `POST /api/assignments/{id}/urge`：催交未完成学员
- `POST /api/assignments/{id}/close`：教师手动关闭
- `POST /api/assignments/{id}/publish`：发布草稿并补同步班级学员
- 访问作业列表/详情时自动将过期作业设为 `closed`（MySQL/API 时区 `+08:00`）

### Azure 纠音配置（可选）

```bash
AZURE_SPEECH_KEY=your-key
AZURE_SPEECH_REGION=eastasia
AZURE_SPEECH_LANGUAGE=en-US
```

未配置 Azure 时：录音同步启用浏览器英文语音识别，后端按识别文本做**词级纠音**；若浏览器不支持识别则回退本地启发式。页面顶部会提示当前引擎。

素材中 `微信视频_20260903122647.mp4` 与 `微信视频_20260903122705.mp4` 的 SHA-256 相同。系统保留原文件并在资源目录标记重复项，不自动删除。

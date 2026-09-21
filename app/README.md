# 领智云英语教学资源中心

这是根据 `asset/` 中两份学生/教师 PDF 手册及 17 段录屏搭建的资源管理系统。

素材分析结果：学生手册 48 页，覆盖 11 个功能；教师手册 45 页，覆盖 8 个功能。系统将 19 个功能整理为结构化目录，并独立管理 17 个视频与 2 份 PDF，避免在没有依据时把视频错误归类。

- Python FastAPI：资源查询、角色筛选、统计接口，并直接托管素材
- Vue 3：固定版本随 Nginx 镜像打包，不依赖运行时 CDN；支持搜索、角色筛选、视频播放和 PDF 查看
- MySQL：启动时同步功能与媒体元数据，并提供功能-资源关联和学习进度表
- Nginx：反向代理 `/api` 与 `/assets`，为 MP4 提供 Range 请求、长连接和缓存策略

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

素材中 `微信视频_20260903122647.mp4` 与 `微信视频_20260903122705.mp4` 的 SHA-256 相同。系统保留原文件并在资源目录标记重复项，不自动删除。

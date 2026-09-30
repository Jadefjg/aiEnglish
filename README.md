# 领智云英语教学资源中心

这是根据 `asset/` 中两份学生/教师 PDF 手册及录屏搭建的资源管理系统，并已扩展为可实际布置/完成作业的学习工作台。

> **完整技术说明与部署手册**：见 [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md)（架构、环境变量、Docker/HTTPS、迁移、验收与排障）。

## 目录结构

```text
aiEnglish/
├── backend/          # FastAPI（业务 API、素材挂载）
├── frontend/         # Vue 静态页（教程中心 + 学习工作台）
├── db/               # schema + migrations（前后端共用数据模型）
├── nginx/            # 网关配置
├── docs/             # 部署与架构文档
├── scripts/          # 验收 / 证书脚本
├── asset/            # 教材视频与 PDF（运行时只读）
├── uploads/          # 运行时上传（语音等）
├── docker-compose*.yml
└── .env.example
```

前后端共用同一套 MySQL 库与 `uploads/` / `asset/` 数据路径；Compose 与本地开发均以**仓库根目录**为工作目录。

## 启动

```bash
python3 -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
cp .env.example .env   # 编辑 JWT / MySQL 等
uvicorn backend.main:app --reload --port 8000
```

开发模式直接访问 `http://localhost:8000`。未配置 MySQL 时自动使用文件目录作为只读数据源。前端 Vue vendor：`cd frontend && npm install`。

生产环境在仓库根目录执行：`docker compose up --build`，访问 `http://localhost:8080`。

已有 MySQL 数据库可单独执行迁移：

```bash
MYSQL_HOST=127.0.0.1 MYSQL_USER=root MYSQL_PASSWORD=... MYSQL_DATABASE=aienglish ./db/migrate.sh
```

本地 HTTPS：

```bash
./scripts/generate-dev-cert.sh
docker compose -f docker-compose.yml -f docker-compose.https.yml up --build
```

离线验收烟雾测试：

```bash
python scripts/acceptance_smoke.py
```

## 技术要点

- Python FastAPI：资源查询、账户认证、班级管理、作业布置与提交、AI 纠音评分
- Vue 3：教程中心 + `/learn.html` 学习工作台；固定版本随 Nginx 镜像打包
- MySQL：功能目录、班级、词库、题库、作业、提交与纠音历史
- AI 纠音：配置 `AZURE_SPEECH_KEY` 后走 Azure；未配置时使用本地启发式评分
- Nginx：反向代理 `/api`、`/assets`、`/uploads`；语音题需允许麦克风

生产安全：必须在 `.env` 中设置不少于 32 字符的 `JWT_SECRET` 和强 MySQL 密码。详情见 [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md)。

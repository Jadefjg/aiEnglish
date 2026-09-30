#!/usr/bin/env node
/** Hint: this frontend has no Vite/Webpack "dev" server. */
const msg = `
[aienglish-web] 没有 Vite 的 "dev" 构建脚本。

本前端是静态 HTML + Vue（vue.global.prod.js），正确启动方式：

  1) 推荐 Docker（含 Nginx + API + MySQL）
     docker compose up --build -d
     打开 http://localhost:8080/ 与 http://localhost:8080/learn.html

  2) 本地开发（API 直接托管静态页）
     cd frontend && npm install
     cd ..
     # 需已配置 MySQL / .env
     uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
     打开 http://localhost:8000/ 与 http://localhost:8000/learn.html

  仅预览静态文件（无 /api）：
     npm run serve
`;
console.error(msg.trim());
process.exit(1);

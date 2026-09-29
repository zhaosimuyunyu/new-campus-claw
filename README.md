# CampusClaw

课程资料与作业管理系统。账号与课程角色分离：同一用户可以在不同课程分别担任教师和学生。

## 启动

启动 Docker Desktop 的 Linux 引擎，然后在项目根目录运行：

```powershell
docker compose up --build -d
```

- 应用：http://localhost:5173
- 本地测试邮箱：http://localhost:8025
- 管理后台：http://localhost:5173/admin/
- 健康检查：http://localhost:5173/health/

首次创建管理员：

```powershell
docker compose exec backend python manage.py createsuperuser
```

登录管理后台，在 Teacher invites 创建邀请码，设置标签和过期时间。邀请码仅在保存成功提示中显示一次。用户完成邮箱注册后，在应用中兑换邀请码，即可创建课程。学生只需输入课程码。

本地发出的验证码在 Mailpit 中查看。配置真实 SMTP 时，复制 `.env.example` 为 `.env`，填写 SMTP 地址、端口、账户和授权密码。465 端口通常设置 `EMAIL_USE_SSL=1`、`EMAIL_USE_TLS=0`；587 端口通常相反。两者不能同时启用。然后重建 backend。不要把 `.env` 提交到版本库。真实收信需要实际邮箱验证，默认本地 Mailpit 不代表真实邮件已通过。

Compose 默认只绑定本机地址，使用开发密钥和 HTTP。若要部署到公网，必须设置随机 `DJANGO_SECRET_KEY`、HTTPS、`COOKIE_SECURE=1`、可信域名与反向代理；不能直接沿用本机配置。

```powershell
docker compose logs -f backend
docker compose down
```

数据库与上传文件保存在 Docker 卷中，`docker compose down` 不会删除。不要运行 `down -v`，除非确实要删除数据。

## 开发与检查

前端：`cd frontend; npm install; npm run dev`。后端：`cd backend; pip install -r requirements.txt; python manage.py migrate; python manage.py runserver 8000`。本机开发先运行 `docker compose up -d mailpit`，验证码即可在 http://localhost:8025 查看。本地后端不设置 PostgreSQL 环境变量时使用 SQLite，仅用于快速开发；正式 Docker 配置使用 PostgreSQL。

检查：`cd backend; python manage.py test core` 和 `cd frontend; npm run build`。Compose 启动后，检查 `docker compose ps` 和浏览器中的注册、课程、资料、作业完整流程。

## 课程 Agent

课程页面的 Agent 工作区支持服务端持久化线程、多轮消息、SSE 流式回答、停止生成、附件理解和 PDF/DOCX 产物预览下载。勾选“参考课程资料”才会访问 PostgreSQL + pgvector 知识库；关闭时是普通 Agent 对话。资料索引支持 TXT、Markdown、PDF、DOCX 的抽取与自动切片，向量模型未配置时会显示明确的索引失败原因。

后端通过环境变量连接模型和嵌入网关：`MODEL_PROVIDER`（`mock`、OpenAI 兼容或 `anthropic`）、`MODEL_API_BASE`、`MODEL_API_KEY`、`MODEL_NAME`、`EMBEDDING_API_BASE`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL` 与 `EMBEDDING_DIMENSIONS`。默认 `mock` 模式无需外部密钥即可验收流式流程。

受控工具统一经过 `ToolRegistry`：`search_course_knowledge`、`list_course_materials`、`create_pdf`、`create_docx`。工具执行入口为 `/api/agent/tools/{name}/execute/`，课程范围由服务端线程和 membership 决定；本轮不开放任意命令执行或外部写入型 MCP。

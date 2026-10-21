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

业务 API 使用 `Authorization: Bearer <access token>`，不接受 Django Session Cookie。登录或注册返回有效期 5 分钟的 access token 和有效期 7 天的 refresh token；`POST /api/auth/refresh/` 会轮换 refresh token，`POST /api/auth/logout/` 接受 `{"refresh":"..."}` 并使其失效。前端将 access token 保存在内存，将 refresh token 保存在当前标签页的 `sessionStorage`。密码重置后，旧 access token 因密码哈希校验而失效。管理后台 `/admin/` 仍使用 Django 自身的 Session 登录。

```powershell
docker compose logs -f backend
docker compose down
```

数据库与上传文件保存在 Docker 卷中，`docker compose down` 不会删除。不要运行 `down -v`，除非确实要删除数据。

## 开发与检查

推荐让后端、PostgreSQL 与邮件服务运行在 Docker，前端使用 Vite 热更新。在项目根目录打开 PowerShell：

```powershell
docker compose up --build -d backend
docker compose stop frontend
cd frontend
npm ci
npm run dev
```

访问 http://localhost:5173。Vite 将 `/api` 和 `/admin` 转发给 http://localhost:8000；停止容器前端是为了释放 5173 端口。后端容器使用根目录 `.env` 中的模型、数据库和邮件配置。修改后端代码后，在项目根目录重新执行 `docker compose up --build -d backend`。

如果前后端都用容器，在根目录执行 `docker compose up --build -d`，无需另开 `npm run dev`。通过 `docker compose ps` 查看状态、`docker compose logs -f backend` 查看后端日志。

如需本机运行 Python，先安装 `backend/requirements.txt`，配置指向本机映射端口 5433 的 PostgreSQL 环境变量，并设置模型、邮件环境变量，再运行 `python manage.py migrate` 和 `python -m uvicorn campus.asgi:application --host 127.0.0.1 --port 8000 --reload`（工作目录为 `backend`）。Python 不自动加载根目录 `.env`，Compose 的数据库主机名 `db` 和邮件主机名 `mailpit` 也不能直接用于本机。课程知识库需要 PostgreSQL + pgvector，建议优先使用上面的 Docker 后端方式。

检查：在根目录执行 `docker compose exec -T backend python manage.py test core`；在 `frontend` 执行 `npm test` 和 `npm run build`。Compose 启动后，检查 `docker compose ps` 和浏览器中的注册、课程、资料、作业完整流程。

### HTML 文件预览

Agent 的普通回复按 Markdown 展示。成功生成的 `html` 围栏代码块（允许片段），以及直接输出的完整 `<html>…</html>` 文档，会保存为 HTML 文件并显示卡片；一条回复可包含多个文件。无需模型必须输出围栏。停止或失败的回复保留文本，不生成 HTML 文件。

点击文件卡片才打开右侧预览面板，可切换效果与带行号、高亮的源码，复制、下载、展开或关闭。下载按钮不打开面板；窄屏使用覆盖面板。旧消息中的 HTML 可作为临时文件预览下载，不在读取历史时写入数据库。

预览支持 JavaScript 交互，使用不授予同源权限的 iframe 沙箱，并禁止连接请求、子框架和表单提交。外部 CSS、图片及脚本需要网络可用；依赖 API 请求的页面不在本轮支持范围内。HTML 源码接口 `/api/agent/artifacts/{id}/source/` 返回 UTF-8 纯文本，要求登录、文件所有者身份与有效课程成员资格。下载文件保存原始 HTML 内容。

## 课程 Agent

课程页面的 Agent 工作区支持服务端持久化线程、多轮消息、SSE 流式回答、停止生成、附件理解和 PDF/DOCX 产物预览下载。勾选“参考课程资料”才会访问 PostgreSQL + pgvector 知识库；关闭时是普通 Agent 对话。资料索引支持 TXT、Markdown、PDF、DOCX 的抽取与自动切片，向量模型未配置时会显示明确的索引失败原因。

后端通过环境变量连接模型和嵌入网关：`MODEL_PROVIDER`（`mock`、`openai` 或 `anthropic`）、`MODEL_API_BASE`、`MODEL_API_KEY`、`MODEL_NAME`、`EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL` 与 `EMBEDDING_DIM`。课程网关的 Base URL 应以 `/v1` 结尾，程序会分别接上 `/chat/completions` 和 `/embeddings`；向量维度为 2048。旧变量名 `EMBEDDING_API_BASE`、`EMBEDDING_DIMENSIONS` 仍可使用，但新变量优先。默认 `mock` 模式无需外部密钥即可验收流式流程。密钥只放在被 Git 忽略的 `.env`，不要提交到仓库。

受控工具统一经过 `ToolRegistry`：`search_course_knowledge`、`list_course_materials`、`create_pdf`、`create_docx`。工具执行入口为 `/api/agent/tools/{name}/execute/`，课程范围由服务端线程和 membership 决定；本轮不开放任意命令执行或外部写入型 MCP。

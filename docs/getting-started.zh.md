# 快速开始

## 1. 创建虚拟环境

```bash
uv sync --project backend
source backend/.venv/bin/activate
```

下面所有内容都假定在这个 venv 里。这不是约定，是硬要求：

!!! warning "一律用 `backend/.venv`，不要用系统 Python"
    测试要跑成 `backend/.venv/bin/python3 -m pytest ...`。系统 Python 缺锁定的
    wheel，而且带着一个过期的 `urllib3` —— 它的 `NotOpenSSLWarning` 垫片已经
    对不上运行时链接的 LibreSSL。失败发生在 **collection** 阶段：测试套件在第一
    个测试跑起来之前就 `ImportError` 退出，读起来像是仓库坏了，而不是解释器用错了。

    激活 venv 是让 `pytest` 导入到测试真正加载的那些模块的**唯一**办法。没有
    任何兜底配置能让系统解释器跑起来。

## 2. 准备 `.env` —— 可选

没有 `.env` 服务也能起来。只有在你要通知、非默认时区或非默认钥匙串时才需要建：

```bash
cp .env.example .env
```

`.env` 是 gitignored 的；`.env.example` 是提交进仓的非机密模板。它里面**没有凭据** ——
provider 的 API key 由运行时那层 provider 供给，两个通知 secret 走 macOS 钥匙串
（见[凭据](operations/configuration.zh.md#凭据)）。模板里每一行都是注释掉的，所以拷过去
本身什么也不改，只有你填了某一行才生效。

## 3. 启动服务

```bash
# 从仓库根运行。应用是一个包（`backend/`），所以 `-m` 才能把根目录和
# `backend/` 同时放进 `sys.path`。在 `backend/` 里跑 `python server.py`
# 会在 `from backend.framework...` 上报错。
python -m backend.server
```

服务**只绑回环**（`http://127.0.0.1:8000`），前端静态文件由同一个进程托管。
8000 被占用时它会向上找下一个空闲端口并打印出来；用 `PDT_PORT` 可以钉死。

浏览器打开 <http://localhost:8000> 用 UI。

## 4. 跑第一个 plan

UI 会走完整条工作流。如果你想用脚本驱动，同样的阶段都有 HTTP 端点 —— 而每个
`/api/*` 请求都需要额外一个请求头：

```bash
curl -H 'X-PDT-Request: 1' http://localhost:8000/api/plans
```

这个头不是密码。它存在的意义是让你恰好开着的某个网页没法驱动你的实例；机制见
[运行它](operations/running.md)。

最小的一轮往返：

| 步骤 | 调用 |
|---|---|
| 用一行需求创建 plan | `POST /api/interview/{plan_id}/start` |
| 回答澄清问题 | `POST /api/interview/{plan_id}/answer` |
| 生成 PRD | `POST /api/prd/{plan_id}/generate` |
| 取出每个决策点 | `GET /api/review/{plan_id}/review/items` |
| ……然后逐个接受 / 修订 / 跳过 | `POST /api/review/{plan_id}/review/item/{index}` |
| 生成任务 | `POST /api/tasks/{plan_id}/generate` |
| 启动执行 | `POST /api/execution/{plan_id}/start` |
| 观察进度 | `GET /api/execution/{plan_id}/progress` |
| 启动验证 | `POST /api/verification/{plan_id}/start` |

评审后的文档在 `GET /api/prd/{id}`、`GET /api/arch/{id}`、`GET /api/test/{id}`；
`GET /api/plan/{id}/summary` 是能一句话告诉你计划当前状态的那个调用。

## 接下来

- [工作流](workflow.md) —— 每个阶段产出什么，以及评审循环为什么存在。
- [运维 / 运行它](operations/running.md) —— 在暴露任何东西之前，先看本机限定范围
  和 request guard。
- [配置](operations/configuration.md) —— provider 路由与并发上限。

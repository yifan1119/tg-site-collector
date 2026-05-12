# tg-site-collector

> Multi-tenant site contact scraper · **Temporal workflow** + **Tavily search** + **Playwright nav-expand** + **Telegram trigger** + **FastAPI** · 关键词触发 → 批量抓取联系方式 → 推送结果

## 是什么

一个**完整的**多租户站点信息采集服务,从 monorepo 拆出来独立可跑:

- **Temporal workflow**:9 步主流程 · auth → guard → search → nav-expand → collect → persist → push → audit
- **Tavily search**:关键词 → 候选 URL 列表 · 自带配额预警(80% 提醒 / 耗尽推送)
- **Nav-expand**:导航站子站展开 · Playwright 顺序跑 · 50 候选 → +28% URL 覆盖
- **Multi-tenant 隔离**:每个 user 独立 history.json / url-cache / runs 目录 + asyncio.Lock 防并发污染
- **Skip-if-fresh**:已采集 URL 7 天内不重复跑
- **Cancel 落 partial**:workflow cancel 时落已采集部分到 disk
- **Telegram Bot**:`@BotFather` 拿 token · 私聊关键词触发 · 跑完结果推回
- **FastAPI HTTP**:trigger / runs / keyword-lists / credentials 4 个 router

## 完整功能模块

```
src/tg_site_collector/
├── workflows/site_collector.py        # ✅ 9 步 Temporal workflow
├── activities/
│   ├── auth_activity.py               # ⚠️  IAM stub · 默认 allow · 接你 IAM
│   ├── guard_activity.py              # ⚠️  护栏 stub · 默认 pass · 接你护栏
│   ├── audit_activity.py              # ⚠️  审计本地 JSONL · 接你 SIEM
│   ├── search_activity.py             # ✅ Tavily 关键词搜索
│   ├── nav_extract_activity.py        # ✅ Playwright 导航站展开
│   ├── collect_activity.py            # ✅ 站点采集 + ThreadPoolExecutor + heartbeat
│   ├── data_io_activity.py            # ✅ 多租户 history.json + url-cache + lock
│   └── tg_activity.py                 # ✅ Telegram 推送
├── services/
│   ├── browser_pool.py                # ✅ Playwright 单例 chromium 池
│   ├── collector_core.py              # ✅ 单站点联系方式抓取(BS4 + lxml)
│   ├── keyword_library.py             # ✅ 关键词库
│   ├── keyword_lists.py               # ✅ 关键词列表 CRUD 服务
│   ├── temporal_client.py             # ✅ 触发 workflow helper
│   └── tg_client.py                   # ✅ Telegram bot 客户端
├── workers/
│   └── site_collector_worker.py       # ✅ Temporal worker 入口
├── agent_tools/
│   └── site_collector_tool.py         # ✅ Agent tool schema(给 LLM 调用)
├── telegram_bot/
│   ├── bot.py                         # ✅ TG bot polling 入口(简化版 · 删 LLM)
│   ├── b_module_router.py             # ✅ 关键词 → workflow 触发
│   ├── welcome.py                     # ✅ /start /help 命令
│   ├── user_mapping.py                # ✅ TG user_id ↔ tenant 绑定(JSON 持久化)
│   └── messages.py                    # ✅ 文案
├── api/
│   ├── app.py                         # ✅ FastAPI app(简化版 · 删 DB)
│   ├── trigger.py                     # ✅ POST /api/workflow/trigger
│   ├── runs.py                        # ✅ GET  /api/workflow/runs/{run_id}
│   ├── keyword_lists.py               # ✅ /api/keyword-lists/* CRUD
│   └── credentials.py                 # ✅ verify-bot / verify-tavily
├── common/
│   └── auth.py                        # ⚠️  JWT / UserContext stub · 接你 IAM
└── types.py                           # ✅ CollectorMode / State / Context / Summary
```

✅ = 完整可用 / ⚠️ = stub 需接你自己实现

## 架构图

```
                    ┌─────────────────┐
关键词 (TG / HTTP) → │   trigger       │ → start_workflow
                    │ ──────────────  │
                    │  workflow runs  │
                    └────────┬────────┘
                             ↓
                     ┌───────────────┐
                     │ 1 auth        │ ← stub
                     │ 2 pre guard   │ ← stub
                     │ 3 load cache  │
                     │ 4 Tavily 搜索 │
                     │ 5 nav-expand  │ ← Playwright
                     │ 6 collect     │ ← ThreadPool + heartbeat
                     │ 7 post guard  │ ← stub
                     │ 8 persist     │ ← history.json + lock
                     │ 9 tg + audit  │
                     └───────────────┘
                             ↓
                     ┌───────────────┐
                     │ 结果文件落地  │ → data/<tenant>/runs/<run_id>.json
                     │ Telegram 推送 │
                     └───────────────┘
```

## 快速开始

### 装依赖

```bash
pip install -e ".[dev]"
playwright install chromium
```

### 准备 .env

```bash
cp .env.example .env
# 填:
#   TAVILY_API_KEY=tvly-xxx           (必需)
#   TG_BOT_TOKEN=xxx:xxx              (可选 · TG bot)
#   TEMPORAL_HOST=localhost:7233
```

### 起 Temporal server

```bash
docker run --rm -p 7233:7233 -p 8233:8233 temporalio/auto-setup:1.24
# Temporal UI: http://localhost:8233
```

### 起 worker

```bash
python -m tg_site_collector.workers.site_collector_worker
```

### 3 种触发方式

#### A · FastAPI HTTP

```bash
uvicorn tg_site_collector.api.app:app --host 0.0.0.0 --port 8002
# POST /api/workflow/trigger
# 注:默认 JWT stub allow · 接你 IAM 前任何人可调
curl -X POST http://localhost:8002/api/workflow/trigger \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"demo","user_id":"u1","keywords":["手机壳","充电器"],"mode":"single"}'
```

#### B · Telegram Bot

```bash
# 起 bot polling
python -m tg_site_collector.telegram_bot.bot
# 私聊 bot 发关键词 · 路由命中即触发 workflow
```

#### C · Python 代码直接调

```python
import asyncio
from temporalio.client import Client
from tg_site_collector.workflows import SITE_COLLECTOR_WORKFLOW_NAME
from tg_site_collector.types import WorkflowContext, CollectorMode

async def main():
    client = await Client.connect('localhost:7233')
    handle = await client.start_workflow(
        SITE_COLLECTOR_WORKFLOW_NAME,
        WorkflowContext(
            tenant_id='demo', user_id='demo_user',
            keywords=['手机壳', '充电器'],
            mode=CollectorMode.SINGLE, tg_chat_id=None,
        ),
        id=f'site-collector-demo',
        task_queue='site-collector',
    )
    print(await handle.result())

asyncio.run(main())
```

## 生产前必接的 3 个 stub

### 1. IAM 鉴权(`activities/auth_activity.py`)

`AUTH_MODE=stub` 默认全 allow。生产改:
- `AUTH_MODE=http` + 取消注释 httpx 代码 + 改 `AUTH_BASE` 指你 IAM endpoint
- 或集成 OAuth / Keycloak / Auth0 / DB 查 employees

### 2. 护栏(`activities/guard_activity.py`)

当前 `phase ∈ {pre, mid, post}` 全 pass。生产改成 PII / 违禁内容 / 越权检查。

### 3. 审计(`activities/audit_activity.py`)

当前写本地 `./data/audit/audit.jsonl`。生产改成调 SIEM / 审计 endpoint(满足 SOC2 / GDPR 留存)。

## 多租户隔离设计

每个 `tenant_id` 完全隔离 · `data/<tenant_id>/`:

```
data/<tenant>/
├── history.json       # 已跑过的关键词 / URL / 时间戳
├── url-cache.txt      # skip-if-fresh 的去重池
└── runs/
    └── <run_id>.json  # 每次运行完整结果(7 天清理)
```

`asyncio.Lock` 保护同租户并发写,跨租户读写互不影响。

## 配置(`.env`)

| Env | 必需 | 默认 | 说明 |
|---|---|---|---|
| `TAVILY_API_KEY` | ✅ | — | https://tavily.com 拿 |
| `TEMPORAL_HOST` | ✅ | `localhost:7233` | Temporal server |
| `TEMPORAL_TASK_QUEUE` | | `site-collector` | task queue 名 |
| `TG_BOT_TOKEN` | | — | @BotFather 拿 · 不填则只走 API |
| `TELEGRAM_ADMIN_CHAT_ID` | | — | 配额预警 / 异常推送 |
| `DATA_ROOT` | | `./data` | 多租户数据根目录 |
| `B_AUDIT_DIR` | | `./data/audit` | 审计 JSONL 目录 |
| `AUTH_MODE` | | `stub` | `stub` / `http` / `deny` |
| `AUTH_BASE` | | — | `http` 模式下你的 IAM endpoint |
| `JWT_SECRET` | | — | 真鉴权时必填 |
| `PLAYWRIGHT_HEADLESS` | | `true` | nav-expand 浏览器模式 |
| `NAV_EXPAND_MAX_CANDIDATES` | | `50` | 单次 nav 站候选上限 |

## 状态

- ✅ Temporal workflow 9 步主流程
- ✅ 多租户隔离 IO + lock
- ✅ Tavily 配额预警(80% / 耗尽)
- ✅ Nav-expand(+28% URL 覆盖)
- ✅ Cancel 落 partial
- ✅ Skip-if-fresh(7 天)
- ✅ Telegram Bot 入口(简化版)
- ✅ FastAPI HTTP 入口(4 router)
- ✅ Agent tool schema(给 LLM 调用)
- ⚠️ IAM / Guard / Audit 是 stub · 生产前必须接
- ⏳ 测试覆盖率低 · 待完善

## 来源

从 monorepo 拆出的完整 B 模块站点采集服务 · 跨模块 IAM / DB / LLM 依赖全部 stub 化为独立 common · 可独立部署给同事 / 客户用。

## License

[Apache 2.0](LICENSE)

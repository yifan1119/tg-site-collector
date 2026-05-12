# tg-site-collector

> Multi-tenant site contact scraper · **Temporal workflow** + **Tavily search** + **Playwright nav-expand** · 关键词触发 → 批量抓取联系方式 → 推送结果

## 是什么

一个**多租户**站点信息采集服务的核心实现:

- **Temporal workflow**:9 步主流程 · auth → guard → search → nav-expand → collect → persist → push → audit
- **Tavily search**:关键词 → 候选 URL 列表 · 自带配额预警(80% 提醒 / 耗尽推送)
- **Nav-expand**:导航站子站展开 · Playwright 顺序跑 · 50 候选 → +28% URL 覆盖
- **Multi-tenant 隔离**:每个 user 独立 history.json / url-cache / runs 目录 + asyncio.Lock 防并发污染
- **Skip-if-fresh**:已采集 URL 7 天内不重复跑
- **Cancel 落 partial**:workflow cancel 时落已采集部分到 disk

## 架构

```
关键词 → workflow start
            ↓
       a_m2_check (auth)       ← stub allow · 自己接 IAM
       s_evaluate (pre guard)   ← stub · 自己接护栏
       load url-cache          
       search_keywords_batch    ← Tavily
       nav_expand_batch         ← Playwright
       collect_sites_batch      ← BeautifulSoup + lxml 抓联系方式
       s_evaluate (post guard)
       persist history          ← JSON 落地 + lock
       tg summary               ← Telegram 推送(可选)
       o_log_event (audit)      ← stub · 写本地 JSONL
```

详见 [`src/tg_site_collector/workflows/site_collector.py`](src/tg_site_collector/workflows/site_collector.py) 9 步流程。

## 快速开始

```bash
# 1. 装依赖
pip install -e ".[dev]"
playwright install chromium

# 2. 准备 .env
cp .env.example .env
# 填入 TAVILY_API_KEY · TELEGRAM_BOT_TOKEN · 等

# 3. 起 Temporal server(本地 dev)
docker run --rm -p 7233:7233 -p 8233:8233 temporalio/auto-setup:1.24

# 4. 起 worker
python -m tg_site_collector.workers.site_collector_worker

# 5. 触发 workflow(代码示例)
python -c "
import asyncio
from temporalio.client import Client
from tg_site_collector.workflows import SITE_COLLECTOR_WORKFLOW_NAME
from tg_site_collector.types import WorkflowContext, CollectorMode

async def main():
    client = await Client.connect('localhost:7233')
    handle = await client.start_workflow(
        SITE_COLLECTOR_WORKFLOW_NAME,
        WorkflowContext(
            tenant_id='demo',
            user_id='demo_user',
            keywords=['手机壳', '充电器'],
            mode=CollectorMode.SINGLE,
            tg_chat_id=None,
        ),
        id=f'site-collector-demo-{int(__import__(\"time\").time())}',
        task_queue='site-collector',
    )
    result = await handle.result()
    print(result)

asyncio.run(main())
"
```

## 自定义鉴权

默认 `auth_activity.a_m2_check` 是 stub · **全部 allow**。生产前必须接你自己的 IAM。

3 种接入方式:

### 1. HTTP 调你自己的 IAM endpoint(推荐)
改 `src/tg_site_collector/activities/auth_activity.py`,取消注释 httpx 代码,改 contract 匹配你的 IAM。

### 2. 集成 OAuth / Keycloak / Auth0
同上,把 stub 实现换成对应 SDK 调用。

### 3. 直接 DB 查
import 你的 ORM,查 employees / roles 表。

设环境变量 `AUTH_MODE=http` 切到真调模式。

## 自定义护栏 / 审计

- **`guard_activity.s_evaluate`**:前置 / 中置 / 后置 guard · 当前 stub pass · 替换成 PII / 违禁内容 / 越权检查
- **`audit_activity.o_log_event`**:当前写本地 JSONL · 替换成你的审计 endpoint / SIEM

⚠️ **生产前必须接**。MVP 默认 stub 等于绕过护栏 + 审计。

## 文件结构

```
src/tg_site_collector/
├── workflows/site_collector.py        # 9 步主流程
├── activities/
│   ├── auth_activity.py               # IAM stub
│   ├── guard_activity.py              # 护栏 stub
│   ├── audit_activity.py              # 审计 stub
│   ├── search_activity.py             # Tavily 关键词搜索
│   ├── nav_extract_activity.py        # Playwright 导航站展开
│   ├── collect_activity.py            # 站点采集 (ThreadPoolExecutor + heartbeat)
│   ├── data_io_activity.py            # 多租户 history.json + url-cache + lock
│   └── tg_activity.py                 # Telegram 推送
├── services/
│   ├── browser_pool.py                # Playwright 单例 chromium 池
│   ├── collector_core.py              # 单站点联系方式抓取
│   ├── keyword_library.py             # 关键词库
│   ├── keyword_lists.py               # 关键词列表服务
│   ├── temporal_client.py             # 触发 workflow helper
│   └── tg_client.py                   # Telegram bot 客户端
├── workers/site_collector_worker.py   # Temporal worker 入口
├── agent_tools/
│   └── site_collector_tool.py         # Agent tool schema(给 LLM 调用用)
└── types.py                           # CollectorMode / CollectorState / WorkflowContext / RunSummary
```

## 多租户隔离设计

每个 `tenant_id` 完全隔离:

```
data/<tenant_id>/
├── history.json       # 已跑过的关键词 / URL / 时间戳
├── url-cache.txt      # skip-if-fresh 的去重池
└── runs/
    └── <run_id>.json  # 每次运行的完整结果(7 天清理)
```

`asyncio.Lock` 保护同租户并发写,跨租户读写互不影响。

## 配置(`.env`)

```bash
# Temporal
TEMPORAL_HOST=localhost:7233
TEMPORAL_TASK_QUEUE=site-collector

# Tavily(必需)
TAVILY_API_KEY=tvly-xxx

# Telegram(可选 · 推送结果用)
TELEGRAM_BOT_TOKEN=xxx:xxx
TELEGRAM_ADMIN_CHAT_ID=

# 数据目录
DATA_ROOT=./data
B_AUDIT_DIR=./data/audit

# 鉴权模式 · stub(默认) / http / deny
AUTH_MODE=stub
AUTH_BASE=http://localhost:8000

# Browser pool
PLAYWRIGHT_HEADLESS=true
NAV_EXPAND_MAX_CANDIDATES=50
```

## 状态

- ✅ Temporal workflow 9 步主流程
- ✅ 多租户隔离 IO + lock
- ✅ Tavily 配额预警(80% / 耗尽)
- ✅ Nav-expand(+28% URL 覆盖)
- ✅ Cancel 落 partial
- ✅ Skip-if-fresh(7 天)
- ⚠️ IAM / Guard / Audit 是 stub · 生产前必须接
- ⏳ 测试覆盖率低 · 待完善
- ⏳ Telegram bot 入口(已删 · 同事自己加 simple polling 即可)

## 来源

从 monorepo 拆出的核心 workflow + activities · v0.1 起 9 步流程 · v0.2 起 fail-closed auth。

## License

[Apache 2.0](LICENSE)

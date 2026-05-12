"""SiteCollectorWorkflow · B 模块第一个 workflow。

确定性约束:
- 不能用 time.time / random / requests / 文件 IO （都在 activity 里做）
- workflow.now() 替代 datetime.now
- workflow.logger 替代 logging
- 所有 IO 通过 execute_activity
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, CancelledError
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from tg_site_collector.types import (
        CollectorMode,
        CollectorState,
        RunSummary,
        WorkflowContext,
        chunk,
    )

SITE_COLLECTOR_WORKFLOW_NAME = "site-info-collector"


@workflow.defn(name=SITE_COLLECTOR_WORKFLOW_NAME, sandboxed=False)
class SiteCollectorWorkflow:
    """主流程:
    1. auth (A.M2.check stub)
    2. pre_guard (S.evaluate pre stub)
    3. load cache (data_io)
    4. search loop (per keyword batch · search_keywords_batch)
    5. collect loop (per site batch · collect_sites_batch)
    6. post_guard (S.evaluate post stub)
    7. persist history (data_io)
    8. tg summary
    9. audit (O.log stub)
    """

    def __init__(self) -> None:
        self.state = CollectorState()

    # ── Query: 让 Dashboard / B.runs.get 拉实时进度 ──
    @workflow.query
    def get_progress(self) -> CollectorState:
        return self.state

    @workflow.run
    async def run(self, ctx: WorkflowContext) -> RunSummary:
        start = workflow.now()
        run_id = workflow.info().workflow_id

        try:
            # 1. auth · design note · 真调 A.M2.check · deny 抛 PermissionError →
            # workflow FAIL(由上游 retry / cancel · ABANDON 走 cleanup)
            await workflow.execute_activity(
                "a_m2_check",
                args=[ctx.tenant_id, ctx.user_id, "workflow:site-collector", "write"],
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(
                    maximum_attempts=2,
                    non_retryable_error_types=["PermissionError"],
                ),
            )

            # 2. pre guard
            await workflow.execute_activity(
                "s_evaluate",
                args=[ctx.tenant_id, "pre", {"mode": ctx.mode.value}],
                start_to_close_timeout=timedelta(seconds=15),
            )

            # 3. load cache
            cache: list[str] = await workflow.execute_activity(
                "io_read_url_cache",
                args=[ctx.tenant_id],
                start_to_close_timeout=timedelta(seconds=30),
            )

            # 4. search loop
            new_urls: list[str] = []
            tavily_quota_hit = False
            if ctx.mode in (CollectorMode.COLLECT, CollectorMode.SEARCH_NEW) and ctx.keywords:
                self.state.phase = "searching"
                kw_groups = chunk(ctx.keywords, ctx.keywords_per_batch)
                self.state.keyword_batches_total = len(kw_groups)

                for idx, group in enumerate(kw_groups):
                    try:
                        batch_urls: list[str] = await workflow.execute_activity(
                            "search_keywords_batch",
                            args=[
                                ctx.tenant_id,
                                ctx.tg_chat_id,
                                ctx.bot_token,
                                ctx.tavily_api_key,
                                group,
                                idx,
                                len(kw_groups),
                            ],
                            start_to_close_timeout=timedelta(minutes=10),
                            heartbeat_timeout=timedelta(seconds=90),
                            retry_policy=RetryPolicy(
                                maximum_attempts=2,
                                non_retryable_error_types=["TavilyQuotaExhausted"],
                            ),
                        )
                        for u in batch_urls:
                            if u not in cache and u not in new_urls:
                                new_urls.append(u)
                        self.state.keyword_batches_done = idx + 1
                        self.state.new_urls_found = len(new_urls)
                    except ActivityError as exc:
                        cause_name = type(exc.cause).__name__ if exc.cause else ""
                        if "TavilyQuotaExhausted" in (cause_name + str(exc)):
                            workflow.logger.warning("tavily quota hit, skip rest")
                            self.state.last_error = "tavily_quota_exhausted"
                            tavily_quota_hit = True
                            break
                        raise

                # 把新搜到的 URL 写回 cache
                if new_urls:
                    await workflow.execute_activity(
                        "io_write_url_cache",
                        args=[ctx.tenant_id, new_urls],
                        start_to_close_timeout=timedelta(seconds=30),
                    )

            # 4.5 导航站子站展开(可选 · ctx.nav_expand 控制 · 默认开)
            # 把 search 出来的 URL 里疑似导航站的丢给 Playwright 渲染抓子站列表
            # · 实测 URL +28% / contacts ×2-3 · 用时 ×2-3
            # (deep-stress-v2: 1580→2033 URL · 4919 contacts · 27 min)
            if ctx.nav_expand and new_urls:
                # 启发式筛选导航站候选(URL pattern)
                from tg_site_collector.activities.nav_extract_activity import (
                    select_nav_candidates,
                )

                candidates = select_nav_candidates(
                    new_urls, max_candidates=ctx.nav_max_candidates
                )
                if candidates:
                    self.state.phase = "nav_expanding"
                    workflow.logger.info(
                        f"nav-expand: {len(candidates)} 候选导航站(总 URL {len(new_urls)})"
                    )
                    nav_result = await workflow.execute_activity(
                        "nav_expand_batch",
                        args=[ctx.tenant_id, candidates],
                        start_to_close_timeout=timedelta(minutes=15),
                        heartbeat_timeout=timedelta(seconds=60),
                        retry_policy=RetryPolicy(maximum_attempts=2),
                    )
                    expanded_subsites: list[str] = nav_result.get("new_subsites", [])
                    if expanded_subsites:
                        # 把子站去重后并入 new_urls + 写入 cache
                        before = len(new_urls)
                        for sub in expanded_subsites:
                            if sub not in cache and sub not in new_urls:
                                new_urls.append(sub)
                        added = len(new_urls) - before
                        self.state.new_urls_found = len(new_urls)
                        workflow.logger.info(
                            f"nav-expand 出: {added} 新子站 · 总 URL → {len(new_urls)}"
                        )
                        if added > 0:
                            await workflow.execute_activity(
                                "io_write_url_cache",
                                args=[ctx.tenant_id, expanded_subsites],
                                start_to_close_timeout=timedelta(seconds=30),
                            )

            # 5. collect loop
            # search_new 模式:仅采本次新发现的 URL(不重采老缓存);
            # collect 模式:全量缓存 + 新 URL 一起采;
            # push_only 模式:全量缓存(不搜不增量)
            if ctx.mode == CollectorMode.SEARCH_NEW:
                urls_to_collect = sorted(set(new_urls))
            else:
                urls_to_collect = sorted(set(cache) | set(new_urls))

            # 增量优化: 跳过最近 skip_if_fresh_hours 内已采过的 URL
            # ctx.skip_if_fresh_hours <= 0 时 io_get_recent_urls 返 [] · 不跳过
            if ctx.skip_if_fresh_hours > 0 and urls_to_collect:
                recent: list[str] = await workflow.execute_activity(
                    "io_get_recent_urls",
                    args=[ctx.tenant_id, ctx.skip_if_fresh_hours],
                    start_to_close_timeout=timedelta(seconds=30),
                )
                skip_set = {u.rstrip("/") for u in recent}
                before = len(urls_to_collect)
                urls_to_collect = [
                    u for u in urls_to_collect if u.rstrip("/") not in skip_set
                ]
                skipped = before - len(urls_to_collect)
                if skipped:
                    workflow.logger.info(
                        f"skip_if_fresh: 跳过 {skipped}/{before} 个 "
                        f"{ctx.skip_if_fresh_hours}h 内已采的 URL · 待采 {len(urls_to_collect)}"
                    )
            all_results: list[dict[str, Any]] = []
            collect_modes = (
                CollectorMode.COLLECT,
                CollectorMode.PUSH_ONLY,
                CollectorMode.SEARCH_NEW,  # 搜新的也接着采
            )
            if ctx.mode in collect_modes and urls_to_collect:
                self.state.phase = "collecting"
                site_batches = chunk(urls_to_collect, ctx.sites_per_batch)
                self.state.site_batches_total = len(site_batches)

                deadline = workflow.now() + timedelta(minutes=90)
                for idx, batch in enumerate(site_batches):
                    if workflow.now() >= deadline:
                        workflow.logger.warning(
                            f"90 min wallclock cap, processed {self.state.site_batches_done}/{len(site_batches)}"
                        )
                        break
                    batch_result: dict[str, Any] = await workflow.execute_activity(
                        "collect_sites_batch",
                        args=[
                            ctx.tenant_id,
                            ctx.tg_chat_id,
                            ctx.bot_token,
                            batch,
                            idx,
                            len(site_batches),
                            ctx.workers,
                            ctx.timeout_per_site,
                        ],
                        start_to_close_timeout=timedelta(minutes=30),
                        heartbeat_timeout=timedelta(minutes=3),
                        retry_policy=RetryPolicy(
                            maximum_attempts=2,
                            initial_interval=timedelta(seconds=10),
                        ),
                    )
                    all_results.extend(batch_result["sites"])
                    self.state.site_batches_done = idx + 1
                    self.state.sites_collected += batch_result["site_count"]
                    self.state.contacts_extracted += batch_result["contact_count"]

            # 6. post guard
            await workflow.execute_activity(
                "s_evaluate",
                args=[
                    ctx.tenant_id,
                    "post",
                    {
                        "sites": self.state.sites_collected,
                        "contacts": self.state.contacts_extracted,
                    },
                ],
                start_to_close_timeout=timedelta(seconds=15),
            )

            # 7. persist
            self.state.phase = "pushing"
            if all_results:
                await workflow.execute_activity(
                    "io_persist_history",
                    args=[ctx.tenant_id, all_results],
                    start_to_close_timeout=timedelta(minutes=2),
                )

            # 8. tg summary - non-fatal
            #    数据已落 history.json,推送失败不能让整 workflow FAIL · 否则 audit 也丢
            duration_sec = int((workflow.now() - start).total_seconds())
            try:
                await workflow.execute_activity(
                    "tg_send_summary",
                    args=[
                        ctx.bot_token,
                        ctx.tg_chat_id,
                        {
                            **self.state.model_dump(),
                            "duration_sec": duration_sec,
                            "tavily_quota_hit": tavily_quota_hit,
                        },
                        None,  # history_path 暂时不发整个 history (太大)
                    ],
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except ActivityError as exc:
                workflow.logger.warning(
                    f"tg_send_summary failed after retries (non-fatal): {exc}"
                )
                self.state.last_error = "tg_summary_failed"

            # 9. audit - 始终写,即使 tg push 挂了
            await workflow.execute_activity(
                "o_log_event",
                args=[
                    ctx.tenant_id,
                    "site_collector.completed",
                    {
                        "run_id": run_id,
                        "mode": ctx.mode.value,
                        **self.state.model_dump(),
                        "duration_sec": duration_sec,
                        "tavily_quota_hit": tavily_quota_hit,
                    },
                ],
                start_to_close_timeout=timedelta(seconds=10),
            )

            self.state.phase = "done"
            return RunSummary(
                tenant_id=ctx.tenant_id,
                run_id=run_id,
                sites_collected=self.state.sites_collected,
                contacts_extracted=self.state.contacts_extracted,
                new_urls_found=self.state.new_urls_found,
                duration_sec=duration_sec,
                tavily_quota_hit=tavily_quota_hit,
            )

        except CancelledError:
            workflow.logger.warning("workflow cancelled")
            self.state.phase = "cancelled"
            self.state.last_error = "cancelled by user"
            # ⚠️ Temporal 1.27 没有 workflow.shielded · 用 ActivityCancellationType.ABANDON
            # 让 cleanup activities 不被级联取消(workflow 取消信号到 server 但 activity
            # 已 ABANDON 模式 server 不通知 worker · activity 跑完结果丢弃)。
            # 这是官方推荐的 cancel cleanup 模式。
            try:
                await workflow.execute_activity(
                    "io_persist_partial",
                    args=[ctx.tenant_id, run_id, self.state.model_dump()],
                    start_to_close_timeout=timedelta(seconds=30),
                    cancellation_type=ActivityCancellationType.ABANDON,
                )
                await workflow.execute_activity(
                    "tg_notify_cancelled",
                    args=[ctx.bot_token, ctx.tg_chat_id, run_id],
                    start_to_close_timeout=timedelta(seconds=15),
                    cancellation_type=ActivityCancellationType.ABANDON,
                )
                await workflow.execute_activity(
                    "o_log_event",
                    args=[
                        ctx.tenant_id,
                        "site_collector.cancelled",
                        self.state.model_dump(),
                    ],
                    start_to_close_timeout=timedelta(seconds=10),
                    cancellation_type=ActivityCancellationType.ABANDON,
                )
            except Exception as cleanup_exc:  # pragma: no cover - 防御
                workflow.logger.warning(f"cleanup activity also failed: {cleanup_exc}")
            raise

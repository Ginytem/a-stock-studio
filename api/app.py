# -*- coding: utf-8 -*-
"""
===================================
FastAPI 应用工厂模块
===================================

职责：
1. 创建和配置 FastAPI 应用实例
2. 配置 CORS 中间件
3. 注册路由和异常处理器
4. 托管前端静态文件（生产模式）

使用方式：
    from api.app import create_app
    app = create_app()
"""

import asyncio
import json
import logging
import mimetypes

import sys

if sys.platform == "win32" and not mimetypes.inited:
    _orig_read_windows_registry = getattr(mimetypes.MimeTypes, 'read_windows_registry', None)
    if _orig_read_windows_registry is not None:
        mimetypes.MimeTypes.read_windows_registry = lambda self, strict=True: None
        try: mimetypes.init()
        finally: mimetypes.MimeTypes.read_windows_registry = _orig_read_windows_registry
    else:
        mimetypes.init()
import os
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote
from typing import List, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

# Match src="/assets/foo.js" / href="/assets/foo.css" produced by the
# vite build. Used by the startup self-check to surface packaging
# mismatches early (see GitHub #1064 / #1065 / #1050).
_INDEX_ASSET_REF_PATTERN = re.compile(
    r"""(?:src|href)\s*=\s*["'](/assets/[^"']+)["']""",
    re.IGNORECASE,
)
_FRONTEND_ASSET_MEDIA_TYPES = {
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
}
_SAFE_MISSING_ASSET_MEDIA_TYPES = frozenset(_FRONTEND_ASSET_MEDIA_TYPES.values())
_FRONTEND_INDEX_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _frontend_index_response(static_dir: Path) -> FileResponse:
    return FileResponse(
        static_dir / "index.html",
        headers=_FRONTEND_INDEX_NO_CACHE_HEADERS,
    )


def _check_frontend_assets_consistency(static_dir: Path) -> List[str]:
    """
    Verify that ``index.html`` only references assets that actually exist
    under ``static_dir``. Returns the list of missing references; an empty
    list means the bundle is consistent.

    Logs an actionable error when a mismatch is detected so the root cause
    is visible in ``logs/desktop.log`` instead of surfacing as a silent
    blank page.
    """
    index_html = static_dir / "index.html"
    if not index_html.is_file():
        return []
    try:
        html = index_html.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Failed to read %s for asset check: %s", index_html, exc)
        return []

    missing: List[str] = []
    for match in _INDEX_ASSET_REF_PATTERN.finditer(html):
        ref = match.group(1)
        candidate = static_dir / ref.lstrip("/")
        if not candidate.is_file() and ref not in missing:
            missing.append(ref)

    if missing:
        logger.error(
            "Frontend bundle is inconsistent: index.html references %d asset(s) "
            "that are not present on disk under %s. This will surface as a "
            "blank page in the desktop app (see GitHub #1064 / #1065). "
            "Missing: %s. Re-run the frontend build and make sure the packaging "
            "step copies the freshly generated static/ directory.",
            len(missing),
            static_dir,
            ", ".join(missing),
        )
    return missing


def _resolve_asset_path(assets_dir: Path, asset_path: str) -> Optional[Path]:
    """Resolve a requested asset path while keeping it confined to assets_dir."""
    decoded_path = unquote(asset_path)
    if not decoded_path or decoded_path.startswith(("/", "\\")):
        return None
    if "\x00" in decoded_path:
        return None
    if "\\" in decoded_path:
        return None
    if ":" in decoded_path.split("/", 1)[0]:
        return None

    assets_root = assets_dir.resolve()
    candidate = (assets_root / decoded_path).resolve()
    if not candidate.is_relative_to(assets_root):
        return None
    return candidate


def _register_frontend_asset_mime_types() -> None:
    """Keep Vite module assets loadable even when OS MIME maps are wrong."""
    for suffix, media_type in _FRONTEND_ASSET_MEDIA_TYPES.items():
        mimetypes.add_type(media_type, suffix)


def _frontend_asset_media_type(asset_path: str) -> Optional[str]:
    suffix = Path(asset_path).suffix.lower()
    if suffix in _FRONTEND_ASSET_MEDIA_TYPES:
        return _FRONTEND_ASSET_MEDIA_TYPES[suffix]
    content_type, _ = mimetypes.guess_type(asset_path)
    return content_type


def _missing_asset_media_type(asset_path: str) -> str:
    """Return a safe media type for a missing asset response."""
    content_type = _frontend_asset_media_type(asset_path)
    if content_type in _SAFE_MISSING_ASSET_MEDIA_TYPES:
        return content_type
    return "text/plain"


def _warn_if_open_cors_without_auth() -> None:
    if is_auth_enabled():
        return
    logger.warning(
        "CORS_ALLOW_ALL=true is enabled while ADMIN_AUTH_ENABLED is false. "
        "The API will accept browser requests from any origin; only use this "
        "on trusted local networks or enable admin authentication."
    )

from api.v1 import api_v1_router
from api.middlewares.auth import add_auth_middleware
from api.middlewares.error_handler import add_error_handlers
from api.v1.schemas.common import HealthResponse
from src.auth import is_auth_enabled
from src.data.stock_index_loader import find_existing_stock_index_path
from src.services.system_config_service import SystemConfigService
from src.services.runtime_scheduler import (
    CLI_SCHEDULER_OWNER_ENV,
    RUNTIME_SCHEDULER_ARGS_ENV,
    RUNTIME_SCHEDULER_FORCE_ENABLED_ENV,
    RUNTIME_SCHEDULER_RUN_IMMEDIATELY_ENV,
    RUNTIME_SCHEDULER_SUPPRESS_START_ENV,
    RuntimeSchedulerService,
)
from src.services.stock_index_remote_service import (
    get_remote_stock_index_cache_path,
    refresh_remote_stock_index_cache,
    settings_from_config,
)


_STOCK_INDEX_FILENAME = "stocks.index.json"
_STOCK_INDEX_HEADERS = {
    "Cache-Control": "no-cache",
}


def _bundled_stock_index_path() -> Path:
    return Path(__file__).parent.parent / "apps" / "dsa-web" / "public" / _STOCK_INDEX_FILENAME


async def _refresh_stock_index_cache_in_background(reason: str) -> None:
    try:
        from src.config import get_config

        settings = settings_from_config(get_config())
        result = await run_in_threadpool(refresh_remote_stock_index_cache, settings)
        if result.refreshed:
            logger.info("[stock-index] background refresh completed (%s): %s", reason, result.cache_path)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - index refresh must stay best-effort.
        logger.warning("[stock-index] background refresh failed (%s): %s", reason, exc)


def _schedule_stock_index_background_refresh(app: FastAPI, reason: str) -> None:
    task = getattr(app.state, "stock_index_refresh_task", None)
    if task is not None and not task.done():
        return

    app.state.stock_index_refresh_task = asyncio.create_task(
        _refresh_stock_index_cache_in_background(reason)
    )


def _load_runtime_scheduler_args() -> dict:
    raw_value = os.getenv(RUNTIME_SCHEDULER_ARGS_ENV)
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        logger.warning("Invalid %s payload; runtime scheduler uses default args", RUNTIME_SCHEDULER_ARGS_ENV)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("%s payload is not an object; runtime scheduler uses default args", RUNTIME_SCHEDULER_ARGS_ENV)
        return {}
    return parsed


async def _ths_export_auto_sync_loop_runner(app: FastAPI) -> None:
    """在应用生命周期内运行导出自动检测循环（协程入口）。"""
    import asyncio

    while True:
        try:
            from src.services.ths_sync.ths_sync_service import ThsSyncService

            svc = ThsSyncService()
            config = svc.get_export_config()
            if config.get("auto_sync"):
                check = svc.should_sync_export()
                if check.get("detected") and check.get("changed") and check.get("latest"):
                    try:
                        svc.import_export_file(check["latest"])
                        svc.record_export_synced(check["latest"])
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.sleep(600)
        except asyncio.CancelledError:
            break


async def _portfolio_close_snapshot_loop_runner(app: FastAPI) -> None:
    """每个交易日收盘后自动生成一份「当日收盘价」持仓快照（协程入口）。

    - 每个交易日 15:10 之后检查一次：当天尚未生成收盘快照则执行一次全账户快照；
    - 快照走 get_portfolio_snapshot，在非盘中时段会自动重拉当日收盘价
      （配合价格缓存跨阶段失效修复，不再复用盘中旧价）；
    - 周末 / 节假日 / 非收盘时段不动作，避免无效写入。
    """
    import asyncio

    last_generated_date = None
    while True:
        try:
            now = datetime.now()
            # 工作日（周一~周五）且已过收盘整理时段
            if now.weekday() < 5 and now.hour >= 15 and now.minute >= 10:
                today = now.date()
                if last_generated_date != today:
                    from src.services.portfolio_service import PortfolioService
                    from src.repositories.portfolio_repo import PortfolioRepository

                    svc = PortfolioService(repo=PortfolioRepository())
                    accounts = svc.list_accounts()
                    generated = 0
                    for acc in accounts:
                        try:
                            svc.get_portfolio_snapshot(account_id=acc["id"], cost_method="fifo")
                            generated += 1
                        except Exception:  # noqa: BLE001 - 单账户失败不阻断其他账户
                            logger.exception("close snapshot failed for account %s", acc.get("id"))
                    if generated:
                        logger.info("[PortfolioCloseSnapshot] 已生成 %d 个账户的收盘快照 (%s)", generated, today)
                    # 资产曲线自愈：收盘后用账本资产曲线补齐缺失/待更新的日快照点
                    await run_in_threadpool(_backfill_asset_curve_from_ths)
                    last_generated_date = today
        except Exception:  # noqa: BLE001 - 后台任务永不崩溃
            pass
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            break


def _backfill_asset_curve_from_ths() -> int:
    """从同花顺账本拉取资产曲线，补齐/更新本地日快照（幂等，缺哪补哪）。

    - 数据源：账本 /pc/asset/v1/asset_trend（约近 24 个交易日，含当日）；
    - 场景：服务启动、每日收盘快照生成后自动调用，保证资产曲线交易日连续；
    - 未登录账本或拉取失败时静默跳过，不影响主流程。
    """
    try:
        from src.services.ths_sync.ths_sync_service import (
            DEFAULT_ACCOUNT_NAME,
            ThsSyncService,
        )

        svc = ThsSyncService()
        if not svc.client.is_logged_in():
            logger.info("[AssetCurveBackfill] 同花顺账本未登录，跳过资产曲线补齐")
            return 0
        account = svc._find_or_create_account(DEFAULT_ACCOUNT_NAME)
        points = svc.fetch_asset_trend_all()
        if not points:
            return 0
        written = svc._import_asset_trend(account["id"], points)
        logger.info("[AssetCurveBackfill] 已从账本补齐 %d 个资产曲线快照点", written)
        return written
    except Exception:  # noqa: BLE001 - 自愈任务失败不影响主流程
        logger.exception("[AssetCurveBackfill] 资产曲线补齐失败")
        return 0


async def _startup_asset_curve_backfill_runner() -> None:
    """服务启动后延迟执行一次账本资产曲线补齐（不阻塞启动）。"""
    import asyncio

    await asyncio.sleep(5)
    await run_in_threadpool(_backfill_asset_curve_from_ths)


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """Initialize and release shared services for the app lifecycle."""
    runtime_owns_schedule = os.getenv(CLI_SCHEDULER_OWNER_ENV, "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }
    runtime_force_enabled = os.getenv(RUNTIME_SCHEDULER_FORCE_ENABLED_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    runtime_suppress_start = os.getenv(RUNTIME_SCHEDULER_SUPPRESS_START_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    runtime_run_immediately_override = os.getenv(RUNTIME_SCHEDULER_RUN_IMMEDIATELY_ENV)
    if runtime_suppress_start or not runtime_owns_schedule:
        runtime_run_immediately = False
    elif runtime_run_immediately_override is None:
        from src.config import get_config

        runtime_run_immediately = bool(getattr(get_config(), "schedule_run_immediately", False))
    else:
        runtime_run_immediately = runtime_run_immediately_override.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    runtime_scheduler_args = _load_runtime_scheduler_args()
    os.environ.pop(RUNTIME_SCHEDULER_FORCE_ENABLED_ENV, None)
    os.environ.pop(RUNTIME_SCHEDULER_RUN_IMMEDIATELY_ENV, None)
    os.environ.pop(RUNTIME_SCHEDULER_SUPPRESS_START_ENV, None)
    os.environ.pop(RUNTIME_SCHEDULER_ARGS_ENV, None)
    runtime_scheduler_service = RuntimeSchedulerService(
        owns_schedule=runtime_owns_schedule,
        force_enabled=runtime_force_enabled,
        run_immediately_in_background=True,
        schedule_args_overrides=runtime_scheduler_args,
    )
    app.state.runtime_scheduler_service = runtime_scheduler_service
    if not runtime_suppress_start:
        app.state.runtime_scheduler_service.reconcile_from_config(
            run_immediately=runtime_run_immediately,
        )
    app.state.system_config_service = SystemConfigService(
        runtime_scheduler=app.state.runtime_scheduler_service,
    )
    _schedule_stock_index_background_refresh(app, "startup")
    # 名称解析器的 AkShare 缓存预热：命中磁盘缓存则零网络加载，否则发起
    # 后台单飞拉取。把冷启动等待从首个用户请求挪到进程启动窗口。
    from src.services.name_to_code_resolver import warmup_akshare_cache

    warmup_akshare_cache()

    # 账本导出文件自动检测同步（方案B）：开启 auto_sync 时每 10 分钟扫描下载目录，
    # 发现新的「汇总持仓.xlsx」即自动导入本地账户。
    ths_export_task = asyncio.create_task(_ths_export_auto_sync_loop_runner(app))
    app.state.ths_export_auto_sync_task = ths_export_task
    # 收盘定时快照：每个交易日收盘后自动生成一份当日收盘价持仓快照，
    # 保证历史资产曲线每天都有完整的收盘数据（无需用户手动打开页面触发）。
    portfolio_close_snapshot_task = asyncio.create_task(_portfolio_close_snapshot_loop_runner(app))
    app.state.portfolio_close_snapshot_task = portfolio_close_snapshot_task
    # 资产曲线自愈：启动后补齐账本资产曲线缺失的交易日快照（服务重启 / 长时间停机后
    # 自动补录，不阻塞启动）。
    asset_curve_backfill_task = asyncio.create_task(_startup_asset_curve_backfill_runner())
    app.state.asset_curve_backfill_task = asset_curve_backfill_task
    try:
        yield
    finally:
        ths_export_task.cancel()
        with suppress(asyncio.CancelledError):
            await ths_export_task
        portfolio_close_snapshot_task.cancel()
        with suppress(asyncio.CancelledError):
            await portfolio_close_snapshot_task
        asset_curve_backfill_task = getattr(app.state, "asset_curve_backfill_task", None)
        if asset_curve_backfill_task is not None and not asset_curve_backfill_task.done():
            asset_curve_backfill_task.cancel()
            with suppress(asyncio.CancelledError):
                await asset_curve_backfill_task
        refresh_task = getattr(app.state, "stock_index_refresh_task", None)
        if refresh_task is not None and not refresh_task.done():
            refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await refresh_task
        if hasattr(app.state, "system_config_service"):
            delattr(app.state, "system_config_service")
        runtime_scheduler = getattr(app.state, "runtime_scheduler_service", None)
        if runtime_scheduler is not None:
            runtime_scheduler.stop()
            delattr(app.state, "runtime_scheduler_service")


def create_app(static_dir: Optional[Path] = None) -> FastAPI:
    """
    创建并配置 FastAPI 应用实例
    
    Args:
        static_dir: 静态文件目录路径（可选，默认为项目根目录下的 static）
        
    Returns:
        配置完成的 FastAPI 应用实例
    """
    # 默认静态文件目录
    _register_frontend_asset_mime_types()

    if static_dir is None:
        static_dir = Path(__file__).parent.parent / "static"
    
    # 创建 FastAPI 实例
    app = FastAPI(
        title="Daily Stock Analysis API",
        description=(
            "A股/港股/美股自选股智能分析系统 API\n\n"
            "## 功能模块\n"
            "- 股票分析：触发 AI 智能分析\n"
            "- 历史记录：查询历史分析报告\n"
            "- 股票数据：获取行情数据\n\n"
            "## 认证方式\n"
            "支持可选管理员认证：ADMIN_AUTH_ENABLED=true 时，除登录、状态、健康检查和 "
            "OpenAPI 文档外，/api/v1/* 需要有效管理员会话 Cookie；关闭时不强制认证。"
        ),
        version="1.0.0",
        lifespan=app_lifespan,
    )
    
    # ============================================================
    # CORS 配置
    # ============================================================
    
    allowed_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    
    # 从环境变量添加额外的允许来源
    extra_origins = os.environ.get("CORS_ORIGINS", "")
    if extra_origins:
        allowed_origins.extend([o.strip() for o in extra_origins.split(",") if o.strip()])
    
    # 允许所有来源（开发/演示用）
    allow_all_origins = os.environ.get("CORS_ALLOW_ALL", "").lower() == "true"
    allow_credentials = not allow_all_origins
    if allow_all_origins:
        _warn_if_open_cors_without_auth()
        allowed_origins = ["*"]
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    add_auth_middleware(app)
    
    # ============================================================
    # 注册路由
    # ============================================================
    
    app.include_router(api_v1_router, prefix="/api/v1")
    add_error_handlers(app)
    
    # ============================================================
    # 根路由和健康检查
    # ============================================================
    
    has_frontend = static_dir.exists() and (static_dir / "index.html").exists()
    
    if has_frontend:
        # Surface bundle inconsistencies as soon as the app starts so that
        # blank-page reports (#1064 / #1065 / #1050) can be diagnosed from
        # logs/desktop.log instead of via browser devtools.
        _check_frontend_assets_consistency(static_dir)

        @app.get("/", include_in_schema=False)
        async def root():
            """根路由 - 返回前端页面"""
            return _frontend_index_response(static_dir)
    else:
        _FRONTEND_NOT_BUILT_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEA - Frontend Not Built</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0a0e17;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,monospace}
  .card{max-width:580px;padding:2.5rem;border:1px solid #1e293b;border-radius:12px;background:#111827}
  h1{font-size:1.25rem;color:#38bdf8;margin-bottom:.75rem}
  p{font-size:.9rem;line-height:1.7;color:#94a3b8;margin-bottom:.5rem}
  code{background:#1e293b;padding:2px 8px;border-radius:4px;font-size:.85rem;color:#67e8f9}
  .hint{margin-top:1.25rem;padding:.75rem 1rem;border-left:3px solid #f59e0b;background:#1c1917;border-radius:0 6px 6px 0}
  .hint p{color:#fbbf24;margin:0}
  a{color:#38bdf8;text-decoration:none}
  a:hover{text-decoration:underline}
  .status{margin-top:1rem;font-size:.8rem;color:#475569}
</style></head><body><div class="card">
<h1>&#9888;&#65039; Frontend Not Built</h1>
<p>API is running, but the Web UI has not been built yet.</p>
<p>Build the frontend first:</p>
<p><code>cd apps/dsa-web &amp;&amp; npm install &amp;&amp; npm run build</code></p>
<p>Or start with auto-build:</p>
<p><code>python main.py --serve-only</code></p>
<div class="hint"><p>If you only need the API, visit <a href="/docs">/docs</a> for the interactive API documentation.</p></div>
<p class="status">API Version 1.0.0 &bull; <a href="/api/health">/api/health</a></p>
</div></body></html>"""

        @app.get("/", include_in_schema=False)
        async def root():
            """根路由 - 前端未构建时返回引导页面"""
            return HTMLResponse(content=_FRONTEND_NOT_BUILT_HTML)
    
    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["Health"],
        summary="健康检查",
        description="用于负载均衡器或监控系统检查服务状态"
    )
    @app.get(
        "/api/health",
        response_model=HealthResponse,
        tags=["Health"],
        summary="健康检查",
        description="用于负载均衡器或监控系统检查服务状态"
    )
    async def health_check() -> HealthResponse:
        """健康检查接口"""
        return HealthResponse(
            status="ok",
            timestamp=datetime.now().isoformat()
        )

    def _stock_index_candidate_paths() -> tuple[Path, ...]:
        local_candidates = (
            static_dir / _STOCK_INDEX_FILENAME,
            _bundled_stock_index_path(),
        )
        local_path = next((path for path in local_candidates if path.is_file()), None)
        if local_path is None:
            return (get_remote_stock_index_cache_path(),)
        return (
            get_remote_stock_index_cache_path(),
            local_path,
        )

    def _find_existing_stock_index_path() -> Optional[Path]:
        remote_cache_path = get_remote_stock_index_cache_path()
        return find_existing_stock_index_path(
            _stock_index_candidate_paths(),
            remote_cache_path=remote_cache_path,
        )

    @app.api_route(
        f"/{_STOCK_INDEX_FILENAME}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def serve_stock_index():
        """Serve the freshest available stock autocomplete index."""
        _schedule_stock_index_background_refresh(app, "serve-stock-index")

        index_path = _find_existing_stock_index_path()
        if index_path is None:
            return Response(
                content="stock index not found",
                status_code=404,
                media_type="text/plain",
            )
        return FileResponse(
            index_path,
            media_type="application/json",
            headers=_STOCK_INDEX_HEADERS,
        )
    
    # ============================================================
    # 静态文件托管（前端 SPA）
    # ============================================================
    
    if has_frontend:
        # Serve `/assets/*` explicitly so that misses return a plain-text
        # 404 with the correct Content-Type instead of the default JSON
        # error response. JSON for a JS/CSS request is what masked the
        # blank-page root cause in #1064; here we make it obvious that the
        # static file simply does not exist on disk.
        assets_dir = static_dir / "assets"

        assets_static_files = StaticFiles(directory=str(assets_dir), check_dir=False)
        assets_root = assets_dir.resolve()

        @app.api_route(
            "/assets/{asset_path:path}",
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )
        async def serve_asset(request: Request, asset_path: str):
            file_path = _resolve_asset_path(assets_dir, asset_path)
            if file_path is None:
                return Response(
                    content="not found",
                    status_code=404,
                    media_type="text/plain",
                )
            if file_path.is_file():
                relative_path = file_path.relative_to(assets_root).as_posix()
                return await assets_static_files.get_response(relative_path, request.scope)
            return Response(
                content="asset not found",
                status_code=404,
                media_type=_missing_asset_media_type(asset_path),
            )

        # SPA 路由回退
        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(request: Request, full_path: str):
            """SPA 路由回退 - 非 API 路由返回 index.html"""
            if full_path == "api" or full_path.startswith("api/"):
                return JSONResponse(
                    status_code=404,
                    content={"error": "not_found", "message": f"API endpoint /{full_path} not found"}
                )

            # Reuse the same containment check as /assets/* so that requests
            # like `/%2e%2e/%2e%2e/etc/passwd` cannot escape static_dir via
            # the SPA fallback. Starlette's :path converter does not collapse
            # `..` segments, so static_dir / full_path can resolve outside
            # the bundle root if served unchecked.
            file_path = _resolve_asset_path(static_dir, full_path) if full_path else None
            if file_path is not None and file_path.is_file():
                if file_path == (static_dir / "index.html").resolve():
                    return _frontend_index_response(static_dir)
                # Issue #520: Explicitly resolve MIME type to avoid
                # browsers rejecting JS modules served as text/plain.
                content_type = _frontend_asset_media_type(str(file_path))
                return FileResponse(file_path, media_type=content_type)

            return _frontend_index_response(static_dir)
    
    return app


# 默认应用实例（供 uvicorn 直接使用）
app = create_app()

import sqlite3
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import DOTENV_PATH, PROJECT_ROOT, learning_data_directory
from app.routes.api import router as api_router
from app.routes.error_pages import configure_error_pages, error_response
from app.routes.extended_api import router as extended_api_router
from app.routes.free_api import router as free_api_router
from app.routes.model_api import router as model_router
from app.routes.pages import configure_pages
from app.security import LocalRequestGuard
from app.services.bootstrap import register_initial_sources
from app.services.free_batches import interrupt_batches
from app.services.interview import InterviewError
from app.services.llm_config import normalize_model_settings
from app.services.practice import PracticeError
from app.services.reflections import ReflectionError
from app.services.review import ReviewError
from app.services.runtime_state import RuntimeVersionGuard
from app.services.tasks import IdempotencyConflictError
from app.storage.database import connect_database, initialize_database

TEMPLATES = Jinja2Templates(directory=PROJECT_ROOT / "app" / "templates")


def asset_url(name):
    version = (PROJECT_ROOT / 'app' / 'static' / name).stat().st_mtime_ns
    return f'/static/{name}?v={version}'


TEMPLATES.env.globals['asset_url'] = asset_url


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the local database before serving requests."""
    load_dotenv(DOTENV_PATH, override=False)
    data_directory = learning_data_directory()
    data_directory.mkdir(parents=True, exist_ok=True)
    database_path = data_directory / "learning.db"
    connection = connect_database(database_path)
    initialize_database(connection)
    normalize_model_settings(connection)
    register_initial_sources(connection)
    interrupt_batches(connection)
    # Local single-process deployment: a prior process cannot still own these runs.
    connection.execute("UPDATE alignment_run SET status='recoverable',finished_at=NULL,"
                       "error='服务重启中断了本轮对齐，请按原请求继续' WHERE status IN ('queued','running')")
    connection.execute("UPDATE sync_run SET status='interrupted',finished_at=datetime('now'),"
                       "error='服务重启中断了读取' WHERE status='running'")
    connection.commit()
    connection.close()
    app.state.database_path = database_path
    yield


app = FastAPI(title="Learning Practice", lifespan=lifespan)


@app.exception_handler(sqlite3.OperationalError)
async def database_error(request, error):
    if "locked" in str(error).lower() or "busy" in str(error).lower():
        return error_response(request, 503, "题库正在保存，请稍后重试。已有回答和任务会保留。")
    return error_response(request, 500, "数据库操作失败，请查看服务日志。")


@app.exception_handler(Exception)
async def unexpected_error(request, error):
    return error_response(request, 500, "服务暂时出错，请重试；若仍失败，请反馈发生问题的操作。")


async def domain_error(request, error):
    return error_response(request, 409, str(error))


for error_type in (PracticeError, ReviewError, IdempotencyConflictError, InterviewError, ReflectionError):
    app.add_exception_handler(error_type, domain_error)
app.add_middleware(LocalRequestGuard)
app.add_middleware(RuntimeVersionGuard, source_root=PROJECT_ROOT / 'app')
configure_error_pages(app)
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "app" / "static"), name="static")
app.include_router(api_router)
app.include_router(model_router)
app.include_router(extended_api_router)
app.include_router(free_api_router)
app.include_router(configure_pages(TEMPLATES))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "phase": "P0-P6 foundation"}

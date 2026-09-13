"""Keep browser failures navigable while preserving the API error contract."""

from pathlib import Path

from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from jinja2 import Environment
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, JSONResponse

# Freeze this self-contained fallback with the process; it must survive template drift.
ERROR_TEMPLATE = Environment(autoescape=True).from_string(
    (Path(__file__).resolve().parents[1] / 'templates/error.html').read_text(encoding='utf-8'))


def is_page(request):
    return not request.url.path.startswith(('/api/', '/static/')) and request.url.path != '/health'


def error_response(request, status, message, title='这一页暂时无法打开', **extra):
    if is_page(request):
        return HTMLResponse(ERROR_TEMPLATE.render(status=status, title=title, message=message),
                            status_code=status, headers={'Cache-Control': 'no-store'})
    return JSONResponse({'detail': message, **extra}, status_code=status, headers={'Cache-Control': 'no-store'})


def configure_error_pages(app):
    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        if not is_page(request):
            return await http_exception_handler(request, error)
        messages = {404: ('没有找到这一页', '链接可能已失效或地址不正确。返回今天，也可以从历史记录重新找到这道题。'),
                    405: ('此操作暂不可用', '请返回页面，使用页面上的操作按钮。')}
        title, message = messages.get(error.status_code, ('这一页暂时无法打开', '请返回今天后重试，已有学习记录仍保留。'))
        return error_response(request, error.status_code, message, title)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        if not is_page(request):
            return await request_validation_exception_handler(request, error)
        return error_response(request, 422, '日期、页码或筛选条件不正确。请从今天或复习库重新进入。', '页面地址有误')

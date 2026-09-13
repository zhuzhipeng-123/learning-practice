"""Do not serve mixed Python, template and JavaScript versions after an edit."""

from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware

from app.routes.error_pages import error_response


def source_signature(root):
    return tuple(sorted((path.relative_to(root).as_posix(), path.stat().st_mtime_ns, path.stat().st_size)
                        for path in root.rglob('*') if path.suffix in {'.py', '.html', '.js', '.css'} and path.is_file()))


class RuntimeVersionGuard(BaseHTTPMiddleware):
    def __init__(self, app, source_root):
        super().__init__(app)
        self.source_root = Path(source_root)
        self.signature = source_signature(self.source_root)

    async def dispatch(self, request, call_next):
        try:
            changed = source_signature(self.source_root) != self.signature
        except OSError:
            changed = True
        if changed:
            return error_response(request, 503,
                '程序文件已更新，需要重启本地服务后继续。此次操作尚未执行；已有学习记录保留，当前页面中的输入请先另存。',
                '服务需要重新启动', request_state='failed', restart_required=True)
        return await call_next(request)

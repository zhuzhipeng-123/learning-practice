from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "testserver"}


class LocalRequestGuard(BaseHTTPMiddleware):
    """Reject cross-origin writes and unexpected Host headers."""

    async def dispatch(self, request: Request, call_next) -> Response:
        host = request.url.hostname
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "untrusted host"}, status_code=400)
        if request.method not in SAFE_METHODS and not self._same_origin(request):
            return JSONResponse({"detail": "cross-origin write rejected"}, status_code=403)
        return await call_next(request)

    def _same_origin(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            return parsed.hostname == request.url.hostname and parsed.port == request.url.port
        return request.headers.get("x-requested-with") == "learning-practice"

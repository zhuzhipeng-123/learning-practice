from fastapi.responses import JSONResponse


def model_error_response(error, failure_status=502):
    """Tell clients whether the same request must be retained for recovery."""
    return JSONResponse(status_code=429 if error.retry_at else failure_status, content={
        'detail': str(error),
        'request_state': 'pending' if error.in_progress or error.retry_at else 'failed',
    })

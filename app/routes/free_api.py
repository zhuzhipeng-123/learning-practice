from typing import Annotated

from fastapi import APIRouter, Header, HTTPException

from app.routes.api import Database, FreePracticeRequest
from app.services.free_batches import batch_state, practice_state, queue_batch, retry_batch

router = APIRouter(prefix='/api/free-practice')


@router.post('/batches')
def create_batch(body: FreePracticeRequest, database: Database,
                 request_key: Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=200)]):
    try:
        return queue_batch(database, body.model_dump(), request_key)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post('/restore')
def restore(database: Database):
    from app.services.free_batches import restore_free
    return restore_free(database)


@router.get('/state')
def read_state(database: Database):
    return practice_state(database)


@router.get('/batches/{batch_id}')
def read_batch(batch_id: str, database: Database):
    try:
        return batch_state(database, batch_id)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error


@router.post('/batches/{batch_id}/retry')
def retry(batch_id: str, database: Database):
    try:
        return retry_batch(database, batch_id)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error

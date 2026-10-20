import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.api import router
from app.db import verify_database_connection

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def mqtt_enabled() -> bool:
    return os.getenv("MQTT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    verify_database_connection()
    logger.info("Database connection verified")

    consumer = None
    if mqtt_enabled():
        from app.mqtt_consumer import TelemetryConsumer

        consumer = TelemetryConsumer()
        consumer.start()

    try:
        yield
    finally:
        if consumer is not None:
            consumer.stop()


app = FastAPI(
    title="Hydroponic Monitoring Backend",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    try:
        verify_database_connection()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Database connection unavailable",
        ) from exc

    return {"status": "ok", "database": "connected"}

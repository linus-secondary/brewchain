"""Things every service shares: start-up, health checks and the pod name header."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

import db


def create_app(title: str, use_db: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        if use_db:
            db.start()
        yield
        if use_db:
            db.stop()

    app = FastAPI(title=title, lifespan=lifespan)

    @app.middleware("http")
    async def add_pod_header(request, call_next):
        response = await call_next(request)
        response.headers["X-Served-By"] = db.POD_NAME   # shows which pod answered
        return response

    @app.get("/health")              # liveness probe: is the process alive?
    def health():
        return {"status": "ok", "service": title, "pod": db.POD_NAME}

    @app.get("/ready")               # readiness probe: can it reach the database?
    def ready():
        if use_db:
            try:
                with db.pool.connection(timeout=3) as conn:
                    conn.execute("SELECT 1")
            except Exception:
                raise HTTPException(503, "database not reachable")
        return {"status": "ready"}

    return app

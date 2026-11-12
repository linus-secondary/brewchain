"""gateway: serves the web pages and forwards /api/... calls to the right service."""
import os
from pathlib import Path

import httpx
from fastapi import HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles

from common import create_app

app = create_app("gateway", use_db=False)

ROUTES = {
    "menu": os.environ.get("MENU_URL", "http://menu-inventory:8000"),
    "orders": os.environ.get("ORDERS_URL", "http://orders:8000"),
    "loyalty": os.environ.get("LOYALTY_URL", "http://loyalty:8000"),
}
# Only services may call these, never a customer's browser
INTERNAL = {("menu", "reserve"), ("menu", "release"),
            ("loyalty", "redeem"), ("loyalty", "earn"), ("loyalty", "reverse")}

client = httpx.AsyncClient(timeout=10, limits=httpx.Limits(max_connections=200, max_keepalive_connections=50))


@app.api_route("/api/{service}/{path:path}", methods=["GET", "POST", "PATCH"])
async def forward(service: str, path: str, request: Request):
    base = ROUTES.get(service)
    if base is None:
        raise HTTPException(404, "Unknown service")
    if (service, path.split("/")[0]) in INTERNAL:
        raise HTTPException(403, "Internal endpoint")
    try:
        upstream = await client.request(
            request.method,
            f"{base}/{path}",
            params=request.query_params,
            content=await request.body(),
            headers={"content-type": request.headers.get("content-type", "application/json")},
        )
    except httpx.RequestError:
        raise HTTPException(503, f"The {service} service is not reachable right now")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
        headers={"X-Upstream-Pod": upstream.headers.get("X-Served-By", "")},
    )


app.mount("/", StaticFiles(directory=Path(__file__).with_name("static"), html=True), name="static")

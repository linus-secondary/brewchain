"""orders service: preorders and walk-in orders, the queue at each shop, and stats.

Placing an order is a small "saga" across services:
  1. loyalty        -> find or create the customer, get their tier discount
  2. menu-inventory -> reserve stock (all items or nothing)
  3. orders DB      -> save the order and work out the queue / ready time
  4. loyalty        -> redeem points (if asked)
  5. loyalty        -> earn points
If step 3 or 4 fails, the earlier steps are undone (stock released, order cancelled).
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field

import db
from common import create_app

app = create_app("orders")

MENU_URL = os.environ.get("MENU_URL", "http://menu-inventory:8000")
LOYALTY_URL = os.environ.get("LOYALTY_URL", "http://loyalty:8000")
http = httpx.Client(timeout=8)

ACTIVE = ("RECEIVED", "PREPARING")
NEXT_STATUS = {
    "RECEIVED": {"PREPARING", "CANCELLED"},
    "PREPARING": {"READY", "CANCELLED"},
    "READY": {"COLLECTED"},
}


class Line(BaseModel):
    item_id: int
    quantity: int = Field(gt=0, le=20)


class OrderIn(BaseModel):
    shop_id: int
    phone: str
    name: str
    dietary_notes: str = "None"
    items: list[Line] = Field(min_length=1, max_length=10)
    order_type: Literal["PREORDER", "WALK_IN"] = "PREORDER"
    pickup_at: datetime | None = None
    redeem_points: int = Field(default=0, ge=0)


class StatusIn(BaseModel):
    status: Literal["PREPARING", "READY", "COLLECTED", "CANCELLED"]


def call(url: str, payload: dict) -> dict:
    """POST to another service and turn its errors into clear messages."""
    try:
        resp = http.post(url, json=payload)
    except httpx.RequestError:
        raise HTTPException(503, f"A service is not reachable: {url.split('/')[2]}")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise HTTPException(resp.status_code if resp.status_code < 500 else 502, detail)
    return resp.json()


@app.post("/orders", status_code=201)
def place_order(o: OrderIn):
    # 1. customer + membership
    customer = call(f"{LOYALTY_URL}/customers",
                    {"phone": o.phone, "name": o.name, "dietary_notes": o.dietary_notes})

    # 2. get an order number first, then reserve stock against it
    with db.pool.connection() as conn:
        order_id = conn.execute(
            "SELECT nextval(pg_get_serial_sequence('orders', 'order_id')) AS id"
        ).fetchone()["id"]
    reserved = call(f"{MENU_URL}/reserve",
                    {"shop_id": o.shop_id, "order_id": order_id,
                     "items": [line.model_dump() for line in o.items]})
    lines = reserved["lines"]

    saved = False
    try:
        # prices and discounts
        subtotal = round(sum(float(l["price"]) * l["quantity"] for l in lines), 2)
        discount = round(subtotal * customer["discount_pct"] / 100, 2)
        after_discount = subtotal - discount
        redeem = min(o.redeem_points, customer["points"], int(after_discount * 0.5))  # max 50% of bill
        total = round(after_discount - redeem, 2)
        drinks = sum(l["quantity"] for l in lines)
        prep_minutes = max(l["prep_minutes"] for l in lines) + (drinks - 1)

        now = datetime.now(timezone.utc)
        pickup_at = o.pickup_at
        if pickup_at is not None:
            if pickup_at.tzinfo is None:
                pickup_at = pickup_at.replace(tzinfo=timezone.utc)
            if pickup_at < now or pickup_at > now + timedelta(hours=12):
                pickup_at = None

        # 3. save the order. Lock the shop row so two orders at the same shop
        #    cannot get the same place in the queue.
        with db.pool.connection() as conn:
            with conn.transaction():
                conn.execute("SELECT shop_id FROM shops WHERE shop_id = %s FOR UPDATE", (o.shop_id,))
                queue = conn.execute(
                    """SELECT COUNT(*) AS ahead, MAX(ready_at) AS last_ready FROM orders
                       WHERE shop_id = %s AND status IN ('RECEIVED', 'PREPARING')""",
                    (o.shop_id,),
                ).fetchone()
                start = max(now, queue["last_ready"] or now)
                ready_at = start + timedelta(minutes=prep_minutes)
                if pickup_at and pickup_at > ready_at:
                    ready_at = pickup_at
                conn.execute(
                    """INSERT INTO orders (order_id, shop_id, customer_id, order_type, subtotal, discount,
                                           points_redeemed, total, pickup_at, ready_at, served_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (order_id, o.shop_id, customer["customer_id"], o.order_type, subtotal, discount,
                     redeem, total, pickup_at, ready_at, db.POD_NAME),
                )
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO order_items (order_id, item_id, quantity, unit_price) VALUES (%s, %s, %s, %s)",
                        [(order_id, l["item_id"], l["quantity"], l["price"]) for l in lines],
                    )
        saved = True

        # 4. redeem points
        if redeem > 0:
            call(f"{LOYALTY_URL}/redeem",
                 {"customer_id": customer["customer_id"], "order_id": order_id, "points": redeem})
    except Exception:
        # undo the earlier steps (compensation)
        try:
            http.post(f"{MENU_URL}/release", json={"order_id": order_id})
            if saved:
                with db.pool.connection() as conn:
                    conn.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = %s", (order_id,))
        except Exception:
            pass
        raise

    # 5. earn points (if loyalty is briefly down the order still goes through)
    earned = {"points_earned": 0, "upgraded": False, "tier": customer["tier"],
              "points": customer["points"] - redeem}
    try:
        earned = call(f"{LOYALTY_URL}/earn",
                      {"customer_id": customer["customer_id"], "order_id": order_id, "amount": total})
        with db.pool.connection() as conn:
            conn.execute("UPDATE orders SET points_earned = %s WHERE order_id = %s",
                         (earned["points_earned"], order_id))
    except HTTPException:
        pass

    return {
        "order_id": order_id,
        "shop_id": o.shop_id,
        "status": "RECEIVED",
        "order_type": o.order_type,
        "queue_position": queue["ahead"] + 1,
        "ready_at": ready_at,
        "minutes_until_ready": max(1, round((ready_at - now).total_seconds() / 60)),
        "items": [{"name": l["name"], "quantity": l["quantity"], "price": l["price"]} for l in lines],
        "subtotal": subtotal,
        "tier_discount_pct": customer["discount_pct"],
        "discount": discount,
        "points_redeemed": redeem,
        "total": total,
        "points_earned": earned["points_earned"],
        "points_balance": earned["points"],
        "tier": earned["tier"],
        "tier_upgraded": earned["upgraded"],
        "dietary_notes": customer["dietary_notes"],
        "served_by": db.POD_NAME,
    }


@app.get("/orders/{order_id}")
def get_order(order_id: int):
    with db.pool.connection() as conn:
        order = conn.execute(
            """SELECT o.*, s.name AS shop, c.name AS customer, c.dietary_notes
               FROM orders o JOIN shops s ON s.shop_id = o.shop_id
               JOIN customers c ON c.customer_id = o.customer_id
               WHERE o.order_id = %s""",
            (order_id,),
        ).fetchone()
        if order is None:
            raise HTTPException(404, "Order not found")
        order["items"] = conn.execute(
            """SELECT m.name, oi.quantity, oi.unit_price FROM order_items oi
               JOIN menu_items m ON m.item_id = oi.item_id WHERE oi.order_id = %s""",
            (order_id,),
        ).fetchall()
        order["queue_position"] = None
        if order["status"] in ACTIVE:
            order["queue_position"] = conn.execute(
                """SELECT COUNT(*) AS n FROM orders WHERE shop_id = %s
                   AND status IN ('RECEIVED', 'PREPARING') AND order_id <= %s""",
                (order["shop_id"], order_id),
            ).fetchone()["n"]
    order["pod"] = db.POD_NAME
    return order


@app.get("/orders")
def list_orders(shop_id: int, active: bool = True, limit: int = 50):
    statuses = ["RECEIVED", "PREPARING", "READY"] if active else \
               ["RECEIVED", "PREPARING", "READY", "COLLECTED", "CANCELLED"]
    with db.pool.connection() as conn:
        rows = conn.execute(
            """SELECT o.order_id, o.order_type, o.status, o.total, o.ready_at, o.pickup_at,
                      o.created_at, c.name AS customer, c.dietary_notes,
                      STRING_AGG(m.name || ' x' || oi.quantity, ', ' ORDER BY m.name) AS items
               FROM orders o
               JOIN customers c   ON c.customer_id = o.customer_id
               JOIN order_items oi ON oi.order_id = o.order_id
               JOIN menu_items m  ON m.item_id = oi.item_id
               WHERE o.shop_id = %s AND o.status = ANY(%s)
               GROUP BY o.order_id, c.name, c.dietary_notes
               ORDER BY o.ready_at
               LIMIT %s""",
            (shop_id, statuses, min(limit, 200)),
        ).fetchall()
    return {"pod": db.POD_NAME, "orders": rows}


@app.patch("/orders/{order_id}/status")
def update_status(order_id: int, s: StatusIn):
    with db.pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT status FROM orders WHERE order_id = %s FOR UPDATE", (order_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Order not found")
            if s.status not in NEXT_STATUS.get(row["status"], set()):
                raise HTTPException(409, f"Cannot change {row['status']} to {s.status}")
            conn.execute("UPDATE orders SET status = %s WHERE order_id = %s", (s.status, order_id))
    if s.status == "CANCELLED":           # give back stock and points
        call(f"{MENU_URL}/release", {"order_id": order_id})
        call(f"{LOYALTY_URL}/reverse", {"order_id": order_id})
    return {"order_id": order_id, "status": s.status}


@app.get("/stats")
def stats():
    """Numbers for the chain dashboard."""
    with db.pool.connection() as conn:
        shops = conn.execute("SELECT * FROM shop_sales_today ORDER BY shop_id").fetchall()
        pods = conn.execute(
            """SELECT served_by AS pod, COUNT(*) AS orders FROM orders
               WHERE created_at > now() - interval '2 minutes'
               GROUP BY served_by ORDER BY orders DESC"""
        ).fetchall()
        per_minute = conn.execute(
            """SELECT to_char(date_trunc('minute', created_at) AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') AS minute,
                      COUNT(*) AS orders
               FROM orders WHERE created_at > now() - interval '10 minutes'
               GROUP BY 1 ORDER BY 1"""
        ).fetchall()
        low = conn.execute(
            "SELECT COUNT(*) AS n FROM shop_inventory WHERE quantity < reorder_level"
        ).fetchone()["n"]
        members = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"]
    return {
        "pod": db.POD_NAME,
        "shops": shops,
        "orders_today": sum(s["orders"] for s in shops),
        "revenue_today": sum(float(s["revenue"]) for s in shops),
        "in_queue": sum(s["in_queue"] for s in shops),
        "low_stock_items": low,
        "members": members,
        "order_pods_last_2_min": pods,
        "orders_per_minute": per_minute,
    }

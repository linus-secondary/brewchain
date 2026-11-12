"""menu-inventory service: shops, menu and stock at every shop."""
from fastapi import HTTPException
from pydantic import BaseModel, Field

import db
from common import create_app

app = create_app("menu-inventory")

DEMO_STOCK = 1000


class Line(BaseModel):
    item_id: int
    quantity: int = Field(gt=0, le=20)


class Reservation(BaseModel):
    shop_id: int
    order_id: int
    items: list[Line] = Field(min_length=1, max_length=10)


class Release(BaseModel):
    order_id: int


class Restock(BaseModel):
    item_id: int
    quantity: int = Field(gt=0, le=10000)


@app.get("/shops")
def list_shops():
    with db.pool.connection() as conn:
        shops = conn.execute(
            "SELECT shop_id, name, area, opens_at, closes_at FROM shops ORDER BY shop_id"
        ).fetchall()
    return {"pod": db.POD_NAME, "shops": shops}


@app.get("/menu")
def menu(shop_id: int):
    with db.pool.connection() as conn:
        items = conn.execute(
            """SELECT m.item_id, m.name, m.category, m.price, m.prep_minutes,
                      i.quantity AS in_stock
               FROM menu_items m
               JOIN shop_inventory i ON i.item_id = m.item_id AND i.shop_id = %s
               ORDER BY m.category, m.name""",
            (shop_id,),
        ).fetchall()
    if not items:
        raise HTTPException(404, "Shop not found")
    return {"pod": db.POD_NAME, "shop_id": shop_id, "items": items}


@app.post("/reserve")
def reserve(r: Reservation):
    """Take stock for an order. All items succeed together or none do."""
    wanted: dict[int, int] = {}
    for line in r.items:
        wanted[line.item_id] = wanted.get(line.item_id, 0) + line.quantity

    with db.pool.connection() as conn:
        with conn.transaction():                       # BEGIN ... COMMIT / ROLLBACK
            for item_id in sorted(wanted):             # fixed order = no deadlocks
                qty = wanted[item_id]
                # Atomic check-and-decrement: safe even with 100 customers at once
                row = conn.execute(
                    """UPDATE shop_inventory
                       SET quantity = quantity - %s, updated_at = now()
                       WHERE shop_id = %s AND item_id = %s AND quantity >= %s
                       RETURNING quantity""",
                    (qty, r.shop_id, item_id, qty),
                ).fetchone()
                if row is None:
                    info = conn.execute(
                        """SELECT m.name, i.quantity FROM menu_items m
                           LEFT JOIN shop_inventory i ON i.item_id = m.item_id AND i.shop_id = %s
                           WHERE m.item_id = %s""",
                        (r.shop_id, item_id),
                    ).fetchone()
                    if info is None or info["quantity"] is None:
                        raise HTTPException(404, f"Item {item_id} is not sold at this shop")
                    raise HTTPException(409, f"Sorry, only {info['quantity']} {info['name']} left at this shop")
                conn.execute(
                    """INSERT INTO stock_movements (shop_id, item_id, change, reason, order_id)
                       VALUES (%s, %s, %s, 'ORDER', %s)""",
                    (r.shop_id, item_id, -qty, r.order_id),
                )
            prices = conn.execute(
                "SELECT item_id, name, price, prep_minutes FROM menu_items WHERE item_id = ANY(%s)",
                (list(wanted),),
            ).fetchall()

    lines = [{**p, "quantity": wanted[p["item_id"]]} for p in prices]
    return {"pod": db.POD_NAME, "order_id": r.order_id, "lines": lines}


@app.post("/release")
def release(r: Release):
    """Give stock back when an order is cancelled (runs at most once per order)."""
    with db.pool.connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(1, %s)", (r.order_id,))
            done = conn.execute(
                "SELECT 1 FROM stock_movements WHERE order_id = %s AND reason = 'CANCEL' LIMIT 1",
                (r.order_id,),
            ).fetchone()
            if done:
                return {"released": 0}
            rows = conn.execute(
                """SELECT shop_id, item_id, -change AS qty FROM stock_movements
                   WHERE order_id = %s AND reason = 'ORDER' ORDER BY item_id""",
                (r.order_id,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """UPDATE shop_inventory SET quantity = quantity + %s, updated_at = now()
                       WHERE shop_id = %s AND item_id = %s""",
                    (row["qty"], row["shop_id"], row["item_id"]),
                )
                conn.execute(
                    """INSERT INTO stock_movements (shop_id, item_id, change, reason, order_id)
                       VALUES (%s, %s, %s, 'CANCEL', %s)""",
                    (row["shop_id"], row["item_id"], row["qty"], r.order_id),
                )
    return {"released": len(rows)}


@app.get("/inventory/low-stock")
def low_stock():
    with db.pool.connection() as conn:
        rows = conn.execute(
            """SELECT s.name AS shop, m.name AS item, i.quantity, i.reorder_level
               FROM shop_inventory i
               JOIN shops s ON s.shop_id = i.shop_id
               JOIN menu_items m ON m.item_id = i.item_id
               WHERE i.quantity < i.reorder_level
               ORDER BY i.quantity, s.name"""
        ).fetchall()
    return {"pod": db.POD_NAME, "items": rows}


@app.get("/inventory/{shop_id}")
def inventory(shop_id: int):
    with db.pool.connection() as conn:
        rows = conn.execute(
            """SELECT m.item_id, m.name, m.category, i.quantity, i.reorder_level,
                      (i.quantity < i.reorder_level) AS low, i.updated_at
               FROM shop_inventory i JOIN menu_items m ON m.item_id = i.item_id
               WHERE i.shop_id = %s ORDER BY m.category, m.name""",
            (shop_id,),
        ).fetchall()
    return {"pod": db.POD_NAME, "shop_id": shop_id, "items": rows}


@app.post("/inventory/{shop_id}/restock")
def restock(shop_id: int, r: Restock):
    with db.pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """UPDATE shop_inventory SET quantity = quantity + %s, updated_at = now()
                   WHERE shop_id = %s AND item_id = %s RETURNING quantity""",
                (r.quantity, shop_id, r.item_id),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Item not found at this shop")
            conn.execute(
                """INSERT INTO stock_movements (shop_id, item_id, change, reason)
                   VALUES (%s, %s, %s, 'RESTOCK')""",
                (shop_id, r.item_id, r.quantity),
            )
    return {"pod": db.POD_NAME, "quantity": row["quantity"]}


@app.post("/inventory/reset-demo")
def reset_demo():
    """Refill every shop to the demo level (button on the admin page)."""
    with db.pool.connection() as conn:
        with conn.transaction():
            rows = conn.execute(
                """SELECT shop_id, item_id, %s - quantity AS diff FROM shop_inventory
                   WHERE quantity < %s ORDER BY shop_id, item_id FOR UPDATE""",
                (DEMO_STOCK, DEMO_STOCK),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE shop_inventory SET quantity = %s, updated_at = now() WHERE shop_id = %s AND item_id = %s",
                    (DEMO_STOCK, row["shop_id"], row["item_id"]),
                )
                conn.execute(
                    "INSERT INTO stock_movements (shop_id, item_id, change, reason) VALUES (%s, %s, %s, 'RESTOCK')",
                    (row["shop_id"], row["item_id"], row["diff"]),
                )
    return {"restocked": len(rows)}

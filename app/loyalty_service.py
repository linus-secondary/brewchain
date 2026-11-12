"""loyalty service: customers, membership tiers and points."""
from fastapi import HTTPException
from pydantic import BaseModel, Field

import db
from common import create_app

app = create_app("loyalty")

WELCOME_BONUS = 50
RUPEES_PER_POINT = 10          # earn 1 point for every Rs. 10 (times the tier multiplier)


class CustomerIn(BaseModel):
    phone: str = Field(min_length=6, max_length=15, pattern=r"^[0-9+ -]+$")
    name: str = Field(min_length=1, max_length=60)
    dietary_notes: str = Field(default="None", max_length=120)


class Redeem(BaseModel):
    customer_id: int
    order_id: int
    points: int = Field(gt=0)


class Earn(BaseModel):
    customer_id: int
    order_id: int
    amount: float = Field(ge=0)


class Reverse(BaseModel):
    order_id: int


def profile(conn, customer_id: int) -> dict:
    me = conn.execute(
        "SELECT * FROM customer_membership WHERE customer_id = %s", (customer_id,)
    ).fetchone()
    if me is None:
        raise HTTPException(404, "Customer not found")
    nxt = conn.execute(
        """SELECT tier, min_lifetime_points FROM membership_tiers
           WHERE min_lifetime_points > %s ORDER BY min_lifetime_points LIMIT 1""",
        (me["lifetime_points"],),
    ).fetchone()
    history = conn.execute(
        """SELECT order_id, points, reason, created_at FROM loyalty_transactions
           WHERE customer_id = %s ORDER BY txn_id DESC LIMIT 10""",
        (customer_id,),
    ).fetchall()
    me["next_tier"] = nxt["tier"] if nxt else None
    me["points_to_next_tier"] = (nxt["min_lifetime_points"] - me["lifetime_points"]) if nxt else 0
    me["history"] = history
    me["pod"] = db.POD_NAME
    return me


@app.post("/customers")
def register_or_update(c: CustomerIn):
    """Sign up a new member (with a welcome bonus) or update an existing one."""
    with db.pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """INSERT INTO customers (phone, name, dietary_notes) VALUES (%s, %s, %s)
                   ON CONFLICT (phone) DO UPDATE
                       SET name = EXCLUDED.name, dietary_notes = EXCLUDED.dietary_notes
                   RETURNING customer_id, (xmax = 0) AS is_new""",   # xmax = 0 means a new row
                (c.phone.strip(), c.name.strip(), c.dietary_notes.strip() or "None"),
            ).fetchone()
            if row["is_new"]:
                conn.execute(
                    """UPDATE customers SET points = points + %s, lifetime_points = lifetime_points + %s
                       WHERE customer_id = %s""",
                    (WELCOME_BONUS, WELCOME_BONUS, row["customer_id"]),
                )
                conn.execute(
                    """INSERT INTO loyalty_transactions (customer_id, points, reason)
                       VALUES (%s, %s, 'BONUS')""",
                    (row["customer_id"], WELCOME_BONUS),
                )
            result = profile(conn, row["customer_id"])
    result["is_new"] = row["is_new"]
    return result


@app.get("/customers/{phone}")
def get_customer(phone: str):
    with db.pool.connection() as conn:
        row = conn.execute("SELECT customer_id FROM customers WHERE phone = %s", (phone,)).fetchone()
        if row is None:
            raise HTTPException(404, "Not a member yet")
        return profile(conn, row["customer_id"])


@app.get("/tiers")
def tiers():
    with db.pool.connection() as conn:
        return conn.execute("SELECT * FROM membership_tiers ORDER BY min_lifetime_points").fetchall()


@app.get("/members/top")
def top_members(limit: int = 5):
    with db.pool.connection() as conn:
        return conn.execute(
            """SELECT name, tier, points, lifetime_points FROM customer_membership
               ORDER BY lifetime_points DESC LIMIT %s""",
            (min(limit, 20),),
        ).fetchall()


# ---------- internal endpoints (called by the orders service only) ----------
@app.post("/redeem")
def redeem(r: Redeem):
    with db.pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """UPDATE customers SET points = points - %s
                   WHERE customer_id = %s AND points >= %s RETURNING points""",
                (r.points, r.customer_id, r.points),
            ).fetchone()
            if row is None:
                raise HTTPException(409, "Not enough points")
            conn.execute(
                """INSERT INTO loyalty_transactions (customer_id, order_id, points, reason)
                   VALUES (%s, %s, %s, 'REDEEM')""",
                (r.customer_id, r.order_id, -r.points),
            )
    return {"points_left": row["points"]}


@app.post("/earn")
def earn(e: Earn):
    with db.pool.connection() as conn:
        with conn.transaction():
            conn.execute("SELECT 1 FROM customers WHERE customer_id = %s FOR UPDATE", (e.customer_id,))
            before = conn.execute(
                "SELECT tier, points_multiplier FROM customer_membership WHERE customer_id = %s",
                (e.customer_id,),
            ).fetchone()
            if before is None:
                raise HTTPException(404, "Customer not found")
            points = int((e.amount // RUPEES_PER_POINT) * float(before["points_multiplier"]))
            if points > 0:
                conn.execute(
                    """UPDATE customers SET points = points + %s, lifetime_points = lifetime_points + %s
                       WHERE customer_id = %s""",
                    (points, points, e.customer_id),
                )
                conn.execute(
                    """INSERT INTO loyalty_transactions (customer_id, order_id, points, reason)
                       VALUES (%s, %s, %s, 'EARN')""",
                    (e.customer_id, e.order_id, points),
                )
            after = profile(conn, e.customer_id)
    return {
        "points_earned": points,
        "points": after["points"],
        "tier_before": before["tier"],
        "tier": after["tier"],
        "upgraded": after["tier"] != before["tier"],
    }


@app.post("/reverse")
def reverse(r: Reverse):
    """Undo all points of a cancelled order (runs at most once per order)."""
    with db.pool.connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(2, %s)", (r.order_id,))
            done = conn.execute(
                "SELECT 1 FROM loyalty_transactions WHERE order_id = %s AND reason = 'REVERSAL' LIMIT 1",
                (r.order_id,),
            ).fetchone()
            if done:
                return {"reversed": 0}
            rows = conn.execute(
                """SELECT customer_id, SUM(points) AS net,
                          COALESCE(SUM(points) FILTER (WHERE reason = 'EARN'), 0) AS earned
                   FROM loyalty_transactions WHERE order_id = %s GROUP BY customer_id""",
                (r.order_id,),
            ).fetchall()
            for row in rows:
                back = -row["net"]       # e.g. earned 15, spent 50 -> give back 35
                conn.execute(
                    """UPDATE customers SET points = GREATEST(points + %s, 0),
                                            lifetime_points = lifetime_points - %s
                       WHERE customer_id = %s""",
                    (back, row["earned"], row["customer_id"]),
                )
                conn.execute(
                    """INSERT INTO loyalty_transactions (customer_id, order_id, points, reason)
                       VALUES (%s, %s, %s, 'REVERSAL')""",
                    (row["customer_id"], r.order_id, back),
                )
    return {"reversed": len(rows)}

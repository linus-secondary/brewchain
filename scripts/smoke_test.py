"""BrewChain end-to-end test. Uses only the Python standard library.

    python scripts/smoke_test.py http://localhost:8080

Checks: shops and menu, sign-up bonus, preorders, queue, discounts, points,
tier upgrade, cancel (stock + points returned), internal endpoints blocked,
and that a rush of parallel orders can never oversell stock.
"""
import json
import random
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
passed = 0


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + "/api/" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def check(name, ok, info=""):
    global passed
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        sys.exit(1)
    passed += 1


def order(phone, shop, items, **extra):
    return call("POST", "orders/orders", {"shop_id": shop, "phone": phone, "name": "Tester",
                                          "items": items, **extra})


# 1. shops and menu
s, shops = call("GET", "menu/shops")
check("4 shops listed", s == 200 and len(shops["shops"]) == 4)
shop = shops["shops"][0]["shop_id"]
s, menu = call("GET", f"menu/menu?shop_id={shop}")
check("menu has items with stock", s == 200 and len(menu["items"]) >= 5)
items = {i["name"]: i for i in menu["items"]}
latte, muffin = items["Latte"], items["Chocolate Muffin"]

# 2. sign up
phone = str(9000000000 + random.randint(0, 99999999))
s, me = call("POST", "loyalty/customers", {"phone": phone, "name": "Tester", "dietary_notes": "No Sugar"})
check("new member gets 50 bonus points", s == 200 and me["points"] == 50 and me["tier"] == "Bronze", me.get("points"))

# 3. preorder
s, o1 = order(phone, shop, [{"item_id": latte["item_id"], "quantity": 2}, {"item_id": muffin["item_id"], "quantity": 1}])
expected = 2 * float(latte["price"]) + float(muffin["price"])
check("preorder placed with correct total", s == 201 and abs(o1["total"] - expected) < 0.01, o1.get("total"))
check("points earned (1 per Rs. 10)", o1["points_earned"] == int(expected // 10), o1["points_earned"])
s, inv = call("GET", f"menu/menu?shop_id={shop}")
new_latte = next(i for i in inv["items"] if i["item_id"] == latte["item_id"])
check("stock went down by 2", new_latte["in_stock"] <= latte["in_stock"] - 2)

# 4. queue
s, o2 = order(phone, shop, [{"item_id": latte["item_id"], "quantity": 1}])
check("second order is later in the queue", o2["queue_position"] > o1["queue_position"] and o2["ready_at"] > o1["ready_at"])

# 5. redeem points
s, me = call("GET", f"loyalty/customers/{phone}")
s, o3 = order(phone, shop, [{"item_id": latte["item_id"], "quantity": 1}], redeem_points=1000)
check("points redeemed (capped at 50% of bill)", s == 201 and o3["points_redeemed"] == min(me["points"], int(float(latte["price"]) * 0.5)), o3.get("points_redeemed"))

# 6. tier upgrade (big order pushes lifetime points past 150)
s, big = order(phone, shop, [{"item_id": items["Mocha"]["item_id"], "quantity": 8}])
s, me = call("GET", f"loyalty/customers/{phone}")
check("customer upgraded to Silver", me["tier"] in ("Silver", "Gold"), me["tier"])
s, o4 = order(phone, shop, [{"item_id": latte["item_id"], "quantity": 1}])
check("Silver member gets 5% discount", o4["discount"] > 0, o4["discount"])

# 7. cancel gives back stock and points
s, before = call("GET", f"menu/menu?shop_id={shop}")
before_latte = next(i for i in before["items"] if i["item_id"] == latte["item_id"])["in_stock"]
s, _ = call("PATCH", f"orders/orders/{o1['order_id']}/status", {"status": "CANCELLED"})
s, after = call("GET", f"menu/menu?shop_id={shop}")
after_latte = next(i for i in after["items"] if i["item_id"] == latte["item_id"])["in_stock"]
check("cancel returns stock", s == 200 and after_latte == before_latte + 2, f"{before_latte} -> {after_latte}")
s, _ = call("PATCH", f"orders/orders/{o1['order_id']}/status", {"status": "PREPARING"})
check("cancelled order cannot be restarted", s == 409)

# 8. status flow
for st in ("PREPARING", "READY", "COLLECTED"):
    s, _ = call("PATCH", f"orders/orders/{o2['order_id']}/status", {"status": st})
check("order moves RECEIVED -> PREPARING -> READY -> COLLECTED", s == 200)

# 9. security: browsers cannot call internal endpoints
s, _ = call("POST", "loyalty/earn", {"customer_id": 1, "order_id": 1, "amount": 99999})
check("internal endpoint blocked at gateway", s == 403)

# 10. concurrency: more customers than stock grab the same item at the same moment
shop4 = shops["shops"][-1]["shop_id"]
s, m4 = call("GET", f"menu/menu?shop_id={shop4}")
cold = next(i for i in m4["items"] if i["name"] == "Cold Brew")
stock = cold["in_stock"]
per_order = 20
customers = stock // per_order + 25        # 25 more customers than there is stock for


def rush(n):
    return order(str(8000000000 + random.randint(0, 99999999)), shop4,
                 [{"item_id": cold["item_id"], "quantity": per_order}])[0]


with ThreadPoolExecutor(max_workers=50) as pool:
    codes = list(pool.map(rush, range(customers)))
ok, sold_out = codes.count(201), codes.count(409)
s, m4 = call("GET", f"menu/menu?shop_id={shop4}")
left = next(i for i in m4["items"] if i["item_id"] == cold["item_id"])["in_stock"]
check(f"no overselling with {customers} parallel orders",
      ok == stock // per_order and left == stock - ok * per_order and left >= 0,
      f"stock {stock}, {ok} succeeded, {sold_out} sold out, {left} left")

s, stats = call("GET", "orders/stats")
check("dashboard stats available", s == 200 and stats["orders_today"] > 0)
call("POST", "menu/inventory/reset-demo")
print(f"\nAll {passed} checks passed against {BASE}")

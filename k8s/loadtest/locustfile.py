"""Simulated customers for the rush-hour demo (Locust).

Each simulated customer signs up, browses a shop's menu, places preorders
and tracks them, just like a real person on the app.
"""
import random

from locust import HttpUser, between, task

DIETS = ["None", "No Sugar", "Mild Sugar", "Lactose Free (Oat Milk)", "Nut Allergy", "Vegan"]


class Customer(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.phone = str(random.randint(6000000000, 6999999999))
        self.shop = random.randint(1, 4)
        self.last_order = None
        self.client.post("/api/loyalty/customers", name="sign up", json={
            "phone": self.phone, "name": "Load Test", "dietary_notes": random.choice(DIETS)})

    @task(5)
    def browse_menu(self):
        self.client.get(f"/api/menu/menu?shop_id={self.shop}", name="view menu")

    @task(1)
    def place_order(self):
        items = [{"item_id": random.randint(1, 10), "quantity": 1} for _ in range(random.randint(1, 3))]
        with self.client.post("/api/orders/orders", name="place order", catch_response=True, json={
                "shop_id": self.shop, "phone": self.phone, "name": "Load Test",
                "items": items, "redeem_points": random.choice([0, 0, 20])}) as resp:
            if resp.status_code == 201:
                self.last_order = resp.json()["order_id"]
            elif resp.status_code == 409:      # sold out is a normal answer, not an error
                resp.success()

    @task(2)
    def track_order(self):
        if self.last_order:
            self.client.get(f"/api/orders/orders/{self.last_order}", name="track order")

"""One image, four services. The SERVICE environment variable picks which one runs."""
import os

SERVICE = os.environ.get("SERVICE", "gateway")

if SERVICE == "menu":
    from menu_service import app
elif SERVICE == "orders":
    from orders_service import app
elif SERVICE == "loyalty":
    from loyalty_service import app
elif SERVICE == "gateway":
    from gateway import app
else:
    raise SystemExit(f"Unknown SERVICE '{SERVICE}'. Use menu, orders, loyalty or gateway.")

__all__ = ["app"]

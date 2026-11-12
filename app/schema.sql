-- =========================================================
--  BrewChain database schema (PostgreSQL)
--  Every service runs this at start-up. It is safe to run
--  many times (IF NOT EXISTS / ON CONFLICT DO NOTHING).
-- =========================================================

-- ---------- menu-inventory service ----------
CREATE TABLE IF NOT EXISTS shops (
    shop_id    SERIAL PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    area       TEXT NOT NULL,
    opens_at   TIME NOT NULL DEFAULT '07:00',
    closes_at  TIME NOT NULL DEFAULT '22:00'
);

CREATE TABLE IF NOT EXISTS menu_items (
    item_id      SERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    category     TEXT NOT NULL CHECK (category IN ('Coffee', 'Tea', 'Cold Drink', 'Snack')),
    price        NUMERIC(8,2) NOT NULL CHECK (price > 0),
    prep_minutes INT NOT NULL DEFAULT 3 CHECK (prep_minutes > 0)
);

CREATE TABLE IF NOT EXISTS shop_inventory (
    shop_id       INT NOT NULL REFERENCES shops(shop_id),
    item_id       INT NOT NULL REFERENCES menu_items(item_id),
    quantity      INT NOT NULL CHECK (quantity >= 0),      -- can never go below zero
    reorder_level INT NOT NULL DEFAULT 20,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_id, item_id)                          -- composite key
);

CREATE TABLE IF NOT EXISTS stock_movements (                -- audit trail
    movement_id SERIAL PRIMARY KEY,
    shop_id     INT NOT NULL,
    item_id     INT NOT NULL,
    change      INT NOT NULL,                               -- negative = sold, positive = restock
    reason      TEXT NOT NULL CHECK (reason IN ('ORDER', 'CANCEL', 'RESTOCK')),
    order_id    INT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (shop_id, item_id) REFERENCES shop_inventory(shop_id, item_id)
);

-- ---------- loyalty service ----------
CREATE TABLE IF NOT EXISTS membership_tiers (
    tier                TEXT PRIMARY KEY,
    min_lifetime_points INT NOT NULL UNIQUE,
    points_multiplier   NUMERIC(3,2) NOT NULL,
    discount_pct        INT NOT NULL CHECK (discount_pct BETWEEN 0 AND 50)
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id     SERIAL PRIMARY KEY,
    phone           TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    dietary_notes   TEXT NOT NULL DEFAULT 'None',
    points          INT NOT NULL DEFAULT 0 CHECK (points >= 0),
    lifetime_points INT NOT NULL DEFAULT 0,
    joined_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- orders service ----------
CREATE TABLE IF NOT EXISTS orders (
    order_id        SERIAL PRIMARY KEY,
    shop_id         INT NOT NULL REFERENCES shops(shop_id),
    customer_id     INT NOT NULL REFERENCES customers(customer_id),
    order_type      TEXT NOT NULL CHECK (order_type IN ('PREORDER', 'WALK_IN')),
    status          TEXT NOT NULL DEFAULT 'RECEIVED'
                    CHECK (status IN ('RECEIVED', 'PREPARING', 'READY', 'COLLECTED', 'CANCELLED')),
    subtotal        NUMERIC(10,2) NOT NULL,
    discount        NUMERIC(10,2) NOT NULL DEFAULT 0,
    points_redeemed INT NOT NULL DEFAULT 0,
    total           NUMERIC(10,2) NOT NULL CHECK (total >= 0),
    points_earned   INT NOT NULL DEFAULT 0,
    pickup_at       TIMESTAMPTZ,                            -- chosen by customer (preorders)
    ready_at        TIMESTAMPTZ NOT NULL,                   -- estimated by the queue
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    served_by       TEXT                                    -- name of the Kubernetes pod
);
CREATE INDEX IF NOT EXISTS idx_orders_shop_status ON orders (shop_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders (created_at);

CREATE TABLE IF NOT EXISTS order_items (
    order_id   INT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    item_id    INT NOT NULL REFERENCES menu_items(item_id),
    quantity   INT NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(8,2) NOT NULL,                       -- price at time of sale
    PRIMARY KEY (order_id, item_id)
);

CREATE TABLE IF NOT EXISTS loyalty_transactions (           -- points ledger
    txn_id      SERIAL PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES customers(customer_id),
    order_id    INT REFERENCES orders(order_id),
    points      INT NOT NULL,                               -- + earned, - spent
    reason      TEXT NOT NULL CHECK (reason IN ('EARN', 'REDEEM', 'BONUS', 'REVERSAL')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- views (saved queries for reports) ----------
CREATE OR REPLACE VIEW customer_membership AS
SELECT c.customer_id, c.name, c.phone, c.dietary_notes, c.points, c.lifetime_points,
       t.tier, t.points_multiplier, t.discount_pct
FROM customers c
JOIN membership_tiers t
  ON t.min_lifetime_points = (SELECT MAX(min_lifetime_points)
                              FROM membership_tiers
                              WHERE min_lifetime_points <= c.lifetime_points);

CREATE OR REPLACE VIEW shop_sales_today AS
SELECT s.shop_id, s.name AS shop,
       COUNT(o.order_id) FILTER (WHERE o.status <> 'CANCELLED')                       AS orders,
       COALESCE(SUM(o.total) FILTER (WHERE o.status <> 'CANCELLED'), 0)               AS revenue,
       COUNT(o.order_id) FILTER (WHERE o.status IN ('RECEIVED', 'PREPARING'))        AS in_queue,
       COUNT(o.order_id) FILTER (WHERE o.order_type = 'PREORDER' AND o.status <> 'CANCELLED') AS preorders
FROM shops s
LEFT JOIN orders o ON o.shop_id = s.shop_id AND o.created_at >= date_trunc('day', now())
GROUP BY s.shop_id, s.name;

-- ---------- sample data ----------
INSERT INTO shops (name, area) VALUES
    ('BrewChain Downtown',  'City Centre'),
    ('BrewChain Lakeside',  'Lake Road'),
    ('BrewChain Tech Park', 'IT Corridor'),
    ('BrewChain Old Town',  'Heritage Market')
ON CONFLICT (name) DO NOTHING;

INSERT INTO menu_items (name, category, price, prep_minutes) VALUES
    ('Espresso',         'Coffee',     120, 2),
    ('Cappuccino',       'Coffee',     150, 3),
    ('Latte',            'Coffee',     160, 3),
    ('Mocha',            'Coffee',     180, 4),
    ('Cold Brew',        'Cold Drink', 190, 2),
    ('Iced Latte',       'Cold Drink', 180, 3),
    ('Masala Chai',      'Tea',        90,  3),
    ('Green Tea',        'Tea',        100, 2),
    ('Chocolate Muffin', 'Snack',      90,  1),
    ('Veg Sandwich',     'Snack',      140, 4)
ON CONFLICT (name) DO NOTHING;

-- every shop stocks every item (starts with 1000 of each)
INSERT INTO shop_inventory (shop_id, item_id, quantity)
SELECT s.shop_id, m.item_id, 1000 FROM shops s CROSS JOIN menu_items m
ON CONFLICT (shop_id, item_id) DO NOTHING;

INSERT INTO membership_tiers VALUES
    ('Bronze', 0,   1.00, 0),
    ('Silver', 150, 1.25, 5),
    ('Gold',   500, 1.50, 10)
ON CONFLICT (tier) DO NOTHING;

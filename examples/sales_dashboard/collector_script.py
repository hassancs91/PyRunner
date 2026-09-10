"""
Sales Collector — a sample PyRun script.

Generates a handful of synthetic orders each run and appends them to the
`sales_data` DataStore, which the Sales Dashboard plugin reads.

Setup in PyRun:
  1. Data Stores -> create one named exactly  sales_data
  2. Scripts -> create a script named exactly  Sales Collector  and paste this code
  3. Run it (or let the dashboard's "Run collector now" button trigger it)

`pyrunner_datastore` is provided by PyRun automatically — no install needed.
"""

import datetime
import random

from pyrunner_datastore import DataStore

PRODUCTS = [
    ("Widget", 19.99),
    ("Gadget", 49.99),
    ("Gizmo", 99.99),
    ("Doohickey", 9.99),
    ("Sprocket", 29.99),
]

store = DataStore("sales_data")
orders = store.get("orders", [])

now = datetime.datetime.utcnow().isoformat(timespec="seconds")
new_count = random.randint(3, 8)
for _ in range(new_count):
    name, price = random.choice(PRODUCTS)
    qty = random.randint(1, 5)
    orders.append(
        {"ts": now, "product": name, "qty": qty, "revenue": round(price * qty, 2)}
    )

store["orders"] = orders
store["last_run"] = now

total = sum(o["revenue"] for o in orders)
print(f"Added {new_count} orders ({len(orders)} total). Revenue so far: ${total:.2f}")

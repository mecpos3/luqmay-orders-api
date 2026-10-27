# Luqmay Cafe Orders API — TEST ONLY

This folder is a first-stage backend for testing website order submission and secure order retrieval by MEC-POS. It does **not** modify the existing website or POS automatically.

## Render settings

Create a Render **Web Service** connected to the `luqmay-cafe` repository, and select the branch that contains this folder. Set:

- **Root Directory:** `api`
- **Runtime:** Python 3
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`

Add these Environment Variables in Render:

- `POS_API_KEY` = a long random secret (at least 32 characters). Keep it private; it must never be placed in website JavaScript or shared publicly.
- `ALLOWED_ORIGINS` = `https://mecpos3.github.io`

After deployment, test `https://YOUR-SERVICE.onrender.com/health`. It should return JSON with `"ok": true`.

## API routes

- `POST /api/orders` — public website order submission. Validates basic fields and stores a pending order.
- `GET /api/orders/pending` — requires header `X-POS-API-Key: <POS_API_KEY>`.
- `GET /api/orders/{order_id}` — requires the same header.
- `POST /api/orders/{order_id}/status` — requires the same header; body example: `{"status":"imported"}`.

A website order body example:

```json
{
  "client_order_id": "browser-generated-unique-id",
  "customer_name": "Test Customer",
  "customer_phone": "03001234567",
  "order_type": "Pickup",
  "delivery_area": "",
  "address": "",
  "notes": "Test order only",
  "payment_method": "COD",
  "items": [
    {"name": "Chicken Burger", "quantity": 2, "unit_price": 249}
  ]
}
```

## Important test limitations

- The current GitHub Pages checkout still opens WhatsApp and does not call this API until the website checkout is updated with the deployed API URL.
- MEC-POS does not poll this API until POS integration is added and tested.
- The default database is SQLite at `/tmp/luqmay_orders.db`. On Render free instances, local files can be lost when the instance restarts/redeploys. This is for controlled testing only, **not live customer orders**. Before live use, configure a persistent database (e.g. PostgreSQL) and add durable backups.
- Do not put MySQL credentials in the browser or expose the POS MySQL server to the internet.
- Order totals are calculated from the item prices submitted by the website. POS staff must review the items and prices before charging; this test API is not a payment or inventory system.

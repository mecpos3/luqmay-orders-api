import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_NAME = "Luqmay Cafe Orders API"
DB_PATH = os.getenv("DATABASE_PATH", "/tmp/luqmay_orders.db")
POS_API_KEY = os.getenv("POS_API_KEY", "")
ALLOWED_ORIGINS = [
    x.strip() for x in os.getenv("ALLOWED_ORIGINS", "https://mecpos3.github.io").split(",") if x.strip()
]

app = FastAPI(title=APP_NAME, version="0.1.0", description="Temporary test API for Luqmay Cafe website orders and MEC-POS.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-POS-API-Key"],
)

# Simple in-memory rate limiter suitable only for an initial test service.
_RATE_BUCKETS: dict[str, list[float]] = {}
RATE_LIMIT = 12
RATE_WINDOW_SECONDS = 60


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                client_order_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                customer_name TEXT NOT NULL,
                customer_phone TEXT NOT NULL,
                order_type TEXT NOT NULL,
                delivery_area TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                payment_method TEXT NOT NULL DEFAULT 'COD',
                items_json TEXT NOT NULL,
                total REAL NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status, created_at)")


@app.on_event("startup")
def startup() -> None:
    init_db()


class OrderItem(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    quantity: int = Field(ge=1, le=100)
    unit_price: float = Field(ge=0, le=1000000)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Item name is required")
        return value


class WebsiteOrder(BaseModel):
    client_order_id: str | None = Field(default=None, max_length=80)
    customer_name: str = Field(min_length=1, max_length=120)
    customer_phone: str = Field(min_length=5, max_length=40)
    order_type: Literal["Delivery", "Pickup"]
    delivery_area: str = Field(default="", max_length=120)
    address: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=1000)
    payment_method: Literal["COD", "Cash on Delivery"] = "COD"
    items: list[OrderItem] = Field(min_length=1, max_length=100)

    @field_validator("customer_name", "customer_phone", "delivery_area", "address", "notes")
    @classmethod
    def trim_strings(cls, value: str) -> str:
        return value.strip()

    @field_validator("customer_phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        digits = re.sub(r"\D", "", value)
        if len(digits) < 7:
            raise ValueError("Please provide a valid phone number")
        return value

    @field_validator("address")
    @classmethod
    def delivery_address_required(cls, value: str, info):
        # The order_type may not yet be available depending on validation order; enforced in endpoint too.
        return value


def require_pos_key(x_pos_api_key: str | None = Header(default=None)) -> None:
    if not POS_API_KEY:
        raise HTTPException(status_code=503, detail="POS_API_KEY has not been configured on the server")
    if not x_pos_api_key or not __import__("hmac").compare_digest(x_pos_api_key, POS_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid POS API key")


def order_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "client_order_id": row["client_order_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "status": row["status"],
        "customer": {
            "name": row["customer_name"],
            "phone": row["customer_phone"],
        },
        "order_type": row["order_type"],
        "delivery_area": row["delivery_area"],
        "address": row["address"],
        "notes": row["notes"],
        "payment_method": row["payment_method"],
        "items": json.loads(row["items_json"]),
        "total": row["total"],
        "currency": "PKR",
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": APP_NAME, "time": now_iso(), "mode": "test"}


@app.post("/api/orders", status_code=201)
def create_order(order: WebsiteOrder, request: Request) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    recent = [t for t in _RATE_BUCKETS.get(client_ip, []) if now - t < RATE_WINDOW_SECONDS]
    if len(recent) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many order attempts. Please wait and try again.")
    recent.append(now)
    _RATE_BUCKETS[client_ip] = recent

    if order.order_type == "Delivery" and (not order.delivery_area or not order.address):
        raise HTTPException(status_code=422, detail="Delivery area and address are required for delivery orders")

    clean_items = []
    total = 0.0
    for item in order.items:
        line_total = round(item.quantity * item.unit_price, 2)
        total += line_total
        clean_items.append({
            "name": item.name.strip(),
            "quantity": item.quantity,
            "unit_price": round(item.unit_price, 2),
            "line_total": line_total,
        })
    total = round(total, 2)
    client_order_id = order.client_order_id.strip() if order.client_order_id else None
    order_id = "W-" + uuid.uuid4().hex[:10].upper()
    created = now_iso()

    try:
        with get_db() as conn:
            if client_order_id:
                existing = conn.execute("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)).fetchone()
                if existing:
                    return {"ok": True, "duplicate": True, "order": {"id": existing["id"], "status": existing["status"], "total": existing["total"]}}
            conn.execute(
                """INSERT INTO orders (
                    id, client_order_id, created_at, updated_at, status,
                    customer_name, customer_phone, order_type, delivery_area,
                    address, notes, payment_method, items_json, total
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, client_order_id, created, created, order.customer_name,
                 order.customer_phone, order.order_type, order.delivery_area,
                 order.address, order.notes, "COD", json.dumps(clean_items, ensure_ascii=False), total),
            )
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    except sqlite3.IntegrityError:
        # Handles a near-simultaneous duplicate client_order_id.
        with get_db() as conn:
            existing = conn.execute("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)).fetchone()
        if existing:
            return {"ok": True, "duplicate": True, "order": {"id": existing["id"], "status": existing["status"], "total": existing["total"]}}
        raise HTTPException(status_code=409, detail="Could not save duplicate order")

    return {"ok": True, "duplicate": False, "order": order_to_dict(row)}


@app.get("/api/orders/pending", dependencies=[Depends(require_pos_key)])
def pending_orders(limit: int = 50) -> dict:
    limit = max(1, min(limit, 100))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?", (limit,)
        ).fetchall()
    return {"ok": True, "orders": [order_to_dict(row) for row in rows], "count": len(rows)}


@app.get("/api/orders/{order_id}", dependencies=[Depends(require_pos_key)])
def get_order(order_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    return {"ok": True, "order": order_to_dict(row)}


class StatusUpdate(BaseModel):
    status: Literal["pending", "imported", "completed", "cancelled"]


@app.post("/api/orders/{order_id}/status", dependencies=[Depends(require_pos_key)])
def update_order_status(order_id: str, body: StatusUpdate) -> dict:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (body.status, now_iso(), order_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Order not found")
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return {"ok": True, "order": order_to_dict(row)}

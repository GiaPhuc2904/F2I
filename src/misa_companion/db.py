"""Quản lý kết nối và lược đồ SQLite cục bộ (lưu tên thật song song mã ẩn danh cho chế độ Che tên)."""

from __future__ import annotations

from pathlib import Path
import sqlite3

from misa_companion.config import DEFAULT_DB_PATH

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS import_metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS analysis_cache (
    cache_key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS warehouses (
    warehouse_code TEXT PRIMARY KEY,
    warehouse_name TEXT,
    warehouse_anon TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id TEXT PRIMARY KEY,
    supplier_code TEXT,
    supplier_name TEXT,
    supplier_anon TEXT NOT NULL,
    default_lead_time_days INTEGER DEFAULT 4
);

CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    product_name TEXT,
    sku_anon TEXT NOT NULL,
    category TEXT,
    warehouse_code TEXT,
    base_uom TEXT,
    box_uom TEXT,
    units_per_box REAL DEFAULT 1.0,
    is_promo_sku INTEGER DEFAULT 0,
    latest_purchase_cost REAL DEFAULT 0.0,
    selling_price_1 REAL DEFAULT 0.0,
    min_stock_qty REAL DEFAULT 0.0,
    shelf_life_days INTEGER,
    status TEXT,
    has_stock_data INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    customer_code TEXT,
    customer_name TEXT,
    customer_anon TEXT NOT NULL,
    source_flags TEXT,
    first_date TEXT,
    last_date TEXT
);

CREATE TABLE IF NOT EXISTS inventory_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    warehouse_code TEXT,
    product_id TEXT NOT NULL,
    posting_date TEXT,
    doc_date TEXT NOT NULL,
    doc_id TEXT,
    doc_type TEXT,
    uom TEXT,
    unit_cost REAL DEFAULT 0.0,
    in_qty REAL DEFAULT 0.0,
    in_value REAL DEFAULT 0.0,
    out_qty REAL DEFAULT 0.0,
    out_value REAL DEFAULT 0.0,
    balance_qty REAL DEFAULT 0.0,
    balance_value REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS inventory_summary (
    warehouse_code TEXT NOT NULL,
    product_id TEXT NOT NULL,
    uom TEXT,
    opening_qty REAL DEFAULT 0.0,
    opening_value REAL DEFAULT 0.0,
    in_qty REAL DEFAULT 0.0,
    in_value REAL DEFAULT 0.0,
    out_qty REAL DEFAULT 0.0,
    out_value REAL DEFAULT 0.0,
    closing_qty REAL DEFAULT 0.0,
    closing_value REAL DEFAULT 0.0,
    selling_price_1 REAL DEFAULT 0.0,
    as_of_date TEXT,
    PRIMARY KEY (warehouse_code, product_id)
);

CREATE TABLE IF NOT EXISTS inbounds (
    inbound_id TEXT NOT NULL,
    supplier_id TEXT,
    inbound_date TEXT NOT NULL,
    gross_amount REAL DEFAULT 0.0,
    discount_amount REAL DEFAULT 0.0,
    return_amount REAL DEFAULT 0.0,
    net_amount REAL DEFAULT 0.0,
    PRIMARY KEY (inbound_id, inbound_date)
);

CREATE TABLE IF NOT EXISTS inbound_details (
    inbound_detail_id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_id TEXT NOT NULL,
    supplier_id TEXT,
    inbound_date TEXT NOT NULL,
    product_id TEXT NOT NULL,
    uom TEXT,
    qty_gross REAL DEFAULT 0.0,
    qty_return REAL DEFAULT 0.0,
    qty_net REAL DEFAULT 0.0,
    unit_cost REAL DEFAULT 0.0,
    gross_amount REAL DEFAULT 0.0,
    discount_amount REAL DEFAULT 0.0,
    discount_rate REAL DEFAULT 0.0,
    return_amount REAL DEFAULT 0.0,
    net_amount REAL DEFAULT 0.0,
    expiry_date TEXT
);

CREATE TABLE IF NOT EXISTS outbounds (
    outbound_id TEXT NOT NULL,
    customer_id TEXT,
    outbound_date TEXT NOT NULL,
    warehouse_code TEXT,
    gross_revenue REAL DEFAULT 0.0,
    discount_amount REAL DEFAULT 0.0,
    return_amount REAL DEFAULT 0.0,
    net_revenue REAL DEFAULT 0.0,
    total_cogs REAL DEFAULT 0.0,
    gross_profit REAL DEFAULT 0.0,
    PRIMARY KEY (outbound_id, outbound_date)
);

CREATE TABLE IF NOT EXISTS outbound_details (
    outbound_detail_id INTEGER PRIMARY KEY AUTOINCREMENT,
    outbound_id TEXT NOT NULL,
    customer_id TEXT,
    outbound_date TEXT NOT NULL,
    warehouse_code TEXT,
    product_id TEXT NOT NULL,
    uom TEXT,
    base_uom TEXT,
    conversion_rate REAL DEFAULT 1.0,
    qty_gross REAL DEFAULT 0.0,
    qty_return REAL DEFAULT 0.0,
    qty_net REAL DEFAULT 0.0,
    base_qty_net REAL DEFAULT 0.0,
    unit_price REAL DEFAULT 0.0,
    gross_revenue REAL DEFAULT 0.0,
    discount_amount REAL DEFAULT 0.0,
    return_amount REAL DEFAULT 0.0,
    net_revenue REAL DEFAULT 0.0,
    cogs REAL DEFAULT 0.0,
    is_free_gift_line INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ar_ledger (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL,
    posting_date TEXT,
    doc_date TEXT NOT NULL,
    doc_id TEXT,
    ar_account TEXT,
    contra_account TEXT,
    debit_amount REAL DEFAULT 0.0,
    credit_amount REAL DEFAULT 0.0,
    balance_debit REAL DEFAULT 0.0,
    balance_credit REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id TEXT PRIMARY KEY,
    outbound_id TEXT,
    customer_id TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    invoice_amount REAL NOT NULL,
    paid_cash_amount REAL DEFAULT 0.0,
    credit_note_amount REAL DEFAULT 0.0,
    outstanding_amount REAL DEFAULT 0.0,
    first_pay_date TEXT,
    last_pay_date TEXT,
    days_to_pay INTEGER,
    payment_status TEXT
);

CREATE TABLE IF NOT EXISTS customer_payments (
    payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    payment_date TEXT NOT NULL,
    doc_id TEXT,
    contra_account TEXT,
    payment_type TEXT,
    allocated_amount REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inv_led_prod_date ON inventory_ledger(product_id, doc_date);
CREATE INDEX IF NOT EXISTS idx_inv_led_date ON inventory_ledger(doc_date);
CREATE INDEX IF NOT EXISTS idx_inb_det_prod_date ON inbound_details(product_id, inbound_date);
CREATE INDEX IF NOT EXISTS idx_inb_det_date ON inbound_details(inbound_date);
CREATE INDEX IF NOT EXISTS idx_out_det_prod_date ON outbound_details(product_id, outbound_date);
CREATE INDEX IF NOT EXISTS idx_out_det_date ON outbound_details(outbound_date);
CREATE INDEX IF NOT EXISTS idx_ar_cust_date ON ar_ledger(customer_id, doc_date);
CREATE INDEX IF NOT EXISTS idx_ar_date ON ar_ledger(doc_date);
CREATE INDEX IF NOT EXISTS idx_inv_cust_date ON invoices(customer_id, invoice_date);
"""

DATA_TABLES = [
    "customer_payments",
    "invoices",
    "ar_ledger",
    "outbound_details",
    "outbounds",
    "inbound_details",
    "inbounds",
    "inventory_summary",
    "inventory_ledger",
    "customers",
    "suppliers",
    "products",
    "warehouses",
    "import_metadata",
    "analysis_cache",
]


def get_connection(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection, reset: bool = False) -> None:
    if reset:
        for tbl in DATA_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {tbl}")
    conn.executescript(SCHEMA_SQL)
    conn.commit()

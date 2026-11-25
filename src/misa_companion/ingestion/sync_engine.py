"""Đồng bộ dữ liệu MISA vào SQLite theo chuẩn Date-Window Upsert (0% trùng dòng) + Đối trừ FIFO + Báo cáo kiểm tra file."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any

from misa_companion.db import get_connection, init_db
from misa_companion.ingestion.misa_parser import (
    parse_ar_ledger_xlsx,
    parse_inventory_ledger_xlsx,
    parse_inventory_summary_xlsx,
    parse_products_xlsx,
    parse_purchases_xlsx,
    parse_sales_xlsx,
)
from misa_companion.ingestion.zip_loader import classify_misa_files, extract_zip_safely

FILE_LABELS_VI: dict[str, str] = {
    "products": "Danh sách hàng hóa dịch vụ",
    "inventory_ledger": "Sổ chi tiết vật tư hàng hóa",
    "inventory_summary": "Tổng hợp tồn kho (dự phòng)",
    "purchases": "Sổ chi tiết mua hàng",
    "sales": "Sổ chi tiết bán hàng",
    "ar_ledger": "Chi tiết công nợ phải thu khách hàng",
}


class _AnonRegistry:
    """Giữ mã ẩn danh (`KH_0001`, `SKU_0001`, `SUP_001`, `KHO_01`) ổn định xuyên suốt các lần nạp `.zip`."""

    def __init__(self, conn: sqlite3.Connection, table: str, id_col: str, anon_col: str, prefix: str, width: int):
        self.prefix = prefix
        self.width = width
        self.mapping: dict[str, str] = {}
        self.max_num = 0
        pat = re.compile(rf"^{re.escape(prefix)}_(\d+)$")
        for row in conn.execute(f"SELECT {id_col}, {anon_col} FROM {table}").fetchall():
            k, v = str(row[0]), str(row[1])
            self.mapping[k] = v
            m = pat.match(v)
            if m:
                self.max_num = max(self.max_num, int(m.group(1)))

    def get_or_assign(self, item_id: str) -> str:
        if item_id in self.mapping:
            return self.mapping[item_id]
        self.max_num += 1
        code = f"{self.prefix}_{self.max_num:0{self.width}d}"
        self.mapping[item_id] = code
        return code


def rebuild_fifo_invoices_and_payments(conn: sqlite3.Connection) -> dict[str, Any]:
    """Đối trừ FIFO toàn cục trên `ar_ledger` để tái dựng bảng `invoices` & `customer_payments` chính xác từng ngày trả."""
    conn.execute("DELETE FROM customer_payments")
    conn.execute("DELETE FROM invoices")

    rows = conn.execute(
        """
        SELECT entry_id, customer_id, doc_date, doc_id, contra_account, debit_amount, credit_amount
        FROM ar_ledger
        ORDER BY customer_id, doc_date, entry_id
        """
    ).fetchall()

    by_cust: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    for r in rows:
        by_cust[str(r[1])].append(r)

    invoices_out: list[tuple[Any, ...]] = []
    payments_out: list[tuple[Any, ...]] = []

    for cid, entries in by_cust.items():
        open_invoices: list[dict[str, Any]] = []
        inv_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        ordered_events: list[tuple[str, Any]] = []

        for _, _, doc_d, doc_id, contra, debit, credit in entries:
            if debit > 0:
                key = (str(doc_id), str(doc_d))
                if key not in inv_by_key:
                    inv = {
                        "invoice_id": f"{cid}_{doc_id}_{doc_d}",
                        "outbound_id": str(doc_id),
                        "customer_id": cid,
                        "invoice_date": str(doc_d),
                        "invoice_amount": 0.0,
                        "paid_cash": 0.0,
                        "credit_note": 0.0,
                        "remaining": 0.0,
                        "first_pay_date": None,
                        "last_pay_date": None,
                    }
                    inv_by_key[key] = inv
                    ordered_events.append(("INV", inv))
                inv_by_key[key]["invoice_amount"] += float(debit)
                inv_by_key[key]["remaining"] += float(debit)
            if credit > 0:
                ordered_events.append(("PAY", (str(doc_d), str(doc_id), str(contra or ""), float(credit))))

        for ev_type, payload in ordered_events:
            if ev_type == "INV":
                open_invoices.append(payload)
            else:
                pay_date, pay_doc, contra, amt = payload
                is_cash = contra.startswith("111") or contra.startswith("112") or contra == ""
                pay_type = (
                    "CASH"
                    if contra.startswith("111")
                    else (
                        "BANK_TRANSFER"
                        if contra.startswith("112")
                        else ("CREDIT_NOTE" if contra.startswith(("521", "511")) else "OTHER")
                    )
                )
                rem_pay = amt
                for inv in open_invoices:
                    if rem_pay <= 1e-6:
                        break
                    if inv["remaining"] <= 1e-6:
                        continue
                    alloc = min(inv["remaining"], rem_pay)
                    inv["remaining"] -= alloc
                    rem_pay -= alloc
                    if is_cash:
                        inv["paid_cash"] += alloc
                    else:
                        inv["credit_note"] += alloc
                    if inv["first_pay_date"] is None or pay_date < inv["first_pay_date"]:
                        inv["first_pay_date"] = pay_date
                    if inv["last_pay_date"] is None or pay_date > inv["last_pay_date"]:
                        inv["last_pay_date"] = pay_date
                    payments_out.append(
                        (inv["invoice_id"], cid, pay_date, pay_doc, contra, pay_type, round(alloc, 2))
                    )

        for inv in open_invoices:
            outst = max(0.0, round(inv["remaining"], 2))
            if outst <= 1.0:
                status = "PAID"
                outst = 0.0
                d_inv = date.fromisoformat(inv["invoice_date"])
                d_pay = date.fromisoformat(inv["last_pay_date"]) if inv["last_pay_date"] else d_inv
                days_to_pay = max(0, (d_pay - d_inv).days)
            elif (inv["paid_cash"] + inv["credit_note"]) > 1.0:
                status = "PARTIAL"
                days_to_pay = None
            else:
                status = "UNPAID"
                days_to_pay = None

            invoices_out.append(
                (
                    inv["invoice_id"],
                    inv["outbound_id"],
                    inv["customer_id"],
                    inv["invoice_date"],
                    round(inv["invoice_amount"], 2),
                    round(inv["paid_cash"], 2),
                    round(inv["credit_note"], 2),
                    outst,
                    inv["first_pay_date"],
                    inv["last_pay_date"],
                    days_to_pay,
                    status,
                )
            )

    conn.executemany(
        """
        INSERT OR REPLACE INTO invoices (
            invoice_id, outbound_id, customer_id, invoice_date, invoice_amount,
            paid_cash_amount, credit_note_amount, outstanding_amount,
            first_pay_date, last_pay_date, days_to_pay, payment_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        invoices_out,
    )
    conn.executemany(
        """
        INSERT INTO customer_payments (
            invoice_id, customer_id, payment_date, doc_id, contra_account, payment_type, allocated_amount
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        payments_out,
    )
    return {
        "fifo_invoices_count": len(invoices_out),
        "fifo_paid_invoices": sum(1 for x in invoices_out if x[11] == "PAID"),
        "fifo_unpaid_or_partial": sum(1 for x in invoices_out if x[11] != "PAID"),
        "fifo_payment_allocations": len(payments_out),
    }


def _window_stats(conn: sqlite3.Connection, table: str, date_col: str, min_d: str, max_d: str, incoming_cnt: int) -> dict[str, int]:
    kept = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {date_col} < ? OR {date_col} > ?",
            (min_d, max_d),
        ).fetchone()[0]
        or 0
    )
    replaced = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {date_col} >= ? AND {date_col} <= ?",
            (min_d, max_d),
        ).fetchone()[0]
        or 0
    )
    updated = min(replaced, incoming_cnt)
    added = max(0, incoming_cnt - replaced)
    return {
        "kept_history_rows": kept,
        "replaced_rows": replaced,
        "incoming_rows": incoming_cnt,
        "updated_rows": updated,
        "added_rows": added,
    }


def sync_misa_directory(
    input_dir: str | Path,
    db_path: str | Path,
    mode: str = "merge",
    is_demo: bool = False,
) -> dict[str, Any]:
    """Đọc toàn bộ file `.xlsx` trong `input_dir`, đồng bộ vào SQLite theo Date-Window Upsert và trả Báo cáo kiểm tra."""
    if mode not in ("merge", "reset"):
        raise ValueError("mode phải là 'merge' hoặc 'reset'.")

    base = Path(input_dir)
    xlsx_files = sorted(
        p for p in base.rglob("*.xlsx") if not p.name.startswith(".~lock") and not p.name.startswith("~$")
    )
    roles = classify_misa_files(xlsx_files)
    detected_any = any(v is not None for v in roles.values())
    if not detected_any:
        raise ValueError("Không nhận diện được sổ MISA hợp lệ nào trong tệp tải lên.")

    # 1. Parse toàn bộ sang RAM trước khi chạm vào SQLite
    parsed_products = parse_products_xlsx(roles["products"]) if roles["products"] else None
    parsed_ar = parse_ar_ledger_xlsx(roles["ar_ledger"]) if roles["ar_ledger"] else None
    parsed_inv_led = parse_inventory_ledger_xlsx(roles["inventory_ledger"]) if roles["inventory_ledger"] else None
    parsed_inv_sum = parse_inventory_summary_xlsx(roles["inventory_summary"]) if roles["inventory_summary"] else None
    parsed_purch = parse_purchases_xlsx(roles["purchases"]) if roles["purchases"] else None
    parsed_sales = (
        parse_sales_xlsx(roles["sales"], ar_doc_to_customer=parsed_ar["doc_to_customer"] if parsed_ar else None)
        if roles["sales"]
        else None
    )

    conn = get_connection(db_path)
    try:
        # Tự động xóa sạch dữ liệu mẫu nếu trước đó đang mở Demo mà người dùng nạp tệp thật ở chế độ merge!
        effective_mode = mode
        if not is_demo and mode == "merge":
            init_db(conn, reset=False)
            prev_demo = conn.execute("SELECT value FROM import_metadata WHERE key = 'is_demo_data'").fetchone()
            has_demo_sku = conn.execute(
                "SELECT 1 FROM products WHERE product_id = 'SP001' AND product_name = 'Nước ngọt Cola 330ml' LIMIT 1"
            ).fetchone()
            if (prev_demo and str(prev_demo[0]) == "1") or (
                has_demo_sku and parsed_products and all(r["product_id"] != "SP001" for r in parsed_products["rows"])
            ):
                effective_mode = "reset"

        init_db(conn, reset=(effective_mode == "reset"))

        sku_anon_reg = _AnonRegistry(conn, "products", "product_id", "sku_anon", "SKU", 4)
        cust_anon_reg = _AnonRegistry(conn, "customers", "customer_id", "customer_anon", "KH", 4)
        sup_anon_reg = _AnonRegistry(conn, "suppliers", "supplier_id", "supplier_anon", "SUP", 3)
        wh_anon_reg = _AnonRegistry(conn, "warehouses", "warehouse_code", "warehouse_anon", "KHO", 2)

        table_sync_stats: dict[str, dict[str, int]] = {}
        warehouses_seen: set[str] = set()

        with conn:
            # --- Master data: Products ---
            if parsed_products:
                for r in parsed_products["rows"]:
                    if r["warehouse_code"]:
                        warehouses_seen.add(r["warehouse_code"])
                conn.executemany(
                    """
                    INSERT INTO products (
                        product_id, sku, product_name, sku_anon, category, warehouse_code,
                        base_uom, box_uom, units_per_box, is_promo_sku,
                        latest_purchase_cost, selling_price_1, min_stock_qty, shelf_life_days, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(product_id) DO UPDATE SET
                        product_name = excluded.product_name,
                        category = CASE WHEN excluded.category != '' THEN excluded.category ELSE products.category END,
                        warehouse_code = CASE WHEN excluded.warehouse_code != '' THEN excluded.warehouse_code ELSE products.warehouse_code END,
                        base_uom = CASE WHEN excluded.base_uom != '' THEN excluded.base_uom ELSE products.base_uom END,
                        box_uom = CASE WHEN excluded.box_uom != '' THEN excluded.box_uom ELSE products.box_uom END,
                        units_per_box = CASE WHEN excluded.units_per_box > 1.0 THEN excluded.units_per_box ELSE products.units_per_box END,
                        is_promo_sku = excluded.is_promo_sku,
                        latest_purchase_cost = CASE WHEN excluded.latest_purchase_cost > 0 THEN excluded.latest_purchase_cost ELSE products.latest_purchase_cost END,
                        selling_price_1 = CASE WHEN excluded.selling_price_1 > 0 THEN excluded.selling_price_1 ELSE products.selling_price_1 END,
                        min_stock_qty = excluded.min_stock_qty,
                        status = excluded.status
                    """,
                    [
                        (
                            r["product_id"],
                            r["sku"],
                            r["product_name"],
                            sku_anon_reg.get_or_assign(r["product_id"]),
                            r["category"],
                            r["warehouse_code"],
                            r["base_uom"],
                            r["box_uom"],
                            r["units_per_box"],
                            r["is_promo_sku"],
                            r["latest_purchase_cost"],
                            r["selling_price_1"],
                            r["min_stock_qty"],
                            r["shelf_life_days"],
                            r["status"],
                        )
                        for r in parsed_products["rows"]
                    ],
                )

            # Fallback products từ Purchases / Sales / Inventory nếu thiếu Danh mục hàng hóa
            fallback_prods: dict[str, dict[str, Any]] = {}
            if parsed_purch:
                for p in parsed_purch["product_fallbacks"]:
                    fallback_prods.setdefault(p["product_id"], p)
            if parsed_sales:
                for p in parsed_sales["product_fallbacks"]:
                    fallback_prods.setdefault(p["product_id"], p)
            if parsed_inv_led:
                for r in parsed_inv_led["rows"]:
                    fallback_prods.setdefault(
                        r["product_id"],
                        {
                            "product_id": r["product_id"],
                            "sku": r["product_id"],
                            "product_name": r["product_name"],
                            "warehouse_code": r["warehouse_code"],
                            "base_uom": r["uom"],
                            "latest_purchase_cost": r["unit_cost"],
                        },
                    )
            if parsed_inv_sum:
                for r in parsed_inv_sum["rows"]:
                    fallback_prods.setdefault(
                        r["product_id"],
                        {
                            "product_id": r["product_id"],
                            "sku": r["product_id"],
                            "product_name": r["product_name"],
                            "warehouse_code": r["warehouse_code"],
                            "base_uom": r["uom"],
                            "selling_price_1": r["selling_price_1"],
                        },
                    )
            if fallback_prods:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO products (
                        product_id, sku, product_name, sku_anon, warehouse_code, base_uom,
                        is_promo_sku, latest_purchase_cost, selling_price_1
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            pid,
                            pid,
                            p.get("product_name") or pid,
                            sku_anon_reg.get_or_assign(pid),
                            p.get("warehouse_code") or "",
                            p.get("base_uom") or "",
                            1 if pid.upper().endswith("_KM") else 0,
                            float(p.get("latest_purchase_cost") or 0.0),
                            float(p.get("selling_price_1") or 0.0),
                        )
                        for pid, p in fallback_prods.items()
                    ],
                )

            # --- Master data: Suppliers ---
            if parsed_purch and parsed_purch["suppliers"]:
                conn.executemany(
                    """
                    INSERT INTO suppliers (supplier_id, supplier_code, supplier_name, supplier_anon, default_lead_time_days)
                    VALUES (?, ?, ?, ?, 4)
                    ON CONFLICT(supplier_id) DO UPDATE SET
                        supplier_code = CASE WHEN excluded.supplier_code != '' THEN excluded.supplier_code ELSE suppliers.supplier_code END,
                        supplier_name = CASE WHEN excluded.supplier_name != '' THEN excluded.supplier_name ELSE suppliers.supplier_name END
                    """,
                    [
                        (
                            s["supplier_id"],
                            s["supplier_code"],
                            s["supplier_name"],
                            sup_anon_reg.get_or_assign(s["supplier_id"]),
                        )
                        for s in parsed_purch["suppliers"]
                    ],
                )

            # --- Master data: Customers (hợp nhất từ Sổ 131 + Sổ Bán) ---
            merged_customers: dict[str, dict[str, Any]] = {}
            for src in (parsed_ar["customers"] if parsed_ar else []) + (parsed_sales["customers"] if parsed_sales else []):
                cid = src["customer_id"]
                if cid not in merged_customers:
                    merged_customers[cid] = dict(src)
                else:
                    cur = merged_customers[cid]
                    if src.get("customer_code") and not cur.get("customer_code"):
                        cur["customer_code"] = src["customer_code"]
                    if src.get("customer_name") and (not cur.get("customer_name") or cur["customer_name"] == "Khách lẻ"):
                        cur["customer_name"] = src["customer_name"]
                    flags = set((cur.get("source_flags") or "").split(",")) | set((src.get("source_flags") or "").split(","))
                    cur["source_flags"] = ",".join(sorted(f for f in flags if f))
                    if src.get("first_date") and (not cur.get("first_date") or src["first_date"] < cur["first_date"]):
                        cur["first_date"] = src["first_date"]
                    if src.get("last_date") and (not cur.get("last_date") or src["last_date"] > cur["last_date"]):
                        cur["last_date"] = src["last_date"]

            if merged_customers:
                conn.executemany(
                    """
                    INSERT INTO customers (
                        customer_id, customer_code, customer_name, customer_anon, source_flags, first_date, last_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(customer_id) DO UPDATE SET
                        customer_code = CASE WHEN excluded.customer_code != '' THEN excluded.customer_code ELSE customers.customer_code END,
                        customer_name = CASE WHEN excluded.customer_name != '' THEN excluded.customer_name ELSE customers.customer_name END,
                        first_date = CASE
                            WHEN customers.first_date IS NULL THEN excluded.first_date
                            WHEN excluded.first_date IS NULL THEN customers.first_date
                            WHEN excluded.first_date < customers.first_date THEN excluded.first_date
                            ELSE customers.first_date
                        END,
                        last_date = CASE
                            WHEN customers.last_date IS NULL THEN excluded.last_date
                            WHEN excluded.last_date IS NULL THEN customers.last_date
                            WHEN excluded.last_date > customers.last_date THEN excluded.last_date
                            ELSE customers.last_date
                        END
                    """,
                    [
                        (
                            c["customer_id"],
                            c.get("customer_code") or "",
                            c.get("customer_name") or c["customer_id"],
                            cust_anon_reg.get_or_assign(c["customer_id"]),
                            c.get("source_flags") or "",
                            c.get("first_date"),
                            c.get("last_date"),
                        )
                        for c in merged_customers.values()
                    ],
                )

            # --- Transactional: Inventory Ledger (Date-Window Upsert) ---
            if parsed_inv_led and parsed_inv_led["rows"]:
                rows = parsed_inv_led["rows"]
                min_d, max_d = parsed_inv_led["meta"]["min_date"], parsed_inv_led["meta"]["max_date"]
                for r in rows:
                    if r["warehouse_code"]:
                        warehouses_seen.add(r["warehouse_code"])
                table_sync_stats["inventory_ledger"] = _window_stats(
                    conn, "inventory_ledger", "doc_date", min_d, max_d, len(rows)
                )
                conn.execute("DELETE FROM inventory_ledger WHERE doc_date >= ? AND doc_date <= ?", (min_d, max_d))
                conn.executemany(
                    """
                    INSERT INTO inventory_ledger (
                        warehouse_code, product_id, posting_date, doc_date, doc_id, doc_type,
                        uom, unit_cost, in_qty, in_value, out_qty, out_value, balance_qty, balance_value
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            r["warehouse_code"],
                            r["product_id"],
                            r["posting_date"],
                            r["doc_date"],
                            r["doc_id"],
                            r["doc_type"],
                            r["uom"],
                            r["unit_cost"],
                            r["in_qty"],
                            r["in_value"],
                            r["out_qty"],
                            r["out_value"],
                            r["balance_qty"],
                            r["balance_value"],
                        )
                        for r in rows
                    ],
                )

            # --- Transactional: Inventory Summary (File 2b fallback) ---
            if parsed_inv_sum and parsed_inv_sum["rows"]:
                rows = parsed_inv_sum["rows"]
                for r in rows:
                    if r["warehouse_code"]:
                        warehouses_seen.add(r["warehouse_code"])
                existing_cnt = int(conn.execute("SELECT COUNT(*) FROM inventory_summary").fetchone()[0] or 0)
                conn.executemany(
                    """
                    INSERT INTO inventory_summary (
                        warehouse_code, product_id, uom, opening_qty, opening_value,
                        in_qty, in_value, out_qty, out_value, closing_qty, closing_value,
                        selling_price_1, as_of_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(warehouse_code, product_id) DO UPDATE SET
                        uom = excluded.uom,
                        opening_qty = excluded.opening_qty,
                        opening_value = excluded.opening_value,
                        in_qty = excluded.in_qty,
                        in_value = excluded.in_value,
                        out_qty = excluded.out_qty,
                        out_value = excluded.out_value,
                        closing_qty = excluded.closing_qty,
                        closing_value = excluded.closing_value,
                        selling_price_1 = excluded.selling_price_1,
                        as_of_date = excluded.as_of_date
                    """,
                    [
                        (
                            r["warehouse_code"],
                            r["product_id"],
                            r["uom"],
                            r["opening_qty"],
                            r["opening_value"],
                            r["in_qty"],
                            r["in_value"],
                            r["out_qty"],
                            r["out_value"],
                            r["closing_qty"],
                            r["closing_value"],
                            r["selling_price_1"],
                            r["as_of_date"],
                        )
                        for r in rows
                    ],
                )
                new_cnt = int(conn.execute("SELECT COUNT(*) FROM inventory_summary").fetchone()[0] or 0)
                added_s = max(0, new_cnt - existing_cnt)
                table_sync_stats["inventory_summary"] = {
                    "kept_history_rows": max(0, existing_cnt - (len(rows) - added_s)),
                    "replaced_rows": len(rows) - added_s,
                    "incoming_rows": len(rows),
                    "updated_rows": len(rows) - added_s,
                    "added_rows": added_s,
                }

            # --- Transactional: Purchases (Date-Window Upsert) ---
            if parsed_purch and parsed_purch["details"]:
                details = parsed_purch["details"]
                inbounds = parsed_purch["inbounds"]
                min_d, max_d = parsed_purch["meta"]["min_date"], parsed_purch["meta"]["max_date"]
                table_sync_stats["purchases"] = _window_stats(
                    conn, "inbound_details", "inbound_date", min_d, max_d, len(details)
                )
                conn.execute("DELETE FROM inbound_details WHERE inbound_date >= ? AND inbound_date <= ?", (min_d, max_d))
                conn.execute("DELETE FROM inbounds WHERE inbound_date >= ? AND inbound_date <= ?", (min_d, max_d))
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO inbounds (
                        inbound_id, supplier_id, inbound_date, gross_amount, discount_amount, return_amount, net_amount
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            i["inbound_id"],
                            i["supplier_id"],
                            i["inbound_date"],
                            i["gross_amount"],
                            i["discount_amount"],
                            i["return_amount"],
                            i["net_amount"],
                        )
                        for i in inbounds
                    ],
                )
                conn.executemany(
                    """
                    INSERT INTO inbound_details (
                        inbound_id, supplier_id, inbound_date, product_id, uom,
                        qty_gross, qty_return, qty_net, unit_cost, gross_amount,
                        discount_amount, discount_rate, return_amount, net_amount, expiry_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            r["inbound_id"],
                            r["supplier_id"],
                            r["inbound_date"],
                            r["product_id"],
                            r["uom"],
                            r["qty_gross"],
                            r["qty_return"],
                            r["qty_net"],
                            r["unit_cost"],
                            r["gross_amount"],
                            r["discount_amount"],
                            r["discount_rate"],
                            r["return_amount"],
                            r["net_amount"],
                            r["expiry_date"],
                        )
                        for r in details
                    ],
                )

            # --- Transactional: Sales (Date-Window Upsert) ---
            if parsed_sales and parsed_sales["details"]:
                details = parsed_sales["details"]
                outbounds = parsed_sales["outbounds"]
                min_d, max_d = parsed_sales["meta"]["min_date"], parsed_sales["meta"]["max_date"]
                for r in details:
                    if r["warehouse_code"]:
                        warehouses_seen.add(r["warehouse_code"])
                table_sync_stats["sales"] = _window_stats(
                    conn, "outbound_details", "outbound_date", min_d, max_d, len(details)
                )
                conn.execute("DELETE FROM outbound_details WHERE outbound_date >= ? AND outbound_date <= ?", (min_d, max_d))
                conn.execute("DELETE FROM outbounds WHERE outbound_date >= ? AND outbound_date <= ?", (min_d, max_d))
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO outbounds (
                        outbound_id, customer_id, outbound_date, warehouse_code,
                        gross_revenue, discount_amount, return_amount, net_revenue, total_cogs, gross_profit
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            o["outbound_id"],
                            o["customer_id"],
                            o["outbound_date"],
                            o["warehouse_code"],
                            o["gross_revenue"],
                            o["discount_amount"],
                            o["return_amount"],
                            o["net_revenue"],
                            o["total_cogs"],
                            o["gross_profit"],
                        )
                        for o in outbounds
                    ],
                )
                conn.executemany(
                    """
                    INSERT INTO outbound_details (
                        outbound_id, customer_id, outbound_date, warehouse_code, product_id,
                        uom, base_uom, conversion_rate, qty_gross, qty_return, qty_net, base_qty_net,
                        unit_price, gross_revenue, discount_amount, return_amount, net_revenue, cogs, is_free_gift_line
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            r["outbound_id"],
                            r["customer_id"],
                            r["outbound_date"],
                            r["warehouse_code"],
                            r["product_id"],
                            r["uom"],
                            r["base_uom"],
                            r["conversion_rate"],
                            r["qty_gross"],
                            r["qty_return"],
                            r["qty_net"],
                            r["base_qty_net"],
                            r["unit_price"],
                            r["gross_revenue"],
                            r["discount_amount"],
                            r["return_amount"],
                            r["net_revenue"],
                            r["cogs"],
                            r["is_free_gift_line"],
                        )
                        for r in details
                    ],
                )

            # --- Transactional: AR Ledger (Date-Window Upsert + Global FIFO) ---
            if parsed_ar and parsed_ar["rows"]:
                rows = parsed_ar["rows"]
                min_d, max_d = parsed_ar["meta"]["min_date"], parsed_ar["meta"]["max_date"]
                table_sync_stats["ar_ledger"] = _window_stats(
                    conn, "ar_ledger", "doc_date", min_d, max_d, len(rows)
                )
                conn.execute("DELETE FROM ar_ledger WHERE doc_date >= ? AND doc_date <= ?", (min_d, max_d))
                conn.executemany(
                    """
                    INSERT INTO ar_ledger (
                        customer_id, posting_date, doc_date, doc_id, ar_account, contra_account,
                        debit_amount, credit_amount, balance_debit, balance_credit
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            r["customer_id"],
                            r["posting_date"],
                            r["doc_date"],
                            r["doc_id"],
                            r["ar_account"],
                            r["contra_account"],
                            r["debit_amount"],
                            r["credit_amount"],
                            r["balance_debit"],
                            r["balance_credit"],
                        )
                        for r in rows
                    ],
                )

            # --- Master data: Warehouses ---
            if warehouses_seen:
                conn.executemany(
                    """
                    INSERT INTO warehouses (warehouse_code, warehouse_name, warehouse_anon)
                    VALUES (?, ?, ?)
                    ON CONFLICT(warehouse_code) DO NOTHING
                    """,
                    [(wh, wh, wh_anon_reg.get_or_assign(wh)) for wh in sorted(warehouses_seen)],
                )

            # --- Cập nhật cờ has_stock_data chính xác (KHÔNG bịa tồn = 0 cho hàng thiếu sổ kho) ---
            conn.execute("UPDATE products SET has_stock_data = 0")
            conn.execute(
                """
                UPDATE products
                SET has_stock_data = 1
                WHERE product_id IN (
                    SELECT DISTINCT product_id FROM inventory_ledger
                    UNION
                    SELECT DISTINCT product_id FROM inventory_summary
                )
                """
            )

            # --- Đối trừ FIFO toàn cục trên sổ công nợ 131 ---
            fifo_stats = rebuild_fifo_invoices_and_payments(conn)

            # --- Xóa cache phân tích cũ vì dữ liệu mới vừa nạp ---
            conn.execute("DELETE FROM analysis_cache")

            # --- Cập nhật metadata ---
            date_bounds = conn.execute(
                """
                SELECT MIN(d), MAX(d) FROM (
                    SELECT outbound_date AS d FROM outbounds
                    UNION ALL SELECT inbound_date AS d FROM inbounds
                    UNION ALL SELECT doc_date AS d FROM inventory_ledger
                    UNION ALL SELECT doc_date AS d FROM ar_ledger
                ) WHERE d IS NOT NULL
                """
            ).fetchone()
            min_all, max_all = (date_bounds[0], date_bounds[1]) if date_bounds else (None, None)
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.executemany(
                "INSERT OR REPLACE INTO import_metadata (key, value) VALUES (?, ?)",
                [
                    ("min_date", str(min_all or "")),
                    ("max_date", str(max_all or "")),
                    ("last_sync_at", now_iso),
                    ("last_sync_mode", effective_mode),
                    ("is_demo_data", "1" if is_demo else "0"),
                ],
            )

        # --- Tổng hợp Báo cáo kiểm tra file .zip cho chủ kho ---
        total_kept = sum(v["kept_history_rows"] for v in table_sync_stats.values())
        total_updated = sum(v["updated_rows"] for v in table_sync_stats.values())
        total_added = sum(v["added_rows"] for v in table_sync_stats.values())

        total_skus = int(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] or 0)
        covered_skus = int(conn.execute("SELECT COUNT(*) FROM products WHERE has_stock_data = 1").fetchone()[0] or 0)
        sold_skus = int(conn.execute("SELECT COUNT(DISTINCT product_id) FROM outbound_details").fetchone()[0] or 0)
        coverage_pct = round((covered_skus / total_skus) * 100.0, 1) if total_skus > 0 else 0.0
        cov_pct_vi = str(coverage_pct).replace(".", ",")

        inv_led_cnt = int(conn.execute("SELECT COUNT(*) FROM inventory_ledger").fetchone()[0] or 0)
        inv_sum_cnt = int(conn.execute("SELECT COUNT(*) FROM inventory_summary").fetchone()[0] or 0)
        stock_source = "inventory_ledger" if inv_led_cnt > 0 else ("inventory_summary" if inv_sum_cnt > 0 else "none")

        files_breakdown_vi: list[str] = []
        if parsed_products:
            files_breakdown_vi.append(
                f"Danh mục hàng hóa ({roles['products'].name}): đã đọc đủ {len(parsed_products['rows']):,} mặt hàng.".replace(",", ".")
            )
        if parsed_sales:
            s_skus = len({r["product_id"] for r in parsed_sales["details"]})
            files_breakdown_vi.append(
                f"Sổ chi tiết bán hàng ({roles['sales'].name}): đã đọc {len(parsed_sales['details']):,} dòng bán ({s_skus:,} mặt hàng có bán ra — đều được tính lãi & dự báo đầy đủ).".replace(",", ".")
            )
        if parsed_purch:
            files_breakdown_vi.append(
                f"Sổ chi tiết mua hàng ({roles['purchases'].name}): đã đọc {len(parsed_purch['details']):,} dòng nhập từ {len(parsed_purch['suppliers']):,} nhà cung cấp.".replace(",", ".")
            )
        if parsed_ar:
            files_breakdown_vi.append(
                f"Sổ công nợ khách hàng ({roles['ar_ledger'].name}): đã đọc {len(parsed_ar['rows']):,} dòng của {len(parsed_ar['customers']):,} khách trên sổ.".replace(",", ".")
            )
        if parsed_inv_led:
            u_skus = parsed_inv_led["meta"]["unique_skus"]
            if total_skus > 0 and u_skus < total_skus * 0.25:
                files_breakdown_vi.append(
                    f"Sổ chi tiết vật tư ({roles['inventory_ledger'].name}): chỉ chứa {u_skus:,} mã lọc lẻ → Hệ thống tự chuyển sang đối chiếu trực tiếp từ Sổ Mua hàng & Sổ Bán hàng cho toàn bộ {total_skus:,} mặt hàng (không cần dùng file vật tư này).".replace(",", ".")
                )
            else:
                files_breakdown_vi.append(
                    f"Sổ chi tiết vật tư hàng hóa ({roles['inventory_ledger'].name}): có {len(parsed_inv_led['rows']):,} dòng của {u_skus:,} mặt hàng.".replace(",", ".")
                )
        elif parsed_inv_sum:
            files_breakdown_vi.append(
                f"Tổng hợp tồn kho ({roles['inventory_summary'].name}): có {parsed_inv_sum['meta']['unique_skus']:,} mặt hàng.".replace(",", ".")
            )
        else:
            files_breakdown_vi.append(
                f"Chế độ 4 sổ MISA gọn nhẹ: đối chiếu trực tiếp Nhập mua – Xuất bán & Dòng tiền cho toàn bộ {total_skus:,} mặt hàng (không cần Sổ chi tiết vật tư).".replace(",", ".")
            )

        # Chỉ yêu cầu 4 sổ MISA cốt lõi (Danh mục, Mua hàng, Bán hàng, Công nợ 131) — KHÔNG bắt buộc Sổ vật tư!
        missing_files: list[str] = []
        for req_role in ("products", "purchases", "sales", "ar_ledger"):
            if roles[req_role] is None:
                missing_files.append(FILE_LABELS_VI[req_role])

        warnings_vi: list[str] = []
        if effective_mode == "reset" and mode == "merge" and not is_demo:
            warnings_vi.append("Đã tự động dọn sạch dữ liệu mẫu dùng thử trước đó để không bị trộn lẫn với sổ MISA thật của bạn.")
        if missing_files:
            warnings_vi.append(f"Chưa thấy file: {', '.join(missing_files)}. Các mục còn lại vẫn xem bình thường.")
        if roles["inventory_ledger"] is None and roles["inventory_summary"] is not None:
            warnings_vi.append(
                "Đang dùng bảng Tổng hợp tồn kho: xem đầy đủ số tồn cuối kỳ và đủ bán mấy ngày."
            )
        if total_skus > 0 and (total_skus * 0.25) <= covered_skus < total_skus:
            warnings_vi.append(
                f"Bảng tồn kho hiện có {covered_skus:,}/{total_skus:,} mặt hàng ({cov_pct_vi}%). Các mặt hàng chưa có trong bảng tồn sẽ đối chiếu theo nhịp Mua – Bán.".replace(",", ".")
            )

        ar_meta = parsed_ar["meta"] if parsed_ar else None
        ar_checksum = {
            "checked": bool(ar_meta),
            "blocks_total": ar_meta["checksum_blocks_total"] if ar_meta else 0,
            "blocks_matched": ar_meta["checksum_blocks_matched"] if ar_meta else 0,
            "ok": (
                ar_meta["checksum_blocks_total"] == ar_meta["checksum_blocks_matched"]
                if ar_meta
                else True
            ),
        }
        if ar_checksum["checked"] and not ar_checksum["ok"]:
            warnings_vi.append(
                f"Sổ công nợ khớp {ar_checksum['blocks_matched']}/{ar_checksum['blocks_total']} khách hàng so với dòng Cộng của MISA."
            )

        return {
            "mode": effective_mode,
            "files_detected": {k: (v.name if v is not None else None) for k, v in roles.items()},
            "files_breakdown_vi": files_breakdown_vi,
            "missing_files_vi": missing_files,
            "date_range": {"min_date": min_all, "max_date": max_all},
            "sync_summary": {
                "kept_history_rows": total_kept,
                "updated_rows": total_updated,
                "added_rows": total_added,
                "by_table": table_sync_stats,
                "summary_vi": f"Giữ nguyên {total_kept:,} dòng lịch sử cũ · Cập nhật {total_updated:,} dòng trong kỳ · Thêm mới {total_added:,} dòng.".replace(",", "."),
            },
            "ar_checksum": ar_checksum,
            "fifo": fifo_stats,
            "stock_coverage": {
                "covered_skus": covered_skus,
                "sold_skus": sold_skus,
                "total_skus": total_skus,
                "coverage_pct": coverage_pct,
                "flow_mode_active": (covered_skus == 0) or (total_skus > 0 and covered_skus < total_skus * 0.25),
                "source": (
                    "purchase_sales_flow"
                    if ((covered_skus == 0) or (total_skus > 0 and covered_skus < total_skus * 0.25))
                    else stock_source
                ),
                "label_vi": (
                    f"Đối chiếu trực tiếp Mua – Bán trên toàn bộ {total_skus:,} mặt hàng ({sold_skus:,} mã phát sinh bán)".replace(",", ".")
                    if ((covered_skus == 0) or (total_skus > 0 and covered_skus < total_skus * 0.25))
                    else f"{covered_skus:,}/{total_skus:,} mặt hàng có sổ tồn ({cov_pct_vi}%) · {sold_skus:,} mã có bán".replace(",", ".")
                ),
            },
            "warnings_vi": warnings_vi,
        }
    finally:
        conn.close()


def sync_misa_zip(
    zip_path: str | Path,
    db_path: str | Path,
    mode: str = "merge",
    is_demo: bool = False,
) -> dict[str, Any]:
    """Giải nén an toàn file `.zip` MISA vào thư mục tạm và đồng bộ vào SQLite."""
    with tempfile.TemporaryDirectory(prefix="misa_zip_") as tmp:
        extracted = extract_zip_safely(zip_path, tmp)
        if not extracted:
            raise ValueError("Không tìm thấy file Excel (.xlsx) nào bên trong tệp .zip.")
        report = sync_misa_directory(tmp, db_path, mode=mode, is_demo=is_demo)
        report["zip_filename"] = Path(zip_path).name
        report["xlsx_files_count"] = len(extracted)
        return report

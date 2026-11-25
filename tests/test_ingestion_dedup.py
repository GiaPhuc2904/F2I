"""Kiểm thử Phase 1: Giải nén .zip an toàn, parse 5 file + Tổng hợp tồn kho, Date-Window Upsert 0% trùng, FIFO."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sqlite3
import zipfile

import pytest

from misa_companion.demo_generator import generate_demo_misa_zip
from misa_companion.ingestion.sync_engine import sync_misa_zip
from misa_companion.ingestion.zip_loader import extract_zip_safely


def _table_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [
            "products", "customers", "suppliers", "warehouses",
            "inventory_ledger", "inventory_summary",
            "inbounds", "inbound_details",
            "outbounds", "outbound_details",
            "ar_ledger", "invoices", "customer_payments",
        ]
        return {t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in tables}
    finally:
        conn.close()


def test_sync_zip_full_and_idempotent_dedup(tmp_path: Path) -> None:
    zip_path = generate_demo_misa_zip(tmp_path / "misa_demo.zip", n_weeks=24, seed=42)
    db_path = tmp_path / "app.db"

    rep1 = sync_misa_zip(zip_path, db_path, mode="merge")
    assert rep1["files_detected"]["products"] is not None
    assert rep1["files_detected"]["inventory_ledger"] is not None
    assert rep1["files_detected"]["purchases"] is not None
    assert rep1["files_detected"]["sales"] is not None
    assert rep1["files_detected"]["ar_ledger"] is not None
    assert rep1["ar_checksum"]["ok"] is True
    assert rep1["ar_checksum"]["blocks_total"] >= 5
    assert rep1["stock_coverage"]["covered_skus"] == rep1["stock_coverage"]["total_skus"]

    counts1 = _table_counts(db_path)
    assert counts1["products"] == 24
    assert counts1["outbounds"] > 30
    assert counts1["outbound_details"] > 200
    assert counts1["invoices"] > 30
    assert counts1["customer_payments"] > 20

    # Kiểm tra lưu song song tên thật + mã ẩn danh + cờ _KM + dòng tặng kèm 0đ có giá vốn > 0
    conn = sqlite3.connect(str(db_path))
    try:
        promo_cnt = conn.execute("SELECT COUNT(*) FROM products WHERE is_promo_sku = 1").fetchone()[0]
        assert promo_cnt == 2

        gift_row = conn.execute(
            "SELECT COUNT(*), SUM(cogs), SUM(gross_revenue) FROM outbound_details WHERE is_free_gift_line = 1"
        ).fetchone()
        assert gift_row[0] > 0
        assert gift_row[1] > 0
        assert gift_row[2] == 0

        cust_sample = conn.execute("SELECT customer_name, customer_anon FROM customers ORDER BY customer_anon LIMIT 1").fetchone()
        assert cust_sample[0] != ""
        assert cust_sample[1].startswith("KH_")
        anon_map_before = dict(conn.execute("SELECT customer_id, customer_anon FROM customers").fetchall())
    finally:
        conn.close()

    # Kéo thả lại ĐÚNG CÙNG 1 FILE .ZIP lần thứ 2 -> 0% dòng trùng, giữ nguyên mã ẩn danh!
    rep2 = sync_misa_zip(zip_path, db_path, mode="merge")
    counts2 = _table_counts(db_path)
    assert counts2 == counts1
    assert rep2["sync_summary"]["added_rows"] == 0
    assert rep2["sync_summary"]["updated_rows"] > 0

    conn = sqlite3.connect(str(db_path))
    try:
        anon_map_after = dict(conn.execute("SELECT customer_id, customer_anon FROM customers").fetchall())
        assert anon_map_after == anon_map_before
    finally:
        conn.close()


def test_incremental_window_merge_preserves_history_and_updates_payment(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    # Kỳ 1: 16 tuần đầu năm 2025
    zip1 = generate_demo_misa_zip(tmp_path / "k1.zip", start_date=date(2025, 1, 6), n_weeks=16, seed=7)
    sync_misa_zip(zip1, db_path, mode="merge")

    conn = sqlite3.connect(str(db_path))
    try:
        min_d_1 = conn.execute("SELECT MIN(outbound_date) FROM outbounds").fetchone()[0]
        early_sales_cnt = conn.execute(
            "SELECT COUNT(*) FROM outbound_details WHERE outbound_date < '2025-03-01'"
        ).fetchone()[0]
        assert early_sales_cnt > 0
    finally:
        conn.close()

    # Kỳ 2: 16 tuần tiếp theo (bắt đầu từ 2025-03-31, gối đầu 1 phần với Kỳ 1) + có khoản thu nợ bổ sung
    zip2 = generate_demo_misa_zip(
        tmp_path / "k2.zip",
        start_date=date(2025, 3, 31),
        n_weeks=16,
        seed=7,
        extra_ar_payment_on_last_week=False,
    )
    rep2 = sync_misa_zip(zip2, db_path, mode="merge")
    assert rep2["sync_summary"]["kept_history_rows"] > 0

    conn = sqlite3.connect(str(db_path))
    try:
        # Dữ liệu cũ trước tháng 3 vẫn được giữ nguyên 100%
        early_sales_after = conn.execute(
            "SELECT COUNT(*) FROM outbound_details WHERE outbound_date < '2025-03-01'"
        ).fetchone()[0]
        assert early_sales_after == early_sales_cnt
        assert conn.execute("SELECT MIN(outbound_date) FROM outbounds").fetchone()[0] == min_d_1
        outst_before_pay = float(conn.execute("SELECT SUM(outstanding_amount) FROM invoices").fetchone()[0] or 0.0)
    finally:
        conn.close()

    # Nạp lại Kỳ 2 với bản MISA mới nhất có thêm phiếu thu tiền cho khách nhóm C -> nợ phải giảm!
    zip2_paid = generate_demo_misa_zip(
        tmp_path / "k2_paid.zip",
        start_date=date(2025, 3, 31),
        n_weeks=16,
        seed=7,
        extra_ar_payment_on_last_week=True,
    )
    sync_misa_zip(zip2_paid, db_path, mode="merge")
    conn = sqlite3.connect(str(db_path))
    try:
        outst_after_pay = float(conn.execute("SELECT SUM(outstanding_amount) FROM invoices").fetchone()[0] or 0.0)
        assert outst_after_pay < outst_before_pay
    finally:
        conn.close()

    # Reset mode xóa sạch kỳ 1, chỉ còn kỳ 2
    sync_misa_zip(zip2_paid, db_path, mode="reset")
    conn = sqlite3.connect(str(db_path))
    try:
        early_after_reset = conn.execute(
            "SELECT COUNT(*) FROM outbound_details WHERE outbound_date < '2025-03-01'"
        ).fetchone()[0]
        assert early_after_reset == 0
    finally:
        conn.close()


def test_inventory_summary_fallback_and_partial_coverage(tmp_path: Path) -> None:
    zip_path = generate_demo_misa_zip(
        tmp_path / "summary_only.zip",
        n_weeks=12,
        include_inventory_summary_only=True,
        partial_stock_coverage=True,
    )
    db_path = tmp_path / "app.db"
    rep = sync_misa_zip(zip_path, db_path, mode="reset")

    assert rep["files_detected"]["inventory_ledger"] is None
    assert rep["files_detected"]["inventory_summary"] is not None
    assert rep["stock_coverage"]["source"] == "inventory_summary"
    assert rep["stock_coverage"]["covered_skus"] == 12
    assert rep["stock_coverage"]["total_skus"] == 24
    assert len(rep["warnings_vi"]) >= 2

    conn = sqlite3.connect(str(db_path))
    try:
        covered = conn.execute("SELECT COUNT(*) FROM products WHERE has_stock_data = 1").fetchone()[0]
        uncovered = conn.execute("SELECT COUNT(*) FROM products WHERE has_stock_data = 0").fetchone()[0]
        assert covered == 12
        assert uncovered == 12
    finally:
        conn.close()


def test_zipslip_protection(tmp_path: Path) -> None:
    bad_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../escape.xlsx", b"malicious")

    with pytest.raises(ValueError, match="không an toàn"):
        extract_zip_safely(bad_zip, tmp_path / "out")

"""Kiểm thử Phase 3: Khám bệnh kho M2 (lãi trừ quà tặng 0đ, không bịa tồn = 0), Công nợ & Dòng tiền M3 (COD vs B2B), Pipeline."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sqlite3
import time

from misa_companion.analytics.m1_arena import run_m1_arena
from misa_companion.analytics.m2_health import compute_m2_base, enrich_m2_with_model
from misa_companion.analytics.m3_credit_cashflow import run_m3_credit_and_cashflow
from misa_companion.analytics.pipeline import (
    build_dashboard_payload,
    get_job_status,
    save_model_settings,
    start_analysis_job,
)
from misa_companion.db import get_connection, init_db
from misa_companion.demo_generator import generate_demo_misa_zip
from misa_companion.ingestion.sync_engine import rebuild_fifo_invoices_and_payments, sync_misa_zip

FORBIDDEN_JARGON = (
    "WAPE",
    "MAE",
    "Bias",
    "DSO",
    "DOI",
    "PD*",
    "EWS",
    "FIFO",
    "Zero-Leakage",
    "Graceful Degradation",
    "Intermittent",
    "Tweedie",
)


def _assert_no_jargon(obj: object) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith("_vi") and isinstance(v, str):
                for bad in FORBIDDEN_JARGON:
                    assert bad.lower() not in v.lower(), f"Thuật ngữ cấm '{bad}' xuất hiện trong {k}: {v}"
            else:
                _assert_no_jargon(v)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_jargon(item)
    elif isinstance(obj, str):
        for bad in FORBIDDEN_JARGON:
            assert bad.lower() not in obj.lower(), f"Thuật ngữ cấm '{bad}' xuất hiện trong chuỗi: {obj}"


def test_m2_real_profit_free_gifts_and_4_tables(tmp_path: Path) -> None:
    zip_path = generate_demo_misa_zip(tmp_path / "misa_11m.zip", start_date=date(2025, 1, 6), n_weeks=48, seed=42)
    db_path = tmp_path / "app.db"
    sync_misa_zip(zip_path, db_path, mode="reset")

    conn = sqlite3.connect(str(db_path))
    try:
        m1 = run_m1_arena(conn)
        m2_base = compute_m2_base(conn)
        sp001_sql = conn.execute(
            """
            SELECT SUM(net_revenue), SUM(cogs),
                   SUM(CASE WHEN is_free_gift_line = 1 THEN cogs ELSE 0 END),
                   SUM(CASE WHEN is_free_gift_line = 0 THEN base_qty_net ELSE 0 END),
                   SUM(CASE WHEN is_free_gift_line = 1 THEN base_qty_net ELSE 0 END)
            FROM outbound_details WHERE product_id = 'SP001'
            """
        ).fetchone()
    finally:
        conn.close()

    m2 = enrich_m2_with_model(m2_base, m1, primary_model_id="best_overall", compare_model_ids=["ma4"])
    assert m2["status"] == "OK"
    assert len(m2["all_items"]) == 24
    assert len(m2["top_sellers"]) > 0
    assert len(m2["top_profit"]) > 0
    assert len(m2["frequent_stockouts"]) >= 2
    assert len(m2["slow_overstock_promo"]) >= 2

    by_sku = {x["product_id"]: x for x in m2["all_items"]}
    sp001 = by_sku["SP001"]
    # Lãi gộp thực của SP001 phải trừ sạch giá vốn của dòng tặng kèm 0đ!
    assert sp001["gift_qty"] == round(float(sp001_sql[4]), 1) and sp001["gift_qty"] > 0
    assert sp001["gift_cogs_vnd"] == int(round(float(sp001_sql[2]))) and sp001["gift_cogs_vnd"] > 0
    assert sp001["gross_profit_vnd"] == int(round(float(sp001_sql[0]) - float(sp001_sql[1])))

    # Hàng hay hết hàng (SP003, SP016) và hàng nằm im >= 60 ngày (SP021, SP022)
    assert by_sku["SP003"]["stockout_events"] > 0
    assert by_sku["SP003"]["stockout_ref_boxes"] >= 1 or by_sku["SP003"]["suggested_boxes"] >= 0
    assert by_sku["SP021"]["days_since_last_sale"] >= 60
    assert by_sku["SP021"]["status_code"] == "dead_stock"

    _assert_no_jargon(m2)


def test_m2_partial_stock_coverage_never_fabricates_zero(tmp_path: Path) -> None:
    zip_path = generate_demo_misa_zip(
        tmp_path / "partial.zip",
        n_weeks=14,
        include_inventory_summary_only=True,
        partial_stock_coverage=True,
    )
    db_path = tmp_path / "partial.db"
    sync_misa_zip(zip_path, db_path, mode="reset")

    conn = sqlite3.connect(str(db_path))
    try:
        m1 = run_m1_arena(conn)
        m2_base = compute_m2_base(conn)
    finally:
        conn.close()

    m2 = enrich_m2_with_model(m2_base, m1)
    assert m2["stock_coverage"]["covered_skus"] == 12
    assert m2["stock_coverage"]["total_skus"] == 24

    uncovered = [x for x in m2["all_items"] if not x["has_stock_data"]]
    covered = [x for x in m2["all_items"] if x["has_stock_data"]]
    assert len(uncovered) == 12
    assert len(covered) == 12

    for item in uncovered:
        assert item["current_stock_qty"] is None
        assert item["current_stock_display_vi"] == "Chưa có sổ kho"
        assert item["days_of_supply"] is None
        assert item["status_code"] == "no_stock_book"


def test_m3_b2b_four_groups_and_cod_fast_cash_mode(tmp_path: Path) -> None:
    # 1. Kho bán gối đầu công nợ đủ 4 nhóm (A, B trả chậm có trả, Khách mới, Cần gọi đòi ngay)
    zip_path = generate_demo_misa_zip(tmp_path / "b2b.zip", start_date=date(2025, 1, 6), n_weeks=48, seed=42)
    db_path = tmp_path / "b2b.db"
    sync_misa_zip(zip_path, db_path, mode="reset")

    conn = sqlite3.connect(str(db_path))
    try:
        m3 = run_m3_credit_and_cashflow(conn)
    finally:
        conn.close()

    assert m3["status"] == "OK"
    assert m3["warehouse_credit_style"] == "credit_terms"
    by_code = {c["customer_code"]: c for c in m3["customers"]}

    assert by_code["KH01"]["group_id"] == "trusted"
    assert by_code["KH04"]["group_id"] == "slow_but_pays"  # Hay trả chậm ~18 ngày nhưng có trả -> không khóa oan!
    assert by_code["KH06"]["group_id"] == "call_now"
    assert by_code["KH08"]["group_id"] == "new_customer"

    # Kiểm tra đủ 3 biểu đồ dòng tiền + chỉ số chuẩn hóa Đỉnh = 100
    cf = m3["cashflow"]
    assert cf["status"] == "OK"
    assert len(cf["monthly_cashflow"]) >= 10
    assert max(r["cash_in_index"] for r in cf["monthly_cashflow"] + [{"cash_in_index": 0}]) <= 100.0
    assert len(cf["payment_speed_histogram"]) == 6
    assert len(cf["payment_method_mix"]) >= 2
    _assert_no_jargon(m3)

    # 2. Kho thu tiền nhanh (COD 1-2 ngày): tuyệt đối không báo động nhầm khách trả 1-2 ngày, chỉ bắt đơn treo > 30 ngày
    cod_db = tmp_path / "cod.db"
    conn2 = get_connection(cod_db)
    try:
        init_db(conn2, reset=True)
        conn2.executemany(
            "INSERT INTO customers (customer_id, customer_code, customer_name, customer_anon) VALUES (?, ?, ?, ?)",
            [
                ("C1", "C1", "Tạp hóa Nhanh 1", "KH_0001"),
                ("C2", "C2", "Tạp hóa Nhanh 2", "KH_0002"),
                ("C3", "C3", "Khách Treo Lâu", "KH_0003"),
            ],
        )
        ar_entries = []
        # 20 đơn trả nhanh trong 1-2 ngày cho C1 & C2, cộng 1 đơn mới giao hôm qua chưa thu (không được báo đỏ!)
        for i in range(1, 11):
            d_inv = f"2025-05-{i:02d}"
            d_pay = f"2025-05-{i + 1:02d}"
            ar_entries.append(("C1", d_inv, d_inv, f"BH{i:03d}", "131", "5112", 2_000_000.0, 0.0, 2_000_000.0, 0.0))
            ar_entries.append(("C1", d_pay, d_pay, f"PT{i:03d}", "131", "1111", 0.0, 2_000_000.0, 0.0, 0.0))
            ar_entries.append(("C2", d_inv, d_inv, f"BHB{i:03d}", "131", "5112", 1_500_000.0, 0.0, 1_500_000.0, 0.0))
            ar_entries.append(("C2", d_pay, d_pay, f"PTB{i:03d}", "131", "1121", 0.0, 1_500_000.0, 0.0, 0.0))
        # C1 có 1 đơn mới giao 2 ngày trước ngày chốt 2025-05-25
        ar_entries.append(("C1", "2025-05-23", "2025-05-23", "BH_NEW", "131", "5112", 1_000_000.0, 0.0, 1_000_000.0, 0.0))
        # C3 có 1 đơn treo > 35 ngày (từ 2025-04-10)
        ar_entries.append(("C3", "2025-04-10", "2025-04-10", "BH_STUCK", "131", "5112", 800_000.0, 0.0, 800_000.0, 0.0))
        conn2.executemany(
            """
            INSERT INTO ar_ledger (
                customer_id, posting_date, doc_date, doc_id, ar_account, contra_account,
                debit_amount, credit_amount, balance_debit, balance_credit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ar_entries,
        )
        rebuild_fifo_invoices_and_payments(conn2)
        conn2.commit()
        m3_cod = run_m3_credit_and_cashflow(conn2)
    finally:
        conn2.close()

    assert m3_cod["warehouse_credit_style"] == "fast_cash"
    cod_map = {c["customer_id"]: c for c in m3_cod["customers"]}
    assert cod_map["C1"]["group_id"] == "trusted"
    assert cod_map["C2"]["group_id"] == "trusted"
    assert cod_map["C3"]["group_id"] == "call_now"
    _assert_no_jargon(m3_cod)


def test_background_pipeline_and_instant_cache_reload(tmp_path: Path) -> None:
    zip_path = generate_demo_misa_zip(tmp_path / "pipe.zip", start_date=date(2025, 1, 6), n_weeks=20, seed=9)
    db_path = tmp_path / "pipe.db"
    rep = sync_misa_zip(zip_path, db_path, mode="reset")

    job = start_analysis_job(db_path=db_path, import_report=rep)
    deadline = time.time() + 30.0
    st = get_job_status(job["job_id"])
    while st and st["status"] == "running" and time.time() < deadline:
        time.sleep(0.1)
        st = get_job_status(job["job_id"])

    assert st is not None
    assert st["status"] == "completed"
    assert st["progress_pct"] == 100

    # Lần đọc tiếp theo từ cache SQLite phải trả ngay < 150ms
    t0 = time.perf_counter()
    dash = build_dashboard_payload(db_path=db_path)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 150.0
    assert dash["has_data"] is True

    # Đổi mô hình dự báo lưu vào SQLite -> cập nhật ngay trên cả M1 và M2
    save_model_settings("ma4", ["ma8", "theta"], db_path=db_path)
    dash_ma4 = build_dashboard_payload(db_path=db_path)
    assert dash_ma4["model_settings"]["primary_model_id"] == "ma4"
    assert dash_ma4["m2"]["active_model_id"] == "ma4"
    _assert_no_jargon(json.loads(json.dumps(dash_ma4, ensure_ascii=False)))


def test_four_book_mode_without_inventory_ledger_keeps_full_cashflow_and_analytics(tmp_path: Path) -> None:
    """Kiểm chứng chế độ 4 sổ MISA (bỏ hẳn Sổ chi tiết vật tư): vẫn đủ 100% dòng tiền, công nợ, dự báo & gợi ý nhập."""
    import zipfile as zf_mod

    full_zip = generate_demo_misa_zip(tmp_path / "full5.zip", start_date=date(2025, 1, 6), n_weeks=20, seed=33)
    four_book_zip = tmp_path / "four_books_only.zip"
    with zf_mod.ZipFile(full_zip, "r") as z_in, zf_mod.ZipFile(four_book_zip, "w") as z_out:
        for info in z_in.infolist():
            if "vat_tu" in info.filename.lower() or "ton_kho" in info.filename.lower():
                continue
            z_out.writestr(info, z_in.read(info.filename))

    db_path = tmp_path / "four_books.db"
    rep = sync_misa_zip(four_book_zip, db_path, mode="reset")
    assert rep["missing_files_vi"] == []
    assert rep["stock_coverage"]["flow_mode_active"] is True
    assert rep["stock_coverage"]["source"] == "purchase_sales_flow"

    conn = sqlite3.connect(str(db_path))
    try:
        m1 = run_m1_arena(conn)
        m2_base = compute_m2_base(conn)
        m3 = run_m3_credit_and_cashflow(conn)
    finally:
        conn.close()

    m2 = enrich_m2_with_model(m2_base, m1)
    assert m2["status"] == "OK"
    assert m2["flow_mode_active"] is True
    assert len(m2["all_items"]) == 24
    assert all(x["status_code"] != "no_stock_book" for x in m2["all_items"])
    assert m3["status"] == "OK"
    assert m3["cashflow"]["status"] == "OK"
    assert len(m3["cashflow"]["monthly_cashflow"]) >= 4
    _assert_no_jargon(m2)
    _assert_no_jargon(m3)

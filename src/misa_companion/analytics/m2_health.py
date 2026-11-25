"""Module 2: Khám bệnh kho & Gợi ý từng mặt hàng (chạy trọn vẹn từ 4 sổ MISA không cần Sổ chi tiết vật tư, hoặc kết hợp Sổ tồn nếu có).

Nguyên tắc cốt lõi:
- Chế độ 4 sổ MISA (Danh mục + Mua hàng + Bán hàng + Công nợ): không cần `So_chi_tiet_vat_tu_hang_hoa.xlsx`! Tự động đối chiếu các đợt Nhập mua gần nhất với lượng Xuất bán và Dự báo M1 để tính nhịp tiêu thụ, phát hiện món sắp cạn đợt nhập cần đặt tiếp, món nằm im lâu (≥ 60 ngày) và hàng KM.
- Nếu người dùng có nạp bảng Tồn kho đủ độ phủ (>= 25% danh mục), hệ thống dùng tồn cuối kỳ thực tế; mặt hàng thiếu sổ tồn giữ nguyên 'Chưa có sổ kho' (tuyệt đối không bịa tồn = 0).
- Lợi nhuận thực tế từng mặt hàng = Doanh thu thuần - Tổng giá vốn (ĐÃ TRỪ giá vốn dòng quà tặng 0đ); tự tách riêng mặt hàng chưa tính giá vốn trên MISA (`Giá vốn = 0`) khỏi bảng Top Lợi nhuận để tránh báo lãi ảo 100%.
"""

from __future__ import annotations

from datetime import date, timedelta
import math
import sqlite3
from typing import Any

import pandas as pd


def _fmt_qty(val: float) -> str:
    if val >= 10 or abs(val - round(val)) < 0.05:
        return f"{round(val):,.0f}".replace(",", ".")
    s = f"{val:,.1f}".rstrip("0").rstrip(".")
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def compute_m2_base(conn: sqlite3.Connection) -> dict[str, Any]:
    """Tính toán nền tảng mua - bán - lãi thực tế và tồn kho (nếu có) cho toàn bộ mặt hàng."""
    tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "products" not in tables:
        return {
            "status": "NO_PRODUCTS",
            "message_vi": "Chưa có danh mục mặt hàng. Hãy nạp tệp .zip xuất từ MISA.",
            "as_of_date": None,
            "flow_mode_active": True,
            "total_purchase_net_vnd": 0,
            "stock_coverage": {"covered_skus": 0, "total_skus": 0, "coverage_pct": 0.0, "source": "purchase_sales_flow"},
            "sku_base_rows": [],
        }

    prods = pd.read_sql_query(
        """
        SELECT p.product_id, p.sku, p.product_name, p.sku_anon, p.category,
               p.warehouse_code, COALESCE(w.warehouse_anon, 'KHO_01') AS warehouse_anon,
               p.base_uom, p.box_uom, p.units_per_box, p.is_promo_sku,
               p.latest_purchase_cost, p.selling_price_1, p.min_stock_qty, p.has_stock_data
        FROM products p
        LEFT JOIN warehouses w ON p.warehouse_code = w.warehouse_code
        ORDER BY p.sku
        """,
        conn,
    )
    if prods.empty:
        return {
            "status": "NO_PRODUCTS",
            "message_vi": "Chưa có mặt hàng nào trong dữ liệu.",
            "as_of_date": None,
            "flow_mode_active": True,
            "total_purchase_net_vnd": 0,
            "stock_coverage": {"covered_skus": 0, "total_skus": 0, "coverage_pct": 0.0, "source": "purchase_sales_flow"},
            "sku_base_rows": [],
        }

    sales_agg: dict[str, dict[str, Any]] = {}
    max_dates: list[str] = []
    min_dates: list[str] = []

    if "outbound_details" in tables:
        s_df = pd.read_sql_query(
            """
            SELECT product_id,
                   MIN(outbound_date) AS first_sale_date,
                   MAX(CASE WHEN base_qty_net > 0 THEN outbound_date ELSE NULL END) AS last_sale_date,
                   SUM(CASE WHEN is_free_gift_line = 0 AND base_qty_net > 0 THEN base_qty_net ELSE 0.0 END) AS paid_qty,
                   SUM(CASE WHEN is_free_gift_line = 1 AND base_qty_net > 0 THEN base_qty_net ELSE 0.0 END) AS gift_qty,
                   SUM(CASE WHEN base_qty_net > 0 THEN base_qty_net ELSE 0.0 END) AS total_out_qty,
                   SUM(net_revenue) AS net_revenue,
                   SUM(cogs) AS total_cogs,
                   SUM(CASE WHEN is_free_gift_line = 1 THEN cogs ELSE 0.0 END) AS gift_cogs
            FROM outbound_details
            GROUP BY product_id
            """,
            conn,
        )
        for r in s_df.itertuples(index=False):
            pid = str(r.product_id)
            if r.last_sale_date:
                max_dates.append(str(r.last_sale_date))
            if r.first_sale_date:
                min_dates.append(str(r.first_sale_date))
            sales_agg[pid] = {
                "first_sale_date": str(r.first_sale_date) if r.first_sale_date else None,
                "last_sale_date": str(r.last_sale_date) if r.last_sale_date else None,
                "paid_qty": float(r.paid_qty or 0.0),
                "gift_qty": float(r.gift_qty or 0.0),
                "total_out_qty": float(r.total_out_qty or 0.0),
                "net_revenue": float(r.net_revenue or 0.0),
                "total_cogs": float(r.total_cogs or 0.0),
                "gift_cogs": float(r.gift_cogs or 0.0),
            }

    # Tổng hợp Sổ mua hàng (`inbound_details`) để đối chiếu đợt nhập gần nhất khi không dùng Sổ vật tư
    purch_agg: dict[str, dict[str, Any]] = {}
    total_purchase_net_vnd = 0
    if "inbounds" in tables:
        p_sum = conn.execute("SELECT SUM(net_amount) FROM inbounds").fetchone()
        total_purchase_net_vnd = int(round(float(p_sum[0] or 0.0))) if p_sum else 0

    if "inbound_details" in tables:
        inb_df = pd.read_sql_query(
            """
            SELECT product_id, inbound_date,
                   SUM(CASE WHEN qty_net > 0 THEN qty_net ELSE 0.0 END) AS day_in_qty,
                   SUM(net_amount) AS day_net_amt,
                   AVG(CASE WHEN unit_cost > 0 THEN unit_cost ELSE NULL END) AS avg_cost
            FROM inbound_details
            GROUP BY product_id, inbound_date
            ORDER BY product_id, inbound_date
            """,
            conn,
        )
        if not inb_df.empty:
            max_dates.append(str(inb_df["inbound_date"].max()))
            min_dates.append(str(inb_df["inbound_date"].min()))
            for pid_raw, grp in inb_df.groupby("product_id", sort=False):
                pid = str(pid_raw)
                tot_in = float(grp["day_in_qty"].sum())
                tot_in_amt = float(grp["day_net_amt"].sum())
                pos_rows = grp[grp["day_in_qty"] > 0]
                if not pos_rows.empty:
                    last_in_r = pos_rows.iloc[-1]
                    last_in_d = str(last_in_r["inbound_date"])
                    last_in_q = float(last_in_r["day_in_qty"])
                    unit_c_in = float(pos_rows["avg_cost"].dropna().iloc[-1]) if pos_rows["avg_cost"].notna().any() else 0.0
                else:
                    last_in_d = None
                    last_in_q = 0.0
                    unit_c_in = 0.0
                purch_agg[pid] = {
                    "total_in_qty": tot_in,
                    "total_in_amount_vnd": tot_in_amt,
                    "last_inbound_date": last_in_d,
                    "last_inbound_qty": last_in_q,
                    "inbound_batches_count": int(len(pos_rows)),
                    "latest_inbound_cost": unit_c_in,
                }

    # Tính lượng bán ra kể từ ngày nhập hàng gần nhất của từng SKU (`sold_since_last_inbound`)
    sold_since_last_inb: dict[str, float] = {}
    if purch_agg and "outbound_details" in tables:
        out_daily = pd.read_sql_query(
            """
            SELECT product_id, outbound_date,
                   SUM(CASE WHEN base_qty_net > 0 THEN base_qty_net ELSE 0.0 END) AS day_out_qty
            FROM outbound_details
            GROUP BY product_id, outbound_date
            """,
            conn,
        )
        if not out_daily.empty:
            for pid_raw, grp in out_daily.groupby("product_id", sort=False):
                pid = str(pid_raw)
                last_in_d = purch_agg.get(pid, {}).get("last_inbound_date")
                if last_in_d:
                    sold_after = float(grp.loc[grp["outbound_date"] >= last_in_d, "day_out_qty"].sum())
                    sold_since_last_inb[pid] = sold_after

    # Sổ chi tiết vật tư / Tổng hợp tồn kho (tùy chọn — nếu chỉ xuất lẻ < 25% danh mục thì tự chuyển sang chế độ Mua-Bán 4 sổ)
    led_df = (
        pd.read_sql_query(
            """
            SELECT ledger_id, warehouse_code, product_id, doc_date, in_qty, out_qty, balance_qty, balance_value
            FROM inventory_ledger
            ORDER BY product_id, doc_date, ledger_id
            """,
            conn,
        )
        if "inventory_ledger" in tables
        else pd.DataFrame()
    )
    if not led_df.empty:
        max_dates.append(str(led_df["doc_date"].max()))
        min_dates.append(str(led_df["doc_date"].min()))

    sum_df = (
        pd.read_sql_query(
            """
            SELECT product_id,
                   SUM(closing_qty) AS closing_qty,
                   SUM(closing_value) AS closing_value,
                   SUM(out_qty) AS out_qty,
                   MAX(as_of_date) AS as_of_date
            FROM inventory_summary
            GROUP BY product_id
            """,
            conn,
        )
        if "inventory_summary" in tables
        else pd.DataFrame()
    )
    if not sum_df.empty and sum_df["as_of_date"].notna().any():
        max_dates.extend(sum_df["as_of_date"].dropna().astype(str).tolist())

    as_of_str = max(max_dates) if max_dates else date.today().isoformat()
    first_global_str = min(min_dates) if min_dates else as_of_str
    as_of_dt = date.fromisoformat(as_of_str)
    first_global_dt = date.fromisoformat(first_global_str)
    global_span_days = max(1, (as_of_dt - first_global_dt).days + 1)
    window_60_str = (as_of_dt - timedelta(days=60)).isoformat()

    recent_sales_60d: dict[str, float] = {}
    if "outbound_details" in tables:
        for pid_r, qty_r in conn.execute(
            """
            SELECT product_id, SUM(CASE WHEN base_qty_net > 0 THEN base_qty_net ELSE 0.0 END)
            FROM outbound_details
            WHERE outbound_date >= ?
            GROUP BY product_id
            """,
            (window_60_str,),
        ).fetchall():
            recent_sales_60d[str(pid_r)] = float(qty_r or 0.0)

    total_skus_cnt = len(prods)
    raw_covered_ids = set(led_df["product_id"].astype(str).unique().tolist() if not led_df.empty else []) | set(
        sum_df["product_id"].astype(str).unique().tolist() if not sum_df.empty else []
    )
    flow_mode_active = (len(raw_covered_ids) == 0) or (len(raw_covered_ids) < total_skus_cnt * 0.25)

    ledger_by_sku: dict[str, dict[str, Any]] = {}
    if not flow_mode_active and not led_df.empty:
        for pid_raw, grp in led_df.groupby("product_id", sort=False):
            pid = str(pid_raw)
            grp = grp.reset_index(drop=True)
            last_r = grp.iloc[-1]
            end_qty = float(last_r["balance_qty"] or 0.0)
            end_val = float(last_r["balance_value"] or 0.0)
            out_rows = grp[grp["out_qty"] > 0]
            last_led_out = str(out_rows["doc_date"].max()) if not out_rows.empty else None
            out_60 = float(grp.loc[grp["doc_date"] >= window_60_str, "out_qty"].sum())

            so_events = 0
            so_days = 0
            in_stockout = False
            so_start_dt: date | None = None
            for r in grp.itertuples(index=False):
                d_cur = date.fromisoformat(str(r.doc_date))
                in_q = float(r.in_qty or 0.0)
                out_q = float(r.out_qty or 0.0)
                bal_q = float(r.balance_qty or 0.0)
                if not in_stockout:
                    if bal_q <= 0.0 and out_q > 0.0:
                        in_stockout = True
                        so_events += 1
                        so_start_dt = d_cur
                else:
                    if in_q > 0.0 and bal_q > 0.0:
                        if so_start_dt is not None:
                            so_days += max(1, (d_cur - so_start_dt).days)
                        in_stockout = False
                        so_start_dt = None
            if in_stockout and so_start_dt is not None:
                so_days += max(1, (as_of_dt - so_start_dt).days)

            ledger_by_sku[pid] = {
                "source": "inventory_ledger",
                "current_stock_qty": round(end_qty, 2),
                "current_stock_value_vnd": round(max(0.0, end_val), 0),
                "last_out_date": last_led_out,
                "out_60d": out_60,
                "stockout_events": so_events,
                "stockout_days": so_days,
            }

    summary_by_sku: dict[str, dict[str, Any]] = {}
    if not flow_mode_active and not sum_df.empty:
        for r in sum_df.itertuples(index=False):
            summary_by_sku[str(r.product_id)] = {
                "source": "inventory_summary",
                "current_stock_qty": round(float(r.closing_qty or 0.0), 2),
                "current_stock_value_vnd": round(max(0.0, float(r.closing_value or 0.0)), 0),
                "out_total": float(r.out_qty or 0.0),
            }

    eff_window_days = max(1, min(60, global_span_days))
    sku_base_rows: list[dict[str, Any]] = []
    covered_count = 0

    for row in prods.itertuples(index=False):
        pid = str(row.product_id)
        s_info = sales_agg.get(pid, {})
        p_info = purch_agg.get(pid, {})
        l_info = ledger_by_sku.get(pid)
        sum_info = summary_by_sku.get(pid)

        is_promo = int(row.is_promo_sku or 0) == 1 or pid.upper().endswith("_KM")
        cost_ref = float(row.latest_purchase_cost or 0.0) or float(p_info.get("latest_inbound_cost") or 0.0)
        price_ref = float(row.selling_price_1 or 0.0)
        paid_qty = float(s_info.get("paid_qty", 0.0))
        gift_qty = float(s_info.get("gift_qty", 0.0))
        total_out_qty = float(s_info.get("total_out_qty", 0.0))
        net_rev = float(s_info.get("net_revenue", 0.0))
        tot_cogs = float(s_info.get("total_cogs", 0.0))
        gift_cogs = float(s_info.get("gift_cogs", 0.0))

        # Phát hiện mọi mã hàng (kể cả mã _KM bán thu tiền) có Giá vốn = 0 trên MISA để không báo lãi ảo 100%!
        missing_cogs = (net_rev > 0.0) and (tot_cogs <= 0.0)
        gross_profit = net_rev - tot_cogs
        margin_pct = 0.0 if missing_cogs else (round(gross_profit / net_rev * 100.0, 1) if net_rev > 0 else 0.0)

        if price_ref <= 0 and paid_qty > 0:
            price_ref = net_rev / paid_qty

        tot_in_qty = float(p_info.get("total_in_qty", 0.0))
        last_in_d = p_info.get("last_inbound_date")
        last_in_qty = float(p_info.get("last_inbound_qty", 0.0))
        sold_after_inb = float(sold_since_last_inb.get(pid, 0.0))
        est_batch_rem = max(0.0, last_in_qty - sold_after_inb) if last_in_qty > 0 else 0.0
        days_since_inb = (
            max(0, (as_of_dt - date.fromisoformat(last_in_d)).days)
            if last_in_d
            else None
        )

        has_stock = (not flow_mode_active) and int(row.has_stock_data or 0) == 1 and (l_info is not None or sum_info is not None)
        if has_stock:
            covered_count += 1

        if l_info is not None:
            stock_source = "inventory_ledger"
            cur_stock: float | None = l_info["current_stock_qty"]
            cur_val: float | None = (
                l_info["current_stock_value_vnd"]
                if l_info["current_stock_value_vnd"] > 0 or (cur_stock is not None and cur_stock <= 0)
                else max(0.0, (cur_stock or 0.0) * cost_ref)
            )
            so_events = int(l_info["stockout_events"])
            so_days = int(l_info["stockout_days"])
            vel_60d = max(recent_sales_60d.get(pid, 0.0), l_info["out_60d"]) / eff_window_days
            last_sale_str = s_info.get("last_sale_date") or l_info["last_out_date"]
        elif sum_info is not None:
            stock_source = "inventory_summary"
            cur_stock = sum_info["current_stock_qty"]
            cur_val = (
                sum_info["current_stock_value_vnd"]
                if sum_info["current_stock_value_vnd"] > 0 or (cur_stock is not None and cur_stock <= 0)
                else max(0.0, (cur_stock or 0.0) * cost_ref)
            )
            so_events = 0
            so_days = 0
            vel_60d = (
                recent_sales_60d.get(pid, 0.0) / eff_window_days
                if pid in recent_sales_60d
                else sum_info["out_total"] / global_span_days
            )
            last_sale_str = s_info.get("last_sale_date")
        else:
            stock_source = "purchase_sales_flow" if flow_mode_active else "none"
            cur_stock = None
            cur_val = None
            so_events = 0
            so_days = 0
            vel_60d = recent_sales_60d.get(pid, 0.0) / eff_window_days
            last_sale_str = s_info.get("last_sale_date")

        if last_sale_str:
            days_since_sale = max(0, (as_of_dt - date.fromisoformat(last_sale_str)).days)
        elif last_in_d:
            days_since_sale = max(0, (as_of_dt - date.fromisoformat(last_in_d)).days)
        else:
            days_since_sale = global_span_days

        est_lost_qty = round(so_days * vel_60d, 1) if so_days > 0 else 0.0
        est_lost_rev = int(round(est_lost_qty * price_ref)) if est_lost_qty > 0 else 0

        sku_base_rows.append(
            {
                "product_id": pid,
                "sku": str(row.sku or pid),
                "product_name": str(row.product_name or pid),
                "sku_anon": str(row.sku_anon or pid),
                "category": str(row.category or "Khác"),
                "warehouse_code": str(row.warehouse_code or "KHO_CHINH"),
                "warehouse_anon": str(row.warehouse_anon or "KHO_01"),
                "base_uom": str(row.base_uom or "ĐV"),
                "box_uom": str(row.box_uom or ""),
                "units_per_box": max(1.0, float(row.units_per_box or 1.0)),
                "is_promo_sku": is_promo,
                "min_stock_qty": float(row.min_stock_qty or 0.0),
                "selling_price_1": round(price_ref, 0),
                "latest_purchase_cost": round(cost_ref, 0),
                "has_stock_data": has_stock,
                "stock_source": stock_source,
                "current_stock_qty": cur_stock,
                "current_stock_value_vnd": int(round(cur_val)) if cur_val is not None else None,
                "total_in_qty": round(tot_in_qty, 1),
                "last_inbound_date": last_in_d,
                "last_inbound_qty": round(last_in_qty, 1),
                "sold_since_last_inbound": round(sold_after_inb, 1),
                "est_batch_remaining_qty": round(est_batch_rem, 1),
                "days_since_last_inbound": days_since_inb,
                "paid_qty": round(paid_qty, 1),
                "gift_qty": round(gift_qty, 1),
                "total_out_qty": round(total_out_qty, 1),
                "net_revenue_vnd": int(round(net_rev)),
                "total_cogs_vnd": int(round(tot_cogs)),
                "gift_cogs_vnd": int(round(gift_cogs)),
                "missing_cogs": missing_cogs,
                "gross_profit_vnd": int(round(gross_profit)),
                "gross_margin_pct": margin_pct,
                "daily_velocity_60d": round(vel_60d, 3),
                "avg_weekly_60d": round(vel_60d * 7.0, 2),
                "days_since_last_sale": int(days_since_sale),
                "stockout_events": so_events,
                "stockout_days": so_days,
                "estimated_lost_qty": est_lost_qty,
                "estimated_lost_revenue_vnd": est_lost_rev,
            }
        )

    total_skus = len(sku_base_rows)
    sold_skus = sum(1 for r in sku_base_rows if r["total_out_qty"] > 0)
    cov_pct = round(covered_count / total_skus * 100.0, 1) if total_skus > 0 else 0.0
    cov_vi = str(cov_pct).replace(".", ",")
    overall_source = (
        "purchase_sales_flow"
        if flow_mode_active
        else ("inventory_ledger" if not led_df.empty else ("inventory_summary" if not sum_df.empty else "none"))
    )

    return {
        "status": "OK",
        "as_of_date": as_of_str,
        "flow_mode_active": flow_mode_active,
        "total_purchase_net_vnd": total_purchase_net_vnd,
        "stock_coverage": {
            "covered_skus": covered_count,
            "sold_skus": sold_skus,
            "total_skus": total_skus,
            "coverage_pct": cov_pct,
            "flow_mode_active": flow_mode_active,
            "source": overall_source,
            "label_vi": (
                f"Đối chiếu trực tiếp Mua – Bán trên đủ {total_skus:,} mặt hàng ({sold_skus:,} mã có bán ra)".replace(",", ".")
                if flow_mode_active
                else f"{covered_count:,}/{total_skus:,} mặt hàng có sổ tồn ({cov_vi}%)".replace(",", ".")
            ),
        },
        "sku_base_rows": sku_base_rows,
    }


def enrich_m2_with_model(
    m2_base: dict[str, Any],
    m1_result: dict[str, Any],
    primary_model_id: str | None = None,
    compare_model_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Ghép dự báo M1 theo mô hình chủ kho đang chọn vào Khám kho & tính gợi ý nhập hàng từng món (< 5ms)."""
    if m2_base.get("status") != "OK":
        return {
            "status": m2_base.get("status", "NO_PRODUCTS"),
            "message_vi": m2_base.get("message_vi", ""),
            "as_of_date": None,
            "flow_mode_active": True,
            "stock_coverage": m2_base.get("stock_coverage", {}),
            "kpis": {},
            "top_sellers": [],
            "top_profit": [],
            "frequent_stockouts": [],
            "slow_overstock_promo": [],
            "all_items": [],
        }

    flow_mode = bool(m2_base.get("flow_mode_active", False))
    sku_fcs = m1_result.get("sku_forecasts", {}) if m1_result else {}
    rec_mid = m1_result.get("recommended_model_id", "best_overall") if m1_result else "ma4"
    chosen_mid = primary_model_id or rec_mid
    comp_ids = [c for c in (compare_model_ids or []) if c != chosen_mid][:2]

    all_items: list[dict[str, Any]] = []
    status_counts = {
        "out_of_stock": 0,
        "low_stock": 0,
        "healthy": 0,
        "overstock": 0,
        "dead_stock": 0,
        "promo_surplus": 0,
        "inactive_zero": 0,
        "no_stock_book": 0,
    }

    total_stock_val = 0
    total_rev = 0
    total_profit_costed = 0
    total_rev_costed = 0
    uncosted_skus_count = 0
    total_gift_cogs = 0
    reorder_skus_count = 0
    reorder_boxes_total = 0
    reorder_est_cost_vnd = 0

    for base in m2_base["sku_base_rows"]:
        pid = base["product_id"]
        fc_entry = sku_fcs.get(pid)
        if fc_entry and chosen_mid in fc_entry["forecasts"]:
            p_fc = fc_entry["forecasts"][chosen_mid]
            fc_1w = float(p_fc["next_1w"])
            fc_4w = float(p_fc["next_4w"])
            avg_wk = float(fc_entry["avg_4w"])
            reason_vi = str(p_fc["reason_vi"])
            demand_group_vi = str(fc_entry["demand_group_vi"])
            comparisons = {
                cid: {
                    "next_1w": float(fc_entry["forecasts"][cid]["next_1w"]),
                    "next_4w": float(fc_entry["forecasts"][cid]["next_4w"]),
                }
                for cid in comp_ids
                if cid in fc_entry["forecasts"]
            }
        else:
            avg_wk = float(base["avg_weekly_60d"])
            fc_1w = round(avg_wk, 2)
            fc_4w = round(avg_wk * 4.0, 2)
            reason_vi = (
                f"Trung bình gần đây bán khoảng {_fmt_qty(avg_wk)} {base['base_uom']}/tuần"
                if avg_wk > 0
                else "Chưa phát sinh đơn bán gần đây"
            )
            demand_group_vi = "Chưa phát sinh" if (base["total_out_qty"] <= 0 and avg_wk <= 0) else "Hàng bán thưa"
            comparisons = {cid: {"next_1w": fc_1w, "next_4w": fc_4w} for cid in comp_ids}

        has_stock = bool(base["has_stock_data"])
        cur_stock = base["current_stock_qty"]
        upb = max(1.0, float(base["units_per_box"]))
        uom = base["base_uom"]
        box_uom = base["box_uom"] or "Thùng"
        is_promo = bool(base["is_promo_sku"])
        days_idle = int(base["days_since_last_sale"])
        ever_active = (base["total_out_qty"] > 0) or (base["total_in_qty"] > 0)

        eff_weekly = max(fc_1w, avg_wk * 0.5) if (fc_1w > 0 or avg_wk > 0) else float(base["avg_weekly_60d"])
        daily_need = eff_weekly / 7.0

        days_of_supply: float | None = None
        suggested_boxes = 0
        suggested_units = 0.0

        if flow_mode:
            # --- CHẾ ĐỘ 4 SỔ MISA (ĐỐI CHIẾU ĐỢT NHẬP MUA GẦN NHẤT VS XUẤT BÁN & DỰ BÁO M1) ---
            last_in_q = float(base["last_inbound_qty"])
            sold_after = float(base["sold_since_last_inbound"])
            batch_rem = float(base["est_batch_remaining_qty"])

            if last_in_q > 0:
                stock_disp_vi = f"Đợt cuối nhập {_fmt_qty(last_in_q)} · Đã bán {_fmt_qty(sold_after)} {uom}"
            elif base["total_out_qty"] > 0:
                stock_disp_vi = f"Đã bán {_fmt_qty(base['total_out_qty'])} {uom} (chưa có phiếu mua trong kỳ)"
            else:
                stock_disp_vi = "Chưa phát sinh mua bán"

            if not ever_active:
                days_of_supply_vi = "Chưa phát sinh"
                status_code = "inactive_zero"
                status_vi = "Chưa phát sinh"
                badge_tone = "neutral"
                action_vi = "Mã trong danh mục chưa phát sinh mua bán · Chưa cần nhập"
            elif is_promo:
                days_of_supply_vi = f"Cách đơn cuối {days_idle} ngày" if base["total_out_qty"] > 0 else "Hàng tặng KM"
                if batch_rem > 0 and days_idle >= 30:
                    status_code = "promo_surplus"
                    status_vi = "Hàng KM còn dư"
                    badge_tone = "warning"
                    action_vi = f"Đợt nhập KM còn khoảng {_fmt_qty(batch_rem)} {uom} · Ghép tặng kèm để giải phóng"
                else:
                    status_code = "healthy"
                    status_vi = "Hàng tặng KM"
                    badge_tone = "info"
                    action_vi = "Theo dõi phát tặng theo chương trình khuyến mãi"
            elif days_idle >= 60:
                days_of_supply_vi = f"Đã nghỉ bán {days_idle} ngày"
                status_code = "dead_stock"
                status_vi = f"Nằm im {days_idle} ngày"
                badge_tone = "danger"
                action_vi = "Ngừng nhập mới · Kiểm kệ đẩy xả nốt nếu còn hàng"
            elif last_in_q > 0 and daily_need > 1e-6 and (batch_rem / daily_need) > 60:
                est_days = int(round(min(365.0, batch_rem / daily_need)))
                days_of_supply = float(est_days)
                days_of_supply_vi = f"Lô mới đủ ~{est_days} ngày"
                status_code = "overstock"
                status_vi = "Nhập nhiều bán chậm"
                badge_tone = "warning"
                action_vi = f"Tạm ngừng nhập · Đợt nhập cuối còn dư khoảng {_fmt_qty(batch_rem)} {uom}"
            elif fc_1w > 0 and (last_in_q <= 0 or batch_rem < fc_1w * 1.2):
                # Đang bán tốt mà lô nhập gần nhất đã bán cạn (hoặc chưa nhập bù trong kỳ) -> gợi ý đặt đủ 2 tuần!
                est_days = int(round(batch_rem / daily_need)) if (last_in_q > 0 and daily_need > 1e-6) else 0
                days_of_supply = float(est_days)
                days_of_supply_vi = (
                    "Đã bán hết đợt nhập cuối"
                    if (last_in_q > 0 and batch_rem <= 0)
                    else (f"Lô mới còn ~{est_days} ngày" if last_in_q > 0 else "Cần kiểm kệ nhập bù")
                )
                status_code = "low_stock" if batch_rem > 0 else "out_of_stock"
                status_vi = "Sắp cạn đợt nhập" if batch_rem > 0 else "Cần đặt đợt mới"
                badge_tone = "danger" if batch_rem > 0 else "critical"
                target_2w = fc_1w * 2.0
                shortage = max(fc_1w, target_2w - batch_rem)
                suggested_boxes = max(1, int(math.ceil(shortage / upb)))
                suggested_units = round(suggested_boxes * upb, 0)
                if upb > 1:
                    action_vi = f"Nên đặt thêm {suggested_boxes} {box_uom.lower()} ({_fmt_qty(suggested_units)} {uom.lower()} cho 2 tuần tới)"
                else:
                    action_vi = f"Nên đặt thêm {_fmt_qty(suggested_units)} {uom.lower()} (đủ bán 2 tuần tới)"
            else:
                est_days = int(round(min(180.0, batch_rem / daily_need))) if (last_in_q > 0 and daily_need > 1e-6) else 14
                days_of_supply = float(est_days)
                days_of_supply_vi = f"Lô mới còn ~{est_days} ngày" if last_in_q > 0 else "Nhịp bán đều"
                status_code = "healthy"
                status_vi = "Đủ bán ổn định"
                badge_tone = "success"
                action_vi = "Đợt nhập gần đây còn đủ bán · Chưa cần đặt gấp"
        else:
            # --- CHẾ ĐỘ SỔ TỒN KHO (KHI NGƯỜI DÙNG CÓ NẠP BẢNG TỒN >= 25% DANH MỤC) ---
            stock_disp_vi = "Chưa có sổ kho" if (not has_stock or cur_stock is None) else f"{_fmt_qty(cur_stock)} {uom}"
            if not has_stock or cur_stock is None:
                days_of_supply_vi = "Chưa có sổ kho"
                status_code = "no_stock_book"
                status_vi = "Chưa có sổ kho"
                badge_tone = "neutral"
                if fc_1w > 0:
                    action_vi = f"Kiểm tồn thực tế tại kệ (dự kiến bán ~{_fmt_qty(fc_1w)} {uom.lower()}/tuần)"
                else:
                    action_vi = "Chưa có sổ kho · Kiểm tra thực tế nếu cần"
            elif cur_stock <= 0 and fc_1w <= 0 and avg_wk <= 0:
                days_of_supply = 0.0
                days_of_supply_vi = "Hết tồn (không phát sinh bán)"
                status_code = "inactive_zero"
                status_vi = "Tạm ngưng / Hết tồn"
                badge_tone = "neutral"
                action_vi = "Hiện bằng 0 và gần đây không phát sinh bán · Chưa cần nhập"
            else:
                if cur_stock <= 0:
                    days_of_supply = 0.0
                    days_of_supply_vi = "Đã hết hàng (0 ngày)"
                elif daily_need > 1e-6:
                    days_of_supply = round(min(365.0, cur_stock / daily_need), 1)
                    days_of_supply_vi = (
                        "Hơn 6 tháng" if days_of_supply >= 180 else f"Khoảng {int(round(days_of_supply))} ngày"
                    )
                else:
                    days_of_supply = 365.0
                    days_of_supply_vi = "Đang nằm im"

                target_stock = max(fc_1w * 2.0, float(base["min_stock_qty"]) * 0.5)
                if is_promo and cur_stock > 0 and (days_idle >= 30 or (days_of_supply is not None and days_of_supply > 45)):
                    status_code = "promo_surplus"
                    status_vi = "Hàng KM còn dư"
                    badge_tone = "warning"
                    action_vi = "Ngừng nhập · Ghép tặng kèm đơn bán để giải phóng kho"
                elif cur_stock > 0 and days_idle >= 60:
                    status_code = "dead_stock"
                    status_vi = f"Nằm im {days_idle} ngày"
                    badge_tone = "danger"
                    action_vi = "Ngừng nhập · Đẩy xả tồn thu hồi vốn"
                elif cur_stock > 0 and days_of_supply is not None and days_of_supply > 60:
                    status_code = "overstock"
                    status_vi = "Tồn hơi nhiều"
                    badge_tone = "warning"
                    action_vi = f"Tạm ngừng nhập · Tồn hiện đủ bán ~{int(round(days_of_supply))} ngày"
                elif cur_stock <= 0 and (fc_1w > 0 or avg_wk > 0):
                    status_code = "out_of_stock"
                    status_vi = "Đang hết hàng"
                    badge_tone = "critical"
                    shortage = max(fc_1w * 2.0, upb)
                    suggested_boxes = max(1, int(math.ceil(shortage / upb)))
                    suggested_units = round(suggested_boxes * upb, 0)
                    action_vi = (
                        f"Nên nhập ngay {suggested_boxes} {box_uom.lower()} ({_fmt_qty(suggested_units)} {uom.lower()})"
                        if upb > 1
                        else f"Nên nhập ngay {_fmt_qty(suggested_units)} {uom.lower()}"
                    )
                elif daily_need > 0 and days_of_supply is not None and days_of_supply < 12 and cur_stock < target_stock:
                    status_code = "low_stock"
                    status_vi = "Sắp hết hàng"
                    badge_tone = "danger"
                    shortage = max(1.0, target_stock - cur_stock)
                    suggested_boxes = max(1, int(math.ceil(shortage / upb)))
                    suggested_units = round(suggested_boxes * upb, 0)
                    action_vi = (
                        f"Nên nhập thêm {suggested_boxes} {box_uom.lower()} ({_fmt_qty(suggested_units)} {uom.lower()})"
                        if upb > 1
                        else f"Nên nhập thêm {_fmt_qty(suggested_units)} {uom.lower()}"
                    )
                else:
                    status_code = "healthy"
                    status_vi = "Đủ bán ổn định"
                    badge_tone = "success"
                    action_vi = "Đủ bán ổn định · Chưa cần nhập thêm"

        ref_order_boxes = suggested_boxes
        if ref_order_boxes == 0 and base["stockout_events"] > 0 and fc_1w > 0:
            need_ref = max(0.0, fc_1w * 2.0 - max(0.0, cur_stock or 0.0))
            if need_ref > 0:
                ref_order_boxes = max(1, int(math.ceil(need_ref / upb)))

        est_order_cost = int(round(suggested_units * float(base["latest_purchase_cost"])))
        status_counts[status_code] = status_counts.get(status_code, 0) + 1
        if base["current_stock_value_vnd"] is not None:
            total_stock_val += int(base["current_stock_value_vnd"])
        total_rev += int(base["net_revenue_vnd"])
        if base["missing_cogs"]:
            uncosted_skus_count += 1
        else:
            total_rev_costed += int(base["net_revenue_vnd"])
            total_profit_costed += int(base["gross_profit_vnd"])
        total_gift_cogs += int(base["gift_cogs_vnd"])
        if suggested_boxes > 0:
            reorder_skus_count += 1
            reorder_boxes_total += suggested_boxes
            reorder_est_cost_vnd += est_order_cost

        all_items.append(
            {
                **base,
                "current_stock_display_vi": stock_disp_vi,
                "avg_weekly_qty": round(avg_wk, 1),
                "forecast_next_1w": round(fc_1w, 1),
                "forecast_next_4w": round(fc_4w, 1),
                "forecast_reason_vi": reason_vi,
                "demand_group_vi": demand_group_vi,
                "comparisons": comparisons,
                "days_of_supply": days_of_supply,
                "days_of_supply_vi": days_of_supply_vi,
                "status_code": status_code,
                "status_vi": status_vi,
                "badge_tone": badge_tone,
                "action_vi": action_vi,
                "suggested_boxes": suggested_boxes,
                "suggested_units": int(round(suggested_units)),
                "suggested_box_uom": box_uom if upb > 1 else uom,
                "estimated_order_cost_vnd": est_order_cost,
                "stockout_ref_boxes": ref_order_boxes,
                "gross_margin_display_vi": (
                    "Chưa có giá vốn"
                    if base["missing_cogs"]
                    else f"{str(base['gross_margin_pct']).replace('.', ',')}%"
                ),
            }
        )

    prio = {
        "out_of_stock": 0,
        "low_stock": 1,
        "dead_stock": 2,
        "promo_surplus": 3,
        "overstock": 4,
        "healthy": 5,
        "no_stock_book": 6,
        "inactive_zero": 7,
    }
    all_items_sorted = sorted(
        all_items,
        key=lambda x: (prio.get(x["status_code"], 9), -x["net_revenue_vnd"], x["sku"]),
    )

    top_sellers = sorted(
        [x for x in all_items if x["total_out_qty"] > 0],
        key=lambda x: (-x["net_revenue_vnd"], -x["total_out_qty"]),
    )[:10]

    # Chỉ xếp hạng Lãi gộp thực trên các mặt hàng ĐÃ CÓ giá vốn trên MISA (loại bỏ lãi ảo 100% do Giá vốn = 0!)
    top_profit = sorted(
        [x for x in all_items if not x["missing_cogs"] and (x["net_revenue_vnd"] > 0 or x["total_cogs_vnd"] > 0)],
        key=lambda x: (-x["gross_profit_vnd"], -x["net_revenue_vnd"]),
    )[:10]

    if flow_mode:
        frequent_stockouts = sorted(
            [x for x in all_items if x["suggested_boxes"] > 0],
            key=lambda x: (-x["forecast_next_1w"] * x["selling_price_1"], -x["suggested_boxes"]),
        )[:10]
    else:
        frequent_stockouts = sorted(
            [x for x in all_items if x["stockout_events"] > 0 or x["status_code"] == "out_of_stock"],
            key=lambda x: (-x["stockout_days"], -x["estimated_lost_revenue_vnd"], -x["forecast_next_1w"]),
        )[:10]

    slow_overstock_promo = sorted(
        [x for x in all_items if x["status_code"] in ("dead_stock", "overstock", "promo_surplus")],
        key=lambda x: (
            0 if x["status_code"] == "dead_stock" else (1 if x["status_code"] == "promo_surplus" else 2),
            -(x["current_stock_value_vnd"] or int(x["est_batch_remaining_qty"] * x["latest_purchase_cost"])),
            -x["days_since_last_sale"],
        ),
    )[:12]

    max_rev = max([1] + [abs(x["net_revenue_vnd"]) for x in all_items])
    max_prof = max([1] + [abs(x["gross_profit_vnd"]) for x in all_items if not x["missing_cogs"]])
    for x in all_items_sorted:
        x["revenue_index"] = round(x["net_revenue_vnd"] / max_rev * 100.0, 1)
        x["profit_index"] = 0.0 if x["missing_cogs"] else round(x["gross_profit_vnd"] / max_prof * 100.0, 1)

    overall_margin = round(total_profit_costed / total_rev_costed * 100.0, 1) if total_rev_costed > 0 else 0.0

    return {
        "status": "OK",
        "as_of_date": m2_base["as_of_date"],
        "flow_mode_active": flow_mode,
        "active_model_id": chosen_mid,
        "compare_model_ids": comp_ids,
        "stock_coverage": m2_base["stock_coverage"],
        "kpis": {
            "total_skus": len(all_items_sorted),
            "flow_mode_active": flow_mode,
            "total_purchase_net_vnd": m2_base.get("total_purchase_net_vnd", 0),
            "total_stock_value_vnd": total_stock_val,
            "total_net_revenue_vnd": total_rev,
            "total_gross_profit_vnd": total_profit_costed,
            "uncosted_skus_count": uncosted_skus_count,
            "total_gift_cogs_vnd": total_gift_cogs,
            "overall_margin_pct": overall_margin,
            "need_reorder_skus": reorder_skus_count,
            "need_reorder_boxes": reorder_boxes_total,
            "need_reorder_est_cost_vnd": reorder_est_cost_vnd,
            "out_of_stock_or_low_skus": status_counts["out_of_stock"] + status_counts["low_stock"],
            "slow_or_overstock_skus": (
                status_counts["dead_stock"] + status_counts["overstock"] + status_counts["promo_surplus"]
            ),
            "status_counts": status_counts,
        },
        "top_sellers": top_sellers,
        "top_profit": top_profit,
        "frequent_stockouts": frequent_stockouts,
        "slow_overstock_promo": slow_overstock_promo,
        "all_items": all_items_sorted,
    }

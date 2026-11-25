"""Module 3: Công nợ Khách hàng (4 nhóm bình dân tự hiệu chuẩn) & 3 Biểu đồ Dòng tiền.

Nguyên tắc thiết kế cho chủ kho U60:
- Không hardcode ngày nợ hay biên lãi: tự hiệu chuẩn từ hóa đơn thực tế và lãi gộp thực tế của kho.
- Tự thích nghi cho cả kho bán thu tiền ngay (nhắc nộp tiền thu hộ trễ, không báo động nhầm) lẫn kho bán gối đầu B2B.
- Chia khách hàng thành 4 nhóm tiếng Việt dễ hiểu:
  1. `call_now`: Cần gọi đòi ngay (nợ quá lâu hoặc đọng nhiều đơn liên tiếp)
  2. `slow_but_pays`: Hay trả chậm nhưng có trả (giữ khách, nhắc nhẹ đúng hẹn, không cắt hàng oan)
  3. `new_customer`: Khách mới (chưa đủ 3 đơn tất toán, theo dõi sát)
  4. `trusted`: Khách trả rất đều (uy tín)
- Tính sẵn cả số tiền thật (`*_vnd`) lẫn Chỉ số chuẩn hóa Đỉnh = 100 (`*_index`) cho chế độ 'Che tên & tiền'.
- Tuyệt đối không dùng thuật ngữ học thuật ở mọi nhãn hiển thị.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import math
import sqlite3
from typing import Any

import numpy as np
import pandas as pd


GROUP_META_VI: dict[str, dict[str, str]] = {
    "call_now": {
        "label_vi": "Cần gọi đòi ngay",
        "desc_vi": "Nợ quá hạn lâu ngày hoặc đang đọng nhiều đơn liên tiếp chưa thanh toán.",
        "badge_tone": "critical",
    },
    "slow_but_pays": {
        "label_vi": "Hay trả chậm nhưng có trả",
        "desc_vi": "Thường thanh toán chậm hơn mặt bằng chung nhưng vẫn trả đủ; nên nhắc nhẹ trước hẹn, tránh cắt hàng oan.",
        "badge_tone": "warning",
    },
    "new_customer": {
        "label_vi": "Khách mới",
        "desc_vi": "Chưa đủ 3 đơn hoàn tất thanh toán; nên giao gối đầu từng đơn nhỏ để theo dõi thêm.",
        "badge_tone": "neutral",
    },
    "trusted": {
        "label_vi": "Khách trả rất đều",
        "desc_vi": "Thanh toán đúng hẹn, uy tín tốt; ưu tiên giao hàng nhanh.",
        "badge_tone": "success",
    },
}


def compute_cashflow_series(conn: sqlite3.Connection) -> dict[str, Any]:
    """Tính dữ liệu cho 3 biểu đồ dòng tiền (kèm chỉ số chuẩn hóa Đỉnh = 100 khi bật Che tên & tiền)."""
    tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    ar_df = (
        pd.read_sql_query(
            """
            SELECT customer_id, doc_date, contra_account, debit_amount, credit_amount,
                   balance_debit, balance_credit, entry_id
            FROM ar_ledger
            WHERE doc_date IS NOT NULL
            ORDER BY doc_date, entry_id
            """,
            conn,
        )
        if "ar_ledger" in tables
        else pd.DataFrame()
    )
    inb_df = (
        pd.read_sql_query(
            "SELECT inbound_date AS doc_date, net_amount FROM inbounds WHERE inbound_date IS NOT NULL",
            conn,
        )
        if "inbounds" in tables
        else pd.DataFrame()
    )
    out_df = (
        pd.read_sql_query(
            "SELECT outbound_date AS doc_date, net_revenue FROM outbounds WHERE outbound_date IS NOT NULL",
            conn,
        )
        if "outbounds" in tables
        else pd.DataFrame()
    )
    invs_df = (
        pd.read_sql_query("SELECT days_to_pay, invoice_amount, payment_status FROM invoices", conn)
        if "invoices" in tables
        else pd.DataFrame()
    )

    if ar_df.empty and inb_df.empty and out_df.empty:
        return {
            "status": "NO_DATA",
            "summary": {},
            "highlights_vi": [],
            "monthly_cashflow": [],
            "payment_speed_histogram": [],
            "payment_method_mix": [],
        }

    ym_set: set[str] = set()
    for df_tmp in (ar_df, inb_df, out_df):
        if not df_tmp.empty:
            ym_set.update(df_tmp["doc_date"].astype(str).str.slice(0, 7).unique().tolist())
    all_yms = sorted(y for y in ym_set if len(y) == 7)

    cash_in_cash: dict[str, float] = defaultdict(float)
    cash_in_bank: dict[str, float] = defaultdict(float)
    ar_sales_m: dict[str, float] = defaultdict(float)
    method_totals = {"cash": 0.0, "bank": 0.0, "return_discount": 0.0, "other": 0.0}

    if not ar_df.empty:
        ar_df["ym"] = ar_df["doc_date"].astype(str).str.slice(0, 7)
        ar_df["contra"] = ar_df["contra_account"].fillna("").astype(str).str.strip()
        for r in ar_df.itertuples(index=False):
            ym = str(r.ym)
            contra = str(r.contra)
            dr = float(r.debit_amount or 0.0)
            cr = float(r.credit_amount or 0.0)
            if dr > 0 and contra != "OPENING":
                ar_sales_m[ym] += dr
            if cr > 0 and contra != "OPENING":
                if contra.startswith("111"):
                    cash_in_cash[ym] += cr
                    method_totals["cash"] += cr
                elif contra.startswith("112"):
                    cash_in_bank[ym] += cr
                    method_totals["bank"] += cr
                elif contra.startswith(("521", "511")):
                    method_totals["return_discount"] += cr
                else:
                    cash_in_cash[ym] += cr
                    method_totals["other"] += cr

    cash_out_m: dict[str, float] = defaultdict(float)
    if not inb_df.empty:
        inb_df["ym"] = inb_df["doc_date"].astype(str).str.slice(0, 7)
        for ym, val in inb_df.groupby("ym")["net_amount"].sum().items():
            cash_out_m[str(ym)] = float(val or 0.0)

    out_rev_m: dict[str, float] = defaultdict(float)
    if not out_df.empty:
        out_df["ym"] = out_df["doc_date"].astype(str).str.slice(0, 7)
        for ym, val in out_df.groupby("ym")["net_revenue"].sum().items():
            out_rev_m[str(ym)] = float(val or 0.0)

    ending_ar_by_ym: dict[str, float] = {}
    if not ar_df.empty:
        cust_bal: dict[str, float] = {}
        rows_arr = list(ar_df.sort_values(["doc_date", "entry_id"]).itertuples(index=False))
        ptr = 0
        n_rows = len(rows_arr)
        for ym in all_yms:
            while ptr < n_rows and str(rows_arr[ptr].ym) <= ym:
                r = rows_arr[ptr]
                b_net = float(r.balance_debit or 0.0) - float(r.balance_credit or 0.0)
                cust_bal[str(r.customer_id)] = max(0.0, b_net)
                ptr += 1
            ending_ar_by_ym[ym] = sum(cust_bal.values())
    else:
        for ym in all_yms:
            ending_ar_by_ym[ym] = 0.0

    monthly_rows: list[dict[str, Any]] = []
    cum_net = 0.0
    for ym in all_yms:
        c_cash = cash_in_cash.get(ym, 0.0)
        c_bank = cash_in_bank.get(ym, 0.0)
        c_in = c_cash + c_bank
        c_out = cash_out_m.get(ym, 0.0)
        net_cf = c_in - c_out
        cum_net += net_cf
        sales_v = ar_sales_m.get(ym, 0.0) if ar_sales_m.get(ym, 0.0) > 0 else out_rev_m.get(ym, 0.0)
        end_ar = ending_ar_by_ym.get(ym, 0.0)
        monthly_rows.append(
            {
                "ym": ym,
                "cash_in_cash_vnd": int(round(c_cash)),
                "cash_in_bank_vnd": int(round(c_bank)),
                "cash_in_total_vnd": int(round(c_in)),
                "cash_out_purchase_vnd": int(round(c_out)),
                "net_cashflow_vnd": int(round(net_cf)),
                "cumulative_net_vnd": int(round(cum_net)),
                "sales_invoiced_vnd": int(round(sales_v)),
                "ending_ar_vnd": int(round(end_ar)),
                "collection_ratio_pct": round(c_in / sales_v * 100.0, 1) if sales_v > 0 else 0.0,
            }
        )

    max_flow = max(
        [1.0]
        + [
            max(r["cash_in_total_vnd"], r["cash_out_purchase_vnd"], r["sales_invoiced_vnd"], r["ending_ar_vnd"])
            for r in monthly_rows
        ]
    )
    for r in monthly_rows:
        r["cash_in_index"] = round(r["cash_in_total_vnd"] / max_flow * 100.0, 1)
        r["cash_out_index"] = round(r["cash_out_purchase_vnd"] / max_flow * 100.0, 1)
        r["net_index"] = round(r["net_cashflow_vnd"] / max_flow * 100.0, 1)
        r["sales_index"] = round(r["sales_invoiced_vnd"] / max_flow * 100.0, 1)
        r["ending_ar_index"] = round(r["ending_ar_vnd"] / max_flow * 100.0, 1)

    buckets = [
        ("0d", "Trả ngay trong ngày", 0, 0),
        ("1_3d", "Sau 1–3 ngày", 1, 3),
        ("4_7d", "Sau 4–7 ngày", 4, 7),
        ("8_15d", "Sau 8–15 ngày", 8, 15),
        ("16_30d", "Sau 16–30 ngày", 16, 30),
        ("gt_30d", "Quá 30 ngày", 31, 99999),
    ]
    paid_invs = invs_df[invs_df["days_to_pay"].notna()].copy() if not invs_df.empty else pd.DataFrame()
    total_paid_cnt = int(len(paid_invs))
    hist_out: list[dict[str, Any]] = []
    max_bucket_amt = 1.0
    raw_bucket_amts: list[float] = []
    for bid, blabel, lo, hi in buckets:
        if total_paid_cnt > 0:
            sub = paid_invs[(paid_invs["days_to_pay"] >= lo) & (paid_invs["days_to_pay"] <= hi)]
            cnt = int(len(sub))
            amt = float(sub["invoice_amount"].sum())
        else:
            cnt, amt = 0, 0.0
        raw_bucket_amts.append(amt)
        max_bucket_amt = max(max_bucket_amt, amt)
        hist_out.append(
            {
                "bucket_id": bid,
                "label_vi": blabel,
                "invoices_count": cnt,
                "share_pct": round(cnt / total_paid_cnt * 100.0, 1) if total_paid_cnt > 0 else 0.0,
                "amount_vnd": int(round(amt)),
            }
        )
    for item, amt in zip(hist_out, raw_bucket_amts):
        item["amount_index"] = round(amt / max_bucket_amt * 100.0, 1)

    tot_method = sum(method_totals.values())
    method_labels = {
        "cash": "Tiền mặt",
        "bank": "Chuyển khoản ngân hàng",
        "return_discount": "Trả hàng / Chiết khấu trừ nợ",
        "other": "Hình thức khác",
    }
    max_method_amt = max([1.0] + list(method_totals.values()))
    method_mix = [
        {
            "method_id": k,
            "label_vi": method_labels[k],
            "amount_vnd": int(round(v)),
            "amount_index": round(v / max_method_amt * 100.0, 1),
            "share_pct": round(v / tot_method * 100.0, 1) if tot_method > 0 else 0.0,
        }
        for k, v in method_totals.items()
        if v > 0
    ]

    tot_in = sum(r["cash_in_total_vnd"] for r in monthly_rows)
    tot_out = sum(r["cash_out_purchase_vnd"] for r in monthly_rows)
    tot_sales = sum(r["sales_invoiced_vnd"] for r in monthly_rows)
    neg_months = [r["ym"] for r in monthly_rows if r["net_cashflow_vnd"] < 0]
    best_net_m = max(monthly_rows, key=lambda r: r["net_cashflow_vnd"]) if monthly_rows else None
    worst_net_m = min(monthly_rows, key=lambda r: r["net_cashflow_vnd"]) if monthly_rows else None
    peak_ar_m = max(monthly_rows, key=lambda r: r["ending_ar_vnd"]) if monthly_rows else None
    fast_pct = sum(b["share_pct"] for b in hist_out if b["bucket_id"] in ("0d", "1_3d"))
    cash_pct = round(method_totals["cash"] / max(1.0, tot_method) * 100.0, 1)
    bank_pct = round(method_totals["bank"] / max(1.0, tot_method) * 100.0, 1)

    highlights_vi: list[str] = []
    if best_net_m and worst_net_m:
        highlights_vi.append(
            f"Tháng dư tiền mặt tốt nhất là {best_net_m['ym']}; tháng chi nhập hàng mạnh nhất là {worst_net_m['ym']} ({len(neg_months)}/{len(monthly_rows)} tháng tiền nhập hàng lớn hơn tiền thu về)."
        )
    if peak_ar_m:
        coll_rate = str(round(tot_in / max(1.0, tot_sales) * 100.0, 1)).replace(".", ",")
        highlights_vi.append(
            f"Tiền khách nợ cuối tháng cao nhất rơi vào {peak_ar_m['ym']}; tính chung toàn kỳ đã thu về được {coll_rate}% tổng tiền bán hàng."
        )
    fast_vi = str(round(fast_pct, 1)).replace(".", ",")
    cash_vi = str(cash_pct).replace(".", ",")
    bank_vi = str(bank_pct).replace(".", ",")
    highlights_vi.append(
        f"Có {fast_vi}% số đơn được khách trả gọn trong 3 ngày đầu; khách trả Tiền mặt chiếm {cash_vi}% và Chuyển khoản chiếm {bank_vi}%."
    )

    return {
        "status": "OK",
        "summary": {
            "months_count": len(monthly_rows),
            "total_cash_in_vnd": int(tot_in),
            "total_cash_out_purchase_vnd": int(tot_out),
            "net_cashflow_vnd": int(tot_in - tot_out),
            "negative_cashflow_months": neg_months,
            "fast_collection_0_3d_pct": round(fast_pct, 1),
            "cash_share_pct": cash_pct,
            "bank_share_pct": bank_pct,
        },
        "highlights_vi": highlights_vi,
        "monthly_cashflow": monthly_rows,
        "payment_speed_histogram": hist_out,
        "payment_method_mix": method_mix,
    }


def run_m3_credit_and_cashflow(conn: sqlite3.Connection) -> dict[str, Any]:
    """Phân nhóm công nợ khách hàng (4 nhóm bình dân tự hiệu chuẩn) & tổng hợp 3 biểu đồ dòng tiền."""
    tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    cashflow = compute_cashflow_series(conn)

    if "invoices" not in tables:
        return {
            "status": "NO_AR_DATA",
            "message_vi": "Chưa có sổ công nợ khách hàng. Hãy nạp thêm file Chi tiết công nợ phải thu từ MISA.",
            "kpis": {},
            "groups_summary": {},
            "customers": [],
            "cashflow": cashflow,
        }

    invs = pd.read_sql_query(
        """
        SELECT i.*, COALESCE(c.customer_name, i.customer_id) AS customer_name,
               COALESCE(c.customer_anon, 'KH_0000') AS customer_anon,
               COALESCE(c.customer_code, '') AS customer_code
        FROM invoices i
        LEFT JOIN customers c ON i.customer_id = c.customer_id
        ORDER BY i.customer_id, i.invoice_date
        """,
        conn,
    )
    if invs.empty:
        return {
            "status": "NO_AR_DATA",
            "message_vi": "Chưa có phát sinh công nợ khách hàng trong kỳ đã nạp.",
            "kpis": {},
            "groups_summary": {},
            "customers": [],
            "cashflow": cashflow,
        }

    as_of_str = str(
        max(
            invs["invoice_date"].max(),
            invs["last_pay_date"].dropna().max() if invs["last_pay_date"].notna().any() else invs["invoice_date"].max(),
        )
    )
    as_of_dt = date.fromisoformat(as_of_str)

    # Tự hiệu chuẩn biên lãi gộp thực tế từ sổ bán (không hardcode)
    gm_rate = 0.15
    if "outbounds" in tables:
        gm_row = conn.execute("SELECT SUM(net_revenue), SUM(gross_profit) FROM outbounds WHERE net_revenue > 0").fetchone()
        if gm_row and gm_row[0] and float(gm_row[0]) > 0:
            gm_rate = float(np.clip(float(gm_row[1] or 0.0) / float(gm_row[0]), 0.03, 0.55))

    paid_days = invs["days_to_pay"].dropna().astype(float).to_numpy()
    if len(paid_days) > 0:
        p25 = float(np.percentile(paid_days, 25))
        p50 = float(np.percentile(paid_days, 50))
        p90 = float(np.percentile(paid_days, 90))
        p95 = float(np.percentile(paid_days, 95))
        p99 = float(np.percentile(paid_days, 99))
    else:
        p25 = p50 = p90 = p95 = p99 = 0.0

    invs["days_open"] = invs.apply(
        lambda r: (
            int(r["days_to_pay"])
            if pd.notna(r["days_to_pay"])
            else max(0, (as_of_dt - date.fromisoformat(str(r["invoice_date"]))).days)
        ),
        axis=1,
    )

    total_invoiced = float(invs["invoice_amount"].sum())
    total_outstanding = float(invs["outstanding_amount"].sum())
    total_cash_paid = float(invs["paid_cash_amount"].sum())
    span_days = max(1, (as_of_dt - date.fromisoformat(str(invs["invoice_date"].min()))).days + 1)
    avg_collection_days = round(total_outstanding / (total_invoiced / span_days), 1) if total_invoiced > 0 else 0.0

    # Nhận diện kho bán thu tiền ngay (nhịp thu <= 5 ngày, 90% đơn <= 7 ngày) vs kho bán gối đầu
    is_fast_cash_warehouse = avg_collection_days <= 5.0 and p90 <= 7.0
    overdue_limit_days = (
        30
        if is_fast_cash_warehouse
        else int(max(35, min(90, math.ceil(p99) + 10 if p99 > 0 else 45)))
    )
    prompt_pay_days = max(5.0, min(p50, p25 + 6.0))
    slow_notice_days = int(max(5, math.ceil(p95) + 1)) if is_fast_cash_warehouse else int(max(7, round(prompt_pay_days + 4)))
    med_inv_amt = float(invs["invoice_amount"].median()) if len(invs) > 0 else 0.0
    min_material_vnd = min(100_000.0, max(10_000.0, med_inv_amt * 0.05))

    groups_summary: dict[str, dict[str, Any]] = {
        k: {
            "group_id": k,
            "label_vi": meta["label_vi"],
            "desc_vi": meta["desc_vi"],
            "badge_tone": meta["badge_tone"],
            "customers_count": 0,
            "outstanding_vnd": 0,
            "invoiced_vnd": 0,
        }
        for k, meta in GROUP_META_VI.items()
    }

    customer_rows: list[dict[str, Any]] = []
    for cid, g in invs.groupby("customer_id", sort=True):
        paid_g = g[g["payment_status"] == "PAID"]
        open_g = g[g["payment_status"] != "PAID"]
        n_paid = int(len(paid_g))
        n_open = int(len(open_g))
        cust_rev = float(g["invoice_amount"].sum())
        cust_outst = float(open_g["outstanding_amount"].sum())
        max_open_days = int(open_g["days_open"].max()) if n_open > 0 else 0
        med_pay_days = float(paid_g["days_to_pay"].median()) if n_paid > 0 else 0.0
        cust_p95_pay = float(np.percentile(paid_g["days_to_pay"], 95)) if n_paid >= 3 else max(7.0, p95)

        c_name = str(g["customer_name"].iloc[0])
        c_anon = str(g["customer_anon"].iloc[0])
        c_code = str(g["customer_code"].iloc[0])

        if is_fast_cash_warehouse:
            # Kho thu tiền ngay: tuyệt đối không báo động nhầm khách trả 1-4 ngày!
            if cust_outst >= min_material_vnd and max_open_days > 30:
                group_id = "call_now"
                reason_vi = f"Đọng {n_open} đơn · Nợ lâu nhất {max_open_days} ngày"
                action_vi = "Gọi nhắc & tạm thu tiền mặt đơn mới"
            elif cust_outst > 0 and max_open_days > slow_notice_days:
                group_id = "slow_but_pays"
                reason_vi = f"Chậm nộp {n_open} đơn · Mở {max_open_days} ngày"
                action_vi = "Nhắc đối chiếu phiếu thu còn treo"
            elif n_paid < 3 and len(g) < 3:
                group_id = "new_customer"
                reason_vi = f"Khách mới ({len(g)} đơn đầu)"
                action_vi = "Thu gọn từng chuyến giao"
            else:
                group_id = "trusted"
                reason_vi = f"Thường trả sau ~{int(round(med_pay_days))} ngày ({n_paid} đơn xong)"
                action_vi = "Giao hàng bình thường"
        else:
            # Kho bán gối đầu công nợ: chỉ báo Cần gọi đòi ngay khi vượt ngưỡng kho HOẶC vượt xa nhịp trả quen của chính khách đó
            cust_habit_limit = (
                max(21.0, med_pay_days * 1.4 + 10.0, cust_p95_pay + 10.0)
                if n_paid >= 1
                else max(30.0, float(overdue_limit_days))
            )
            overdue_vs_habit_cnt = int((open_g["days_open"] > cust_habit_limit).sum()) if n_open > 0 else 0
            too_long = (cust_outst >= min_material_vnd) and (
                max_open_days > max(float(overdue_limit_days), med_pay_days + 12.0)
            )
            broken_habit = (cust_outst >= min_material_vnd) and (
                (max_open_days > cust_habit_limit + 5.0)
                or (overdue_vs_habit_cnt >= 2 and n_open >= 3)
            )

            if too_long or broken_habit:
                group_id = "call_now"
                reason_vi = f"Đọng {n_open} đơn · Nợ lâu nhất {max_open_days} ngày"
                action_vi = "Gọi nhắc & tạm thu tiền mặt đơn mới"
            elif n_paid < 3:
                group_id = "new_customer"
                reason_vi = f"Mới xong {n_paid} đơn" + (f" · Đang mở {n_open} đơn" if n_open > 0 else "")
                action_vi = "Giao gối đầu từng đơn nhỏ"
            elif med_pay_days > prompt_pay_days:
                group_id = "slow_but_pays"
                reason_vi = (
                    f"Quen trả ~{int(round(med_pay_days))} ngày · Đang mở {n_open} đơn ({max_open_days} ngày)"
                    if cust_outst > 0
                    else f"Quen trả ~{int(round(med_pay_days))} ngày · Đã trả hết {n_paid} đơn"
                )
                action_vi = "Nhắc nhẹ đúng hẹn, giữ mối" if cust_outst > 0 else "Sòng phẳng · Giao bình thường"
            elif cust_outst > 0 and max_open_days > max(slow_notice_days, int(round(med_pay_days + 7))):
                group_id = "slow_but_pays"
                reason_vi = f"Quen trả ~{int(round(med_pay_days))} ngày · Còn {n_open} đơn mở {max_open_days} ngày"
                action_vi = "Nhắc nhẹ hoặc đối chiếu phiếu lẻ"
            else:
                group_id = "trusted"
                reason_vi = (
                    f"Trả đều ~{int(round(med_pay_days))} ngày · Mở {n_open} đơn trong hạn"
                    if n_open > 0
                    else f"Trả đều ~{int(round(med_pay_days))} ngày · Đã tất toán {n_paid} đơn"
                )
                action_vi = "Ưu tiên giao bình thường"

        meta = GROUP_META_VI[group_id]
        groups_summary[group_id]["customers_count"] += 1
        groups_summary[group_id]["outstanding_vnd"] += int(round(cust_outst))
        groups_summary[group_id]["invoiced_vnd"] += int(round(cust_rev))

        customer_rows.append(
            {
                "customer_id": str(cid),
                "customer_code": c_code,
                "customer_name": c_name,
                "customer_anon": c_anon,
                "group_id": group_id,
                "group_label_vi": meta["label_vi"],
                "badge_tone": meta["badge_tone"],
                "total_invoices": int(len(g)),
                "paid_invoices": n_paid,
                "open_invoices": n_open,
                "usual_pay_days": round(med_pay_days, 1),
                "oldest_unpaid_days": max_open_days,
                "total_invoiced_vnd": int(round(cust_rev)),
                "outstanding_vnd": int(round(cust_outst)),
                "reason_vi": reason_vi,
                "action_vi": action_vi,
            }
        )

    group_prio = {"call_now": 0, "slow_but_pays": 1, "new_customer": 2, "trusted": 3}
    customer_rows.sort(
        key=lambda x: (group_prio.get(x["group_id"], 9), -x["outstanding_vnd"], -x["oldest_unpaid_days"])
    )

    max_cust_amt = max([1] + [max(c["total_invoiced_vnd"], c["outstanding_vnd"]) for c in customer_rows])
    for c in customer_rows:
        c["invoiced_index"] = round(c["total_invoiced_vnd"] / max_cust_amt * 100.0, 1)
        c["outstanding_index"] = round(c["outstanding_vnd"] / max_cust_amt * 100.0, 1)

    for g_val in groups_summary.values():
        g_val["outstanding_share_pct"] = (
            round(g_val["outstanding_vnd"] / total_outstanding * 100.0, 1) if total_outstanding > 0 else 0.0
        )

    ar_ledger_custs = (
        int(conn.execute("SELECT COUNT(DISTINCT customer_id) FROM ar_ledger").fetchone()[0] or 0)
        if "ar_ledger" in tables
        else len(customer_rows)
    )
    gm_vi = str(round(gm_rate * 100.0, 1)).replace(".", ",")
    cust_note_suffix = (
        f" (trên tổng {ar_ledger_custs} khách ghi nhận ở sổ công nợ, gồm cả khách chỉ có số dư có/tất toán cũ)"
        if ar_ledger_custs > len(customer_rows)
        else ""
    )

    warehouse_style_note_vi = (
        f"Kho mình chủ yếu thu tiền nhanh gọn (trung bình ~{avg_collection_days:.0f} ngày){cust_note_suffix}. Hệ thống tự tắt cảnh báo nhầm với đơn mới giao 1–4 ngày, chỉ lọc đúng các phiếu chậm nộp tiền hoặc nợ treo lâu."
        if is_fast_cash_warehouse
        else f"Kho mình bán gối đầu công nợ (trung bình ~{avg_collection_days:.0f} ngày thu tiền, lãi gộp thực ~{gm_vi}%){cust_note_suffix}. Hệ thống tách riêng khách hay trả chậm nhưng có trả để giữ mối, chỉ báo đỏ khách nợ quá {overdue_limit_days} ngày hoặc đọng nhiều đơn liên tiếp."
    )

    return {
        "status": "OK",
        "as_of_date": as_of_str,
        "warehouse_credit_style": "fast_cash" if is_fast_cash_warehouse else "credit_terms",
        "warehouse_style_note_vi": warehouse_style_note_vi,
        "kpis": {
            "total_customers": len(customer_rows),
            "total_invoices": int(len(invs)),
            "open_invoices": int((invs["payment_status"] != "PAID").sum()),
            "total_invoiced_vnd": int(round(total_invoiced)),
            "total_cash_collected_vnd": int(round(total_cash_paid)),
            "total_outstanding_vnd": int(round(total_outstanding)),
            "outstanding_ratio_pct": round(total_outstanding / total_invoiced * 100.0, 1) if total_invoiced > 0 else 0.0,
            "avg_collection_days": avg_collection_days,
            "usual_pay_days_median": round(p50, 1),
            "overdue_limit_days": overdue_limit_days,
            "gross_margin_pct": round(gm_rate * 100.0, 1),
            "call_now_customers": groups_summary["call_now"]["customers_count"],
            "call_now_outstanding_vnd": groups_summary["call_now"]["outstanding_vnd"],
        },
        "groups_summary": groups_summary,
        "customers": customer_rows,
        "cashflow": cashflow,
    }

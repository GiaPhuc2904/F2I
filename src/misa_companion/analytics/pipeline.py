"""Điều phối luồng phân tích nền (M1 + M2 + M3) có thanh tiến trình (0..100%) và lưu đệm vào SQLite."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable
import uuid

from misa_companion.analytics.m1_arena import ALL_MODEL_IDS, MODEL_CATALOG, run_m1_arena, select_model_forecasts
from misa_companion.analytics.m2_health import compute_m2_base, enrich_m2_with_model
from misa_companion.analytics.m3_credit_cashflow import run_m3_credit_and_cashflow
from misa_companion.config import DEFAULT_DB_PATH
from misa_companion.db import get_connection, init_db

CACHE_KEY_V1 = "full_analysis_v1"

_JOBS_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_LATEST_JOB_BY_DB: dict[str, str] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_model_settings(db_path: str | Path = DEFAULT_DB_PATH, m1_result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Đọc lựa chọn mô hình chính & 1-2 mô hình so sánh từ SQLite (tự điền mặc định hợp lý nếu chưa chọn)."""
    conn = get_connection(db_path)
    try:
        init_db(conn, reset=False)
        rows = dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
    finally:
        conn.close()

    valid_ids = set(ALL_MODEL_IDS)
    rec_id = (m1_result or {}).get("recommended_model_id") or "best_overall"
    if rec_id not in valid_ids:
        rec_id = "best_overall"

    primary = rows.get("primary_model_id", "")
    if primary not in valid_ids:
        primary = rec_id

    raw_comp = rows.get("compare_model_ids", "")
    compares = [c.strip() for c in raw_comp.split(",") if c.strip() in valid_ids and c.strip() != primary][:2]
    if not compares and m1_result:
        candidates = [
            m1_result.get("leaders", {}).get("least_stockout_risk"),
            m1_result.get("leaders", {}).get("least_overstock_risk"),
            "ma4",
            "ma8",
        ]
        for cand in candidates:
            if cand and cand in valid_ids and cand != primary and cand not in compares:
                compares.append(cand)
            if len(compares) >= 2:
                break

    label_map = {m["model_id"]: m["label_vi"] for m in MODEL_CATALOG}
    return {
        "primary_model_id": primary,
        "primary_model_label_vi": label_map.get(primary, primary),
        "compare_model_ids": compares,
        "compare_model_labels_vi": [label_map.get(c, c) for c in compares],
    }


def save_model_settings(
    primary_model_id: str,
    compare_model_ids: list[str] | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Lưu lựa chọn mô hình chính và tối đa 2 mô hình tham khảo vào SQLite."""
    valid_ids = set(ALL_MODEL_IDS)
    if primary_model_id not in valid_ids:
        raise ValueError(f"Mã cách dự báo không hợp lệ: {primary_model_id}")
    compares = [c for c in (compare_model_ids or []) if c in valid_ids and c != primary_model_id][:2]

    conn = get_connection(db_path)
    try:
        init_db(conn, reset=False)
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                [
                    ("primary_model_id", primary_model_id),
                    ("compare_model_ids", ",".join(compares)),
                ],
            )
    finally:
        conn.close()
    return get_model_settings(db_path)


def run_full_analysis_sync(
    db_path: str | Path = DEFAULT_DB_PATH,
    import_report: dict[str, Any] | None = None,
    progress_cb: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    """Chạy toàn bộ M1 + M2 + M3 và lưu đệm kết quả vào `analysis_cache`."""
    def _notify(pct: int, msg: str) -> None:
        if progress_cb is not None:
            progress_cb(pct, msg)

    t0 = time.perf_counter()
    _notify(12, "Bước 1/4: Đang kiểm tra dữ liệu sổ MISA...")

    conn = get_connection(db_path)
    try:
        init_db(conn, reset=False)
        meta_rows = dict(conn.execute("SELECT key, value FROM import_metadata").fetchall())

        _notify(30, "Bước 2/4: Đang thử 11 cách dự báo để tìm cách hợp nhất với kho...")
        m1_res = run_m1_arena(conn, progress_cb=_notify)

        _notify(68, "Bước 3/4: Đang khám bệnh kho & tính số thùng nên nhập...")
        m2_base = compute_m2_base(conn)

        _notify(86, "Bước 4/4: Đang rà soát công nợ khách hàng & dòng tiền...")
        m3_res = run_m3_credit_and_cashflow(conn)

        # Nếu chưa có cấu hình mô hình trong app_settings, đặt mặc định theo mô hình tốt nhất vừa chấm
        cur_settings = dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
        if "primary_model_id" not in cur_settings or import_report and import_report.get("mode") == "reset":
            rec = m1_res.get("recommended_model_id") or "best_overall"
            cands = [
                m1_res.get("leaders", {}).get("least_stockout_risk"),
                "ma4",
                "ma8",
            ]
            comps: list[str] = []
            for c in cands:
                if c and c in ALL_MODEL_IDS and c != rec and c not in comps:
                    comps.append(c)
                if len(comps) >= 2:
                    break
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                    [
                        ("primary_model_id", rec),
                        ("compare_model_ids", ",".join(comps)),
                    ],
                )

        if import_report is not None:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO import_metadata (key, value) VALUES (?, ?)",
                    ("last_import_report_json", json.dumps(import_report, ensure_ascii=False)),
                )
        elif "last_import_report_json" in meta_rows:
            try:
                import_report = json.loads(meta_rows["last_import_report_json"])
            except Exception:
                import_report = {}

        elapsed_sec = round(time.perf_counter() - t0, 2)
        cached_bundle = {
            "computed_at": _now_iso(),
            "elapsed_sec": elapsed_sec,
            "date_range": {
                "min_date": meta_rows.get("min_date", ""),
                "max_date": meta_rows.get("max_date", ""),
            },
            "import_report": import_report or {},
            "m1": m1_res,
            "m2_base": m2_base,
            "m3": m3_res,
        }
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO analysis_cache (cache_key, payload_json, updated_at) VALUES (?, ?, ?)",
                (CACHE_KEY_V1, json.dumps(cached_bundle, ensure_ascii=False), cached_bundle["computed_at"]),
            )
    finally:
        conn.close()

    _notify(100, "Hoàn tất! Đã sẵn sàng xem báo cáo kho.")
    return build_dashboard_payload(db_path=db_path)


def _compute_period_presets(conn: Any, min_date_str: str, max_date_str: str) -> dict[str, dict[str, Any]]:
    """Tính gọn KPI Doanh thu / Lãi gộp / Quà tặng / Tiền nhập theo 4 mốc lùi từ ngày cuối kỳ sổ MISA."""
    if not max_date_str or len(max_date_str) < 10:
        return {}
    max_dt = date.fromisoformat(max_date_str[:10])
    min_dt = date.fromisoformat(min_date_str[:10]) if (min_date_str and len(min_date_str) >= 10) else max_dt
    specs = {
        "30d": max(min_dt, max_dt - timedelta(days=29)).isoformat(),
        "90d": max(min_dt, max_dt - timedelta(days=89)).isoformat(),
        "365d": max(min_dt, max_dt - timedelta(days=364)).isoformat(),
        "all": min_dt.isoformat(),
    }
    out: dict[str, dict[str, Any]] = {}
    for key, start_s in specs.items():
        sku_rows = conn.execute(
            """
            SELECT product_id,
                   COALESCE(SUM(net_revenue), 0.0) AS rev,
                   COALESCE(SUM(cogs), 0.0) AS cogs,
                   COALESCE(SUM(CASE WHEN is_free_gift_line = 1 THEN cogs ELSE 0.0 END), 0.0) AS gift_cogs
            FROM outbound_details
            WHERE outbound_date >= ? AND outbound_date <= ?
            GROUP BY product_id
            """,
            (start_s, max_date_str[:10]),
        ).fetchall()
        p_row = conn.execute(
            "SELECT COALESCE(SUM(net_amount), 0.0) FROM inbounds WHERE inbound_date >= ? AND inbound_date <= ?",
            (start_s, max_date_str[:10]),
        ).fetchone()
        net_rev = 0.0
        prof_costed = 0.0
        rev_costed = 0.0
        gift_cogs = 0.0
        uncosted_skus = 0
        for _pid, r_val, c_val, g_val in sku_rows:
            rv = float(r_val or 0.0)
            cv = float(c_val or 0.0)
            gv = float(g_val or 0.0)
            net_rev += rv
            gift_cogs += gv
            if rv > 0.0 and cv <= 0.0:
                uncosted_skus += 1
            else:
                rev_costed += rv
                prof_costed += rv - cv
        purch_net = float(p_row[0] or 0.0) if p_row else 0.0
        out[key] = {
            "start_date": start_s,
            "end_date": max_date_str[:10],
            "net_revenue_vnd": int(round(net_rev)),
            "gross_profit_vnd": int(round(prof_costed)),
            "gross_margin_pct": round(prof_costed / rev_costed * 100.0, 1) if rev_costed > 0 else 0.0,
            "gift_cogs_vnd": int(round(gift_cogs)),
            "uncosted_skus_count": uncosted_skus,
            "total_purchase_net_vnd": int(round(purch_net)),
        }
    return out


def build_dashboard_payload(
    db_path: str | Path = DEFAULT_DB_PATH,
    primary_model_id: str | None = None,
    compare_model_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Đọc kết quả từ cache SQLite (lên ngay < 50ms) và áp dụng tức thì mô hình M1 đang chọn cho cả Tab 1-4."""
    conn = get_connection(db_path)
    try:
        init_db(conn, reset=False)
        row = conn.execute(
            "SELECT payload_json FROM analysis_cache WHERE cache_key = ?",
            (CACHE_KEY_V1,),
        ).fetchone()
        prod_cnt = int(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] or 0)
        meta_now = dict(conn.execute("SELECT key, value FROM import_metadata").fetchall())
        period_presets = (
            _compute_period_presets(conn, meta_now.get("min_date", ""), meta_now.get("max_date", ""))
            if prod_cnt > 0
            else {}
        )
    finally:
        conn.close()

    if row is None:
        if prod_cnt == 0:
            return {"has_data": False, "message_vi": "Chưa có dữ liệu MISA. Hãy kéo thả file .zip vào để bắt đầu."}
        # Nếu DB đã có dữ liệu nhưng chưa có cache, tự tính đồng bộ 1 lần
        return run_full_analysis_sync(db_path=db_path)

    bundle = json.loads(row[0])
    m1_raw = bundle["m1"]
    m2_base = bundle["m2_base"]
    m3_res = bundle["m3"]

    saved_settings = get_model_settings(db_path=db_path, m1_result=m1_raw)
    active_primary = primary_model_id if (primary_model_id in ALL_MODEL_IDS) else saved_settings["primary_model_id"]
    if compare_model_ids is not None:
        active_compares = [c for c in compare_model_ids if c in ALL_MODEL_IDS and c != active_primary][:2]
    else:
        active_compares = [c for c in saved_settings["compare_model_ids"] if c != active_primary][:2]

    m1_selected = select_model_forecasts(
        m1_raw,
        primary_model_id=active_primary,
        compare_model_ids=active_compares,
    )
    m2_enriched = enrich_m2_with_model(
        m2_base,
        m1_raw,
        primary_model_id=active_primary,
        compare_model_ids=active_compares,
    )

    label_map = {m["model_id"]: m["label_vi"] for m in MODEL_CATALOG}
    m2_kpis = m2_enriched.get("kpis", {})
    m3_kpis = m3_res.get("kpis", {})

    overview = {
        "date_range": bundle.get("date_range", {}),
        "period_presets": period_presets,
        "computed_at": bundle.get("computed_at"),
        "elapsed_sec": bundle.get("elapsed_sec"),
        "active_model_id": active_primary,
        "active_model_label_vi": label_map.get(active_primary, active_primary),
        "recommended_model_id": m1_raw.get("recommended_model_id", "best_overall"),
        "recommended_model_label_vi": m1_raw.get("recommended_model_label_vi", ""),
        "cards": {
            "flow_mode_active": m2_kpis.get("flow_mode_active", False),
            "total_purchase_net_vnd": m2_kpis.get("total_purchase_net_vnd", 0),
            "uncosted_skus_count": m2_kpis.get("uncosted_skus_count", 0),
            "net_revenue_vnd": m2_kpis.get("total_net_revenue_vnd", 0),
            "gross_profit_vnd": m2_kpis.get("total_gross_profit_vnd", 0),
            "gross_margin_pct": m2_kpis.get("overall_margin_pct", 0.0),
            "gift_cogs_vnd": m2_kpis.get("total_gift_cogs_vnd", 0),
            "stock_value_vnd": m2_kpis.get("total_stock_value_vnd", 0),
            "need_reorder_skus": m2_kpis.get("need_reorder_skus", 0),
            "need_reorder_boxes": m2_kpis.get("need_reorder_boxes", 0),
            "need_reorder_est_cost_vnd": m2_kpis.get("need_reorder_est_cost_vnd", 0),
            "slow_or_overstock_skus": m2_kpis.get("slow_or_overstock_skus", 0),
            "total_outstanding_vnd": m3_kpis.get("total_outstanding_vnd", 0),
            "call_now_customers": m3_kpis.get("call_now_customers", 0),
            "call_now_outstanding_vnd": m3_kpis.get("call_now_outstanding_vnd", 0),
        },
        "urgent_reorder_items": [x for x in m2_enriched.get("all_items", []) if x["suggested_boxes"] > 0][:6],
        "urgent_debt_customers": [c for c in m3_res.get("customers", []) if c["group_id"] == "call_now"][:6],
        "stock_coverage": m2_enriched.get("stock_coverage", {}),
        "import_report": bundle.get("import_report", {}),
    }

    # Lược bỏ trường `sku_forecasts` thô cỡ lớn khỏi payload trả về trình duyệt vì `m1_selected` & `m2.all_items` đã có đủ
    m1_light = {k: v for k, v in m1_raw.items() if k != "sku_forecasts"}
    m1_light["selected"] = m1_selected

    return {
        "has_data": True,
        "model_settings": {
            "primary_model_id": active_primary,
            "primary_model_label_vi": label_map.get(active_primary, active_primary),
            "compare_model_ids": active_compares,
            "compare_model_labels_vi": [label_map.get(c, c) for c in active_compares],
        },
        "overview": overview,
        "m1": m1_light,
        "m2": m2_enriched,
        "m3": m3_res,
    }


def start_analysis_job(
    db_path: str | Path = DEFAULT_DB_PATH,
    import_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Khởi chạy luồng phân tích nền và trả về `job_id` để giao diện hỏi thăm tiến độ (0..100%)."""
    job_id = uuid.uuid4().hex[:12]
    db_key = str(Path(db_path).resolve())
    job_state: dict[str, Any] = {
        "job_id": job_id,
        "status": "running",
        "progress_pct": 8,
        "step_message_vi": "Bước 1/4: Đã đọc xong sổ MISA, chuẩn bị phân tích kho...",
        "error": None,
        "started_at": _now_iso(),
        "finished_at": None,
        "recommended_model_id": None,
        "recommended_model_label_vi": None,
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job_state
        _LATEST_JOB_BY_DB[db_key] = job_id

    def _update(pct: int, msg: str) -> None:
        with _JOBS_LOCK:
            job_state["progress_pct"] = pct
            job_state["step_message_vi"] = msg

    def _worker() -> None:
        try:
            dash = run_full_analysis_sync(db_path=db_path, import_report=import_report, progress_cb=_update)
            with _JOBS_LOCK:
                job_state["status"] = "completed"
                job_state["progress_pct"] = 100
                job_state["step_message_vi"] = "Hoàn tất! Đã tìm ra cách dự báo hợp nhất với kho."
                job_state["finished_at"] = _now_iso()
                job_state["recommended_model_id"] = dash.get("overview", {}).get("recommended_model_id")
                job_state["recommended_model_label_vi"] = dash.get("overview", {}).get("recommended_model_label_vi")
                job_state["models"] = dash.get("m1", {}).get("models", [])
                job_state["model_settings"] = dash.get("model_settings", {})
        except Exception as exc:  # pragma: no cover
            with _JOBS_LOCK:
                job_state["status"] = "failed"
                job_state["error"] = str(exc)
                job_state["step_message_vi"] = f"Có trục trặc khi phân tích: {exc}"
                job_state["finished_at"] = _now_iso()

    th = threading.Thread(target=_worker, name=f"misa-job-{job_id}", daemon=True)
    th.start()
    return dict(job_state)


def get_job_status(job_id: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        st = _JOBS.get(job_id)
        return dict(st) if st is not None else None


def get_active_job_for_db(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    db_key = str(Path(db_path).resolve())
    with _JOBS_LOCK:
        jid = _LATEST_JOB_BY_DB.get(db_key)
        if not jid:
            return None
        st = _JOBS.get(jid)
        if st and st["status"] == "running":
            return dict(st)
        return None

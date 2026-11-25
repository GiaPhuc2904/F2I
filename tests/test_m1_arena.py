"""Kiểm thử Phase 2: Đấu trường 11 mô hình + best_overall, đổi mô hình tức thì, không NaN/âm, ngôn ngữ bình dân."""

from __future__ import annotations

from datetime import date
import math
from pathlib import Path
import sqlite3
import time

from misa_companion.analytics.m1_arena import ALL_MODEL_IDS, run_m1_arena, select_model_forecasts
from misa_companion.demo_generator import generate_demo_misa_zip
from misa_companion.ingestion.sync_engine import sync_misa_zip

FORBIDDEN_JARGON = ("WAPE", "MAE", "Bias", "Tweedie", "Zero-Leakage", "Intermittent", "DSO", "DOI", "FIFO")


def test_m1_arena_11_models_and_best_overall(tmp_path: Path) -> None:
    zip_path = generate_demo_misa_zip(tmp_path / "demo_11m.zip", start_date=date(2025, 1, 6), n_weeks=48, seed=42)
    db_path = tmp_path / "app.db"
    sync_misa_zip(zip_path, db_path, mode="reset")

    conn = sqlite3.connect(str(db_path))
    try:
        res = run_m1_arena(conn)
    finally:
        conn.close()

    assert res["status"] == "OK"
    assert res["panel_meta"]["n_weeks"] >= 46
    assert res["panel_meta"]["tet_seasonality_enabled"] is False
    assert len(res["models"]) == 12
    assert {m["model_id"] for m in res["models"]} == set(ALL_MODEL_IDS)

    # Kiểm tra 3 danh hiệu dẫn đầu & không có NaN/âm/thuật ngữ học thuật
    for key in ("most_accurate", "least_stockout_risk", "least_overstock_risk"):
        assert res["leaders"][key] in ALL_MODEL_IDS

    assert isinstance(res.get("warehouse_forecast_advice_vi"), str) and len(res["warehouse_forecast_advice_vi"]) > 20
    for bad in FORBIDDEN_JARGON:
        assert bad.lower() not in res["warehouse_forecast_advice_vi"].lower()

    for m in res["models"]:
        assert 0.0 <= m["accuracy_est_pct"] <= 100.0
        assert m["under_forecast_risk_pct"] >= 0.0
        assert m["over_forecast_risk_pct"] >= 0.0
        if m["model_id"] in ("lgbm_global", "catboost_global"):
            # Ở mốc 11 tháng (< 12-14 tháng), học máy mùa vụ báo rõ cần chờ dày sổ qua Lễ/Tết
            assert m["recommended_for_current_data"] is False
        else:
            assert m["recommended_for_current_data"] is True
        for txt in (
            m["label_vi"],
            m["short_desc_vi"],
            m["demand_style_vi"],
            m["best_for_goods_vi"],
            m["accuracy_label_vi"],
            m["data_readiness_vi"],
        ):
            for bad in FORBIDDEN_JARGON:
                assert bad.lower() not in txt.lower(), f"Phát hiện thuật ngữ cấm '{bad}' trong: {txt}"

    # Kiểm tra dự báo từng SKU cho đủ 12 mô hình (không NaN, không Inf, không âm, có câu giải thích)
    sku_fcs = res["sku_forecasts"]
    assert len(sku_fcs) >= 20
    for pid, info in sku_fcs.items():
        assert set(info["forecasts"].keys()) == set(ALL_MODEL_IDS)
        for mid, fc in info["forecasts"].items():
            assert math.isfinite(fc["next_1w"]) and fc["next_1w"] >= 0.0
            assert math.isfinite(fc["next_4w"]) and fc["next_4w"] >= 0.0
            assert isinstance(fc["reason_vi"], str) and len(fc["reason_vi"]) > 10
            for bad in FORBIDDEN_JARGON:
                assert bad.lower() not in fc["reason_vi"].lower()

    # Đổi mô hình chính & so sánh tức thì (0ms, không cần train lại)
    t0 = time.perf_counter()
    switched = select_model_forecasts(res, primary_model_id="lgbm_global", compare_model_ids=["ma4", "catboost_global"])
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 50.0
    assert switched["primary_model_id"] == "lgbm_global"
    assert switched["compare_model_ids"] == ["ma4", "catboost_global"]
    assert len(switched["items"]) == len(sku_fcs)
    sample_item = switched["items"][0]
    assert set(sample_item["comparisons"].keys()) == {"ma4", "catboost_global"}


def test_m1_arena_short_history_thresholds_and_tet_mode(tmp_path: Path) -> None:
    # 1. Dữ liệu ngắn (~2 tháng = 9 tuần): mô hình cần >= 4 tháng phải đánh dấu recommended_for_current_data = False
    z_short = generate_demo_misa_zip(tmp_path / "short.zip", start_date=date(2025, 1, 6), n_weeks=9, seed=11)
    db_short = tmp_path / "short.db"
    sync_misa_zip(z_short, db_short, mode="reset")
    conn = sqlite3.connect(str(db_short))
    try:
        res_short = run_m1_arena(conn)
    finally:
        conn.close()

    by_id = {m["model_id"]: m for m in res_short["models"]}
    assert by_id["ma4"]["recommended_for_current_data"] is True
    assert by_id["lgbm_global"]["recommended_for_current_data"] is False
    assert by_id["catboost_global"]["recommended_for_current_data"] is False

    # 2. Dữ liệu dài >= 14 tháng (62 tuần): tự bật mùa Tết & cùng kỳ năm ngoái
    z_long = generate_demo_misa_zip(tmp_path / "long.zip", start_date=date(2024, 1, 1), n_weeks=62, seed=12)
    db_long = tmp_path / "long.db"
    sync_misa_zip(z_long, db_long, mode="reset")
    conn = sqlite3.connect(str(db_long))
    try:
        res_long = run_m1_arena(conn)
    finally:
        conn.close()

    assert res_long["status"] == "OK"
    assert res_long["panel_meta"]["tet_seasonality_enabled"] is True
    assert len(res_long["models"]) == 12
    by_long_id = {m["model_id"]: m for m in res_long["models"]}
    assert by_long_id["lgbm_global"]["recommended_for_current_data"] is True
    assert by_long_id["catboost_global"]["recommended_for_current_data"] is True

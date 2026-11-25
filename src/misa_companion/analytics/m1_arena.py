"""Module 1: Đấu trường 11 mô hình dự báo + Tự động ghép tốt nhất (`best_overall`) & Giải thích dân dã cho chủ kho U60.

Đặc điểm thiết kế:
- Tự bỏ tuần cuối chưa đủ 6 ngày (tránh tụt số ảo khi xuất MISA giữa tuần).
- Tự cộng bù nhu cầu bị che khuất ở các tuần kho hết hàng (`balance_qty <= 0` trên Sổ kho).
- 11 mô hình chạy nhanh (< 45 giây cho ~1.500 SKU) + `best_overall` (chọn riêng trên tập kiểm chứng nội bộ cho hàng bán đều & hàng bán thưa).
- Tự bật đặc trưng mùa Tết & cùng kỳ năm ngoái khi dữ liệu đạt >= 14 tháng (>= 60 tuần).
- Lưu sẵn dự báo 1 tuần tới & 4 tuần tới của TẤT CẢ 12 lựa chọn cho từng SKU để đổi mô hình trên UI tức thì (0ms).
- Mọi nhãn hiển thị và câu giải thích đều dùng tiếng Việt bình dân, không có thuật ngữ học thuật.
"""

from __future__ import annotations

from datetime import date, timedelta
import math
import sqlite3
from typing import Any
import warnings

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

try:
    import lightgbm as lgb
    _HAS_LGBM = True
except Exception:  # pragma: no cover
    _HAS_LGBM = False

try:
    from catboost import CatBoostRegressor
    _HAS_CATBOOST = True
except Exception:  # pragma: no cover
    _HAS_CATBOOST = False

from misa_companion.config import is_pre_tet_week, open_days_in_week

# Danh mục chuẩn 11 mô hình + best_overall (chia rõ Kiểu nhịp bán, Danh mục hàng khuyên dùng & Độ dày sổ Lễ/Tết)
MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "model_id": "ma4",
        "label_vi": "Trung bình 4 tuần gần nhất",
        "short_desc_vi": "Lấy trung bình sức bán 4 tuần vừa qua; dễ hiểu, bám sát thay đổi mới.",
        "demand_style_vi": "Hàng bán đều ổn định",
        "style_badge_tone": "success",
        "best_for_goods_vi": "Hàng tiêu dùng thiết yếu bán đều mỗi tuần (hóa mỹ phẩm, giấy, chăm sóc cá nhân, gia vị, đồ khô...) hoặc kho mới mở sổ.",
        "horizon_tier_vi": "Sổ ngắn từ 1 tháng",
        "min_months": 1,
        "kind": "local",
    },
    {
        "model_id": "ma8",
        "label_vi": "Trung bình 8 tuần gần nhất",
        "short_desc_vi": "Lấy trung bình 2 tháng gần nhất; êm hơn, ít bị giật khi có 1 tuần bất thường.",
        "demand_style_vi": "Hàng bán đều ổn định",
        "style_badge_tone": "success",
        "best_for_goods_vi": "Hàng thiết yếu quay vòng đều quanh năm, ít chịu tác động thời tiết hay mùa lễ hội.",
        "horizon_tier_vi": "Sổ từ 2 tháng trở lên",
        "min_months": 2,
        "kind": "local",
    },
    {
        "model_id": "wma",
        "label_vi": "Trung bình ưu tiên tuần gần",
        "short_desc_vi": "Tuần càng gần tính trọng số càng cao, phản ứng nhanh khi hàng bắt đầu hút.",
        "demand_style_vi": "Hàng bán đều & nhích nhẹ",
        "style_badge_tone": "success",
        "best_for_goods_vi": "Hàng tiêu dùng hàng ngày (chăm sóc gia đình, mẹ & bé, thực phẩm thiết yếu...) có sức mua dịch chuyển nhẹ theo tháng.",
        "horizon_tier_vi": "Sổ từ 2 tháng trở lên",
        "min_months": 2,
        "kind": "local",
    },
    {
        "model_id": "ses",
        "label_vi": "Làm mượt theo nhịp gần đây",
        "short_desc_vi": "Tự cân bằng giữa sức bán tuần vừa rồi và nhịp bán đều trước đó.",
        "demand_style_vi": "Hàng bán đều ổn định",
        "style_badge_tone": "success",
        "best_for_goods_vi": "Hàng gia dụng, vệ sinh, thực phẩm đóng gói bán đều nhưng đôi tuần khách lấy lệch ngày.",
        "horizon_tier_vi": "Sổ từ 2 tháng trở lên",
        "min_months": 2,
        "kind": "local",
    },
    {
        "model_id": "damped_ets",
        "label_vi": "Xu hướng tăng/giảm tắt dần",
        "short_desc_vi": "Nhận biết hàng đang bán lên hay xuống, nhưng tự hãm lại để không đoán quá đà.",
        "demand_style_vi": "Hàng đang đổi xu hướng",
        "style_badge_tone": "info",
        "best_for_goods_vi": "Mặt hàng mới mở thị trường đang lên đơn, hoặc hàng qua đợt cao điểm đang hạ nhiệt dần.",
        "horizon_tier_vi": "Sổ từ 3 tháng trở lên",
        "min_months": 3,
        "kind": "local",
    },
    {
        "model_id": "theta",
        "label_vi": "Cân bằng xu hướng & ngắn hạn",
        "short_desc_vi": "Kết hợp nhịp bán gần đây với xu hướng dài hạn; rất ổn định khi dữ liệu dưới 1 năm.",
        "demand_style_vi": "Hàng theo mùa nắng/mưa",
        "style_badge_tone": "info",
        "best_for_goods_vi": "Đồ uống giải khát, sữa, thực phẩm ăn liền có sức mua tăng giảm theo quý hoặc thời tiết.",
        "horizon_tier_vi": "Sổ từ 3–12 tháng",
        "min_months": 3,
        "kind": "local",
    },
    {
        "model_id": "croston_sba",
        "label_vi": "Chuyên hàng bán thưa (cổ điển)",
        "short_desc_vi": "Dành cho món lâu lâu mới có khách lấy; tách riêng nhịp mua và số lượng mỗi lần mua.",
        "demand_style_vi": "Hàng sỉ lấy thưa cách quãng",
        "style_badge_tone": "warning",
        "best_for_goods_vi": "Hàng thùng lớn, hàng giá trị cao, phụ liệu hoặc đại lý tuyến huyện vài tuần mới gom 1 đơn.",
        "horizon_tier_vi": "Sổ từ 3–4 tháng trở lên",
        "min_months": 3,
        "kind": "local",
    },
    {
        "model_id": "tsb",
        "label_vi": "Chuyên hàng bán thưa & sắp ngưng bán",
        "short_desc_vi": "Tự hạ dự báo về gần 0 nếu món hàng đã nhiều tuần liền không có khách hỏi.",
        "demand_style_vi": "Hàng theo đợt / sắp ngưng",
        "style_badge_tone": "warning",
        "best_for_goods_vi": "Hàng khuyến mãi ngắn hạn, hàng thời vụ nhỏ lẻ hoặc các mã bán chậm đang thu hẹp danh mục.",
        "horizon_tier_vi": "Sổ từ 3–4 tháng trở lên",
        "min_months": 3,
        "kind": "local",
    },
    {
        "model_id": "ridge_global",
        "label_vi": "Học chéo toàn kho (ổn định)",
        "short_desc_vi": "Học quy luật chung của toàn bộ mặt hàng trong kho (kể cả nhịp chạy hàng cuối tháng).",
        "demand_style_vi": "Nhịp chạy số cuối tháng",
        "style_badge_tone": "info",
        "best_for_goods_vi": "Kho phân phối có đặc thù nhân viên/đại lý chốt sản lượng rộ vào tuần cuối mỗi tháng.",
        "horizon_tier_vi": "Sổ từ 4 tháng trở lên",
        "min_months": 4,
        "kind": "global",
    },
    {
        "model_id": "lgbm_global",
        "label_vi": "Học máy cây tăng tốc",
        "short_desc_vi": "Học quy luật phi tuyến theo tuần, cuối tháng và ngành hàng; phát huy lợi thế khi sổ đủ dày qua các kỳ lễ.",
        "demand_style_vi": "Mùa vụ Lễ/Tết & Khuyến mãi",
        "style_badge_tone": "neutral",
        "best_for_goods_vi": "Kho đa ngành hàng có nhiều đợt khuyến mãi lớn, mùa tựu trường, Trung thu, cuối năm (bia, nước ngọt, bánh kẹo...).",
        "horizon_tier_vi": "Phát huy khi sổ ≥ 12 tháng (trọn năm)",
        "min_months": 12,
        "kind": "global",
    },
    {
        "model_id": "catboost_global",
        "label_vi": "Học máy theo nhóm hàng & kho",
        "short_desc_vi": "Học sâu theo từng ngành hàng, kho con và đối chiếu cùng kỳ năm ngoái; mạnh nhất khi dữ liệu dày qua Tết.",
        "demand_style_vi": "Mùa vụ Lễ/Tết mạnh & Đa kho",
        "style_badge_tone": "neutral",
        "best_for_goods_vi": "Kho nhiều nhóm hàng & nhiều kho con, hàng quà biếu/thực phẩm/đồ uống vọt mạnh dịp cận Tết Nguyên Đán.",
        "horizon_tier_vi": "Mạnh nhất khi sổ ≥ 14 tháng (đủ Lễ/Tết)",
        "min_months": 14,
        "kind": "global",
    },
    {
        "model_id": "best_overall",
        "label_vi": "Tự động ghép tốt nhất",
        "short_desc_vi": "Tự động dùng công thức gọn khi sổ còn ngắn và tự mở khóa học máy mùa vụ khi sổ dày đủ năm.",
        "demand_style_vi": "Kho hỗn hợp (Tự chia nhóm)",
        "style_badge_tone": "success",
        "best_for_goods_vi": "Hợp mọi kho có cả hàng thiết yếu bán đều lẫn hàng lấy sỉ theo đợt — tự chọn cách tính chuẩn nhất cho từng nhóm.",
        "horizon_tier_vi": "Thích nghi mọi độ dài sổ (≥ 2 tháng)",
        "min_months": 2,
        "kind": "hybrid",
    },
]

LOCAL_MODEL_IDS = ("ma4", "ma8", "wma", "ses", "damped_ets", "theta", "croston_sba", "tsb")
GLOBAL_MODEL_IDS = ("ridge_global", "lgbm_global", "catboost_global")
ALL_BASE_MODEL_IDS = LOCAL_MODEL_IDS + GLOBAL_MODEL_IDS
ALL_MODEL_IDS = ALL_BASE_MODEL_IDS + ("best_overall",)


# =============================================================================
# 1. CÁC MÔ HÌNH ĐƠN CHUỖI (8 MÔ HÌNH LOCAL — NHANH, KHÔNG NaN/ÂM)
# =============================================================================


def _clean_forecast(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr, dtype=float)
    a = np.where(np.isfinite(a), a, 0.0)
    return np.clip(a, 0.0, None)


def _trim_leading_zeros(arr: np.ndarray) -> np.ndarray:
    """Loại bỏ chuỗi tuần 0 ở đầu kỳ trước khi mặt hàng thực sự bắt đầu mở bán tại kho."""
    a = np.asarray(arr, dtype=float)
    nz = np.flatnonzero(a > 0)
    if len(nz) == 0:
        return a[-4:] if len(a) > 4 else a
    return a[int(nz[0]) :]


def _classify_demand_group(vals: np.ndarray) -> str:
    active = _trim_leading_zeros(vals)
    if len(active) == 0 or np.all(active <= 0):
        return "sparse"
    zero_ratio = float(np.mean(active <= 0))
    nz_cnt = int(np.sum(active > 0))
    adi = float(len(active) / max(1, nz_cnt))
    return "sparse" if (zero_ratio >= 0.35 or adi >= 1.32) else "regular"


def _fc_ma4(history: np.ndarray, horizon: int = 1) -> np.ndarray:
    h = _trim_leading_zeros(history)
    if len(h) == 0:
        return np.zeros(horizon, dtype=float)
    return np.full(horizon, max(0.0, float(np.mean(h[-4:]))), dtype=float)


def _fc_ma8(history: np.ndarray, horizon: int = 1) -> np.ndarray:
    h = _trim_leading_zeros(history)
    if len(h) == 0:
        return np.zeros(horizon, dtype=float)
    return np.full(horizon, max(0.0, float(np.mean(h[-8:]))), dtype=float)


def _fc_wma(history: np.ndarray, horizon: int = 1) -> np.ndarray:
    h = _trim_leading_zeros(history)
    if len(h) == 0:
        return np.zeros(horizon, dtype=float)
    tail = h[-4:]
    w = np.arange(1, len(tail) + 1, dtype=float)
    val = float(np.dot(tail, w) / np.sum(w))
    return np.full(horizon, max(0.0, val), dtype=float)


def _fc_ses(history: np.ndarray, horizon: int = 1) -> np.ndarray:
    y = _trim_leading_zeros(history)
    n = len(y)
    if n == 0:
        return np.zeros(horizon, dtype=float)
    if n < 3:
        return np.full(horizon, max(0.0, float(np.mean(y))), dtype=float)

    best_sse = float("inf")
    best_lvl = float(y[-1])
    for alpha in (0.15, 0.30, 0.50):
        lvl = float(y[0])
        sse = 0.0
        for val in y:
            err = val - lvl
            sse += err * err
            lvl = alpha * val + (1.0 - alpha) * lvl
        if sse < best_sse:
            best_sse = sse
            best_lvl = lvl
    return np.full(horizon, max(0.0, best_lvl), dtype=float)


def _fc_damped_ets(history: np.ndarray, horizon: int = 1) -> np.ndarray:
    y = _trim_leading_zeros(history)
    n = len(y)
    if n == 0:
        return np.zeros(horizon, dtype=float)
    if n < 4 or np.allclose(y, y[0]):
        return np.full(horizon, max(0.0, float(np.mean(y[-4:]))), dtype=float)

    best_sse = float("inf")
    best_state = (float(y[-1]), 0.0, 0.85)
    init_l = float(np.mean(y[: min(3, n)]))
    init_b = float((y[min(3, n - 1)] - y[0]) / max(1, min(3, n - 1)))

    for alpha in (0.15, 0.30, 0.50):
        for beta in (0.05, 0.15):
            for phi in (0.80, 0.90):
                l_val = init_l
                b_val = init_b
                sse = 0.0
                for val in y:
                    pred = l_val + phi * b_val
                    err = val - pred
                    sse += err * err
                    l_new = alpha * val + (1.0 - alpha) * pred
                    b_new = beta * (l_new - l_val) + (1.0 - beta) * phi * b_val
                    l_val, b_val = l_new, b_new
                if sse < best_sse:
                    best_sse = sse
                    best_state = (l_val, b_val, phi)

    l_f, b_f, phi_f = best_state
    upper = max(float(np.max(y)) * 2.5, 1.0)
    preds = []
    phi_sum = 0.0
    for h in range(1, horizon + 1):
        phi_sum += phi_f**h
        preds.append(float(np.clip(l_f + phi_sum * b_f, 0.0, upper)))
    return np.asarray(preds, dtype=float)


def _fc_theta(history: np.ndarray, horizon: int = 1) -> np.ndarray:
    y = _trim_leading_zeros(history)
    n = len(y)
    if n == 0:
        return np.zeros(horizon, dtype=float)
    if n < 4:
        return np.full(horizon, max(0.0, float(np.mean(y))), dtype=float)

    t = np.arange(n, dtype=float)
    t_mean = t.mean()
    y_mean = y.mean()
    denom = float(np.sum((t - t_mean) ** 2))
    slope = float(np.sum((t - t_mean) * (y - y_mean)) / denom) if denom > 0 else 0.0

    best_sse = float("inf")
    best_alpha = 0.25
    best_level = float(y[-1])
    for alpha in (0.10, 0.25, 0.45):
        lvl = float(y[0])
        sse = 0.0
        for val in y:
            err = val - lvl
            sse += err * err
            lvl = alpha * val + (1.0 - alpha) * lvl
        if sse < best_sse:
            best_sse = sse
            best_alpha = alpha
            best_level = lvl

    drift = 0.5 * slope
    upper = max(float(np.max(y)) * 2.5, 1.0)
    preds = []
    for h in range(1, horizon + 1):
        val = best_level + drift * (h - 1 + (1.0 - (1.0 - best_alpha) ** n) / max(best_alpha, 0.1))
        preds.append(float(np.clip(val, 0.0, upper)))
    return np.asarray(preds, dtype=float)


def _fc_croston_sba(history: np.ndarray, horizon: int = 1) -> np.ndarray:
    """Croston với hệ số hiệu chỉnh Syntetos-Boylan Approximation (SBA) cho hàng bán thưa."""
    y = _trim_leading_zeros(history)
    if len(y) == 0 or np.all(y <= 0):
        return np.zeros(horizon, dtype=float)

    alpha = 0.2
    nz_idx = np.where(y > 0)[0]
    if len(nz_idx) == 1:
        val = float(y[nz_idx[0]]) / max(1.0, float(len(y)))
        return np.full(horizon, max(0.0, val), dtype=float)

    z = float(y[nz_idx[0]])
    p = max(1.0, float(nz_idx[1] - nz_idx[0]))
    q = 1
    for i in range(int(nz_idx[0]) + 1, len(y)):
        if y[i] > 0:
            z = alpha * float(y[i]) + (1.0 - alpha) * z
            p = alpha * float(q) + (1.0 - alpha) * p
            q = 1
        else:
            q += 1
    # Hiệu chỉnh SBA: (1 - alpha/2) * (z / p)
    val = (1.0 - alpha / 2.0) * (z / max(p, 1.0))
    return np.full(horizon, max(0.0, val), dtype=float)


def _fc_tsb(history: np.ndarray, horizon: int = 1) -> np.ndarray:
    """TSB (Teunter-Syntetos-Babai) cho hàng bán thưa & hàng sắp ngưng bán."""
    y = _trim_leading_zeros(history)
    n = len(y)
    if n == 0 or np.all(y <= 0):
        return np.zeros(horizon, dtype=float)

    nz = y[y > 0]
    init_p = float(np.mean(y > 0))
    init_z = float(np.mean(nz)) if len(nz) > 0 else 0.0

    best_sse = float("inf")
    best_fc = init_p * init_z
    for alpha in (0.15, 0.30):
        for beta in (0.10, 0.25, 0.40):
            p_val = init_p
            z_val = init_z
            sse = 0.0
            for val in y:
                pred = p_val * z_val
                err = val - pred
                sse += err * err
                if val > 0:
                    p_val = p_val + beta * (1.0 - p_val)
                    z_val = z_val + alpha * (val - z_val)
                else:
                    p_val = p_val + beta * (0.0 - p_val)
            if sse < best_sse:
                best_sse = sse
                best_fc = p_val * z_val
    return np.full(horizon, max(0.0, float(best_fc)), dtype=float)


_LOCAL_FN_MAP = {
    "ma4": _fc_ma4,
    "ma8": _fc_ma8,
    "wma": _fc_wma,
    "ses": _fc_ses,
    "damped_ets": _fc_damped_ets,
    "theta": _fc_theta,
    "croston_sba": _fc_croston_sba,
    "tsb": _fc_tsb,
}


# =============================================================================
# 2. XÂY DỰNG BẢNG CẦU TUẦN & BÙ NHU CẦU NGÀY ĐỨT HÀNG
# =============================================================================


def _impute_censored_demand(panel: pd.DataFrame, window: int = 4) -> pd.DataFrame:
    """Bù nhu cầu cho tuần bị hết hàng (`on_hand <= 0`) dựa trên trung bình 4 tuần còn hàng gần nhất trước đó."""
    df = panel.sort_values(["product_id", "week_start"]).copy()
    demands = np.zeros(len(df), dtype=float)
    censored = np.zeros(len(df), dtype=int)

    for _, idxs in df.groupby("product_id", sort=False).groups.items():
        pos = df.index.get_indexer(idxs)
        sold = df["sold_qty"].to_numpy(dtype=float)[pos]
        on_hand = df["on_hand"].to_numpy(dtype=float)[pos]
        recent_clean: list[float] = []

        for k, row_i in enumerate(pos):
            s_val = max(0.0, float(sold[k]))
            if on_hand[k] <= 0.0:
                if recent_clean:
                    est = float(np.mean(recent_clean[-window:]))
                    if est > s_val:
                        demands[row_i] = est
                        censored[row_i] = 1
                    else:
                        demands[row_i] = s_val
                else:
                    demands[row_i] = s_val
            else:
                demands[row_i] = s_val
                recent_clean.append(s_val)

    df["demand"] = demands
    df["is_censored"] = censored
    return df


def build_weekly_demand_panel(conn: sqlite3.Connection) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Dựng bảng cầu tuần hoàn chỉnh `(product_id, week_start)` + tự bỏ tuần cuối < 6 ngày + bù đứt hàng."""
    tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    sales_df = pd.DataFrame()
    if "outbound_details" in tables:
        sales_df = pd.read_sql_query(
            """
            SELECT product_id,
                   outbound_date AS d,
                   SUM(CASE WHEN base_qty_net > 0 THEN base_qty_net ELSE 0 END) AS sold_qty
            FROM outbound_details
            GROUP BY product_id, outbound_date
            """,
            conn,
        )
    if (sales_df.empty or sales_df["d"].nunique() < 7) and "inventory_ledger" in tables:
        led_sales = pd.read_sql_query(
            """
            SELECT product_id,
                   doc_date AS d,
                   SUM(CASE WHEN out_qty > 0 THEN out_qty ELSE 0 END) AS sold_qty
            FROM inventory_ledger
            WHERE out_qty > 0
            GROUP BY product_id, doc_date
            """,
            conn,
        )
        if len(led_sales) > len(sales_df):
            sales_df = led_sales

    if sales_df.empty:
        return pd.DataFrame(), {"source": "none", "n_weeks": 0, "n_months": 0.0, "n_skus": 0, "stockout_sku_weeks": 0}

    sales_df["dt"] = pd.to_datetime(sales_df["d"])
    sales_df["week_start"] = (sales_df["dt"] - pd.to_timedelta(sales_df["dt"].dt.day_of_week, unit="D")).dt.strftime(
        "%Y-%m-%d"
    )
    weekly_sales = sales_df.groupby(["product_id", "week_start"], as_index=False)["sold_qty"].sum()

    all_weeks = pd.date_range(
        pd.to_datetime(weekly_sales["week_start"].min()),
        pd.to_datetime(weekly_sales["week_start"].max()),
        freq="7D",
    ).strftime("%Y-%m-%d").tolist()

    # Tự động bỏ tuần cuối nếu chưa đủ 6 ngày dữ liệu (tránh chấm cuối bị tụt oan)
    max_raw_dt = sales_df["dt"].max()
    dropped_partial_last_week: str | None = None
    if len(all_weeks) >= 6:
        last_ws_dt = pd.to_datetime(all_weeks[-1])
        days_covered_last_wk = int((max_raw_dt - last_ws_dt).days) + 1
        if days_covered_last_wk < 6:
            dropped_partial_last_week = all_weeks[-1]
            all_weeks = all_weeks[:-1]
            weekly_sales = weekly_sales[weekly_sales["week_start"] != dropped_partial_last_week].copy()

    prod_meta = (
        pd.read_sql_query(
            "SELECT product_id, product_name, sku_anon, warehouse_code, category, base_uom, box_uom, units_per_box, is_promo_sku FROM products",
            conn,
        )
        if "products" in tables
        else pd.DataFrame(
            columns=[
                "product_id", "product_name", "sku_anon", "warehouse_code",
                "category", "base_uom", "box_uom", "units_per_box", "is_promo_sku",
            ]
        )
    )

    skus = sorted(weekly_sales["product_id"].unique().tolist())
    grid = pd.MultiIndex.from_product([skus, all_weeks], names=["product_id", "week_start"]).to_frame(index=False)
    panel = grid.merge(weekly_sales, on=["product_id", "week_start"], how="left")
    panel["sold_qty"] = panel["sold_qty"].fillna(0.0)
    panel = panel.merge(prod_meta, on="product_id", how="left")
    panel["product_name"] = panel["product_name"].fillna(panel["product_id"]).astype(str)
    panel["sku_anon"] = panel["sku_anon"].fillna(panel["product_id"]).astype(str)
    panel["warehouse_code"] = panel["warehouse_code"].fillna("DEFAULT").replace("", "DEFAULT").astype(str)
    panel["category"] = panel["category"].fillna("Khác").replace("", "Khác").astype(str)
    panel["base_uom"] = panel["base_uom"].fillna("ĐV").replace("", "ĐV").astype(str)
    panel["box_uom"] = panel["box_uom"].fillna("").astype(str)
    panel["units_per_box"] = panel["units_per_box"].fillna(1.0).astype(float)
    panel["is_promo_sku"] = panel["is_promo_sku"].fillna(0).astype(int)

    # Tích hợp ngày hết hàng (`balance_qty <= 0`) từ inventory_ledger
    stockout_sku_weeks = 0
    panel["on_hand"] = 1.0
    panel["stockout_days"] = 0
    if "inventory_ledger" in tables:
        led_min = pd.read_sql_query(
            """
            SELECT product_id, doc_date AS d, MIN(balance_qty) AS min_bal
            FROM inventory_ledger
            GROUP BY product_id, doc_date
            """,
            conn,
        )
        if not led_min.empty:
            led_min["dt"] = pd.to_datetime(led_min["d"])
            led_min["week_start"] = (
                led_min["dt"] - pd.to_timedelta(led_min["dt"].dt.day_of_week, unit="D")
            ).dt.strftime("%Y-%m-%d")
            led_min["is_so_day"] = (led_min["min_bal"] <= 0.0).astype(int)
            wk_bal = led_min.groupby(["product_id", "week_start"], as_index=False).agg(
                min_bal=("min_bal", "min"),
                so_days=("is_so_day", "sum"),
            )
            panel = panel.merge(wk_bal, on=["product_id", "week_start"], how="left")
            is_so = panel["min_bal"].notna() & (panel["min_bal"] <= 0.0)
            stockout_sku_weeks = int(is_so.sum())
            panel["on_hand"] = np.where(is_so, 0.0, 1.0)
            panel["stockout_days"] = panel["so_days"].fillna(0).astype(int)
            panel = panel.drop(columns=["min_bal", "so_days"])

    panel = _impute_censored_demand(panel, window=4)
    panel["open_days"] = panel["week_start"].map(open_days_in_week)
    panel["week_of_month"] = pd.to_datetime(panel["week_start"]).dt.day.map(lambda d: min(5, (int(d) - 1) // 7 + 1))
    panel["is_pre_tet"] = panel["week_start"].map(is_pre_tet_week)

    n_weeks = len(all_weeks)
    n_months = round(n_weeks / 4.345, 1)
    return panel, {
        "source": "outbound_details" if "outbound_details" in tables else "inventory_ledger",
        "n_weeks": n_weeks,
        "n_months": n_months,
        "tet_seasonality_enabled": n_weeks >= 60,  # >= 14 tháng
        "dropped_partial_last_week": dropped_partial_last_week,
        "n_skus": len(skus),
        "stockout_sku_weeks": stockout_sku_weeks,
        "censored_imputed_rows": int(panel["is_censored"].sum()),
    }


# =============================================================================
# 3. CÁC MÔ HÌNH HỌC CHÉO TOÀN KHO (RIDGE, LIGHTGBM, CATBOOST)
# =============================================================================


def _prepare_global_features(
    panel: pd.DataFrame,
    train_weeks: list[str],
    use_yearly_seasonality: bool,
) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Tạo bộ đặc trưng nhân quả (chỉ shift >= 1, 0% rò rỉ tương lai) cho 3 mô hình học chéo toàn kho."""
    df = panel.sort_values(["product_id", "week_start"]).copy()
    g = df.groupby("product_id", sort=False)["demand"]

    df["lag_1"] = g.shift(1).fillna(0.0)
    df["lag_2"] = g.shift(2).fillna(0.0)
    df["lag_4"] = g.shift(4).fillna(0.0)
    df["roll_mean_4"] = g.transform(lambda s: s.shift(1).rolling(4, min_periods=1).mean()).fillna(0.0)
    df["roll_mean_8"] = g.transform(lambda s: s.shift(1).rolling(8, min_periods=1).mean()).fillna(0.0)
    df["roll_std_8"] = g.transform(lambda s: s.shift(1).rolling(8, min_periods=2).std()).fillna(0.0)
    df["zero_ratio_8"] = g.transform(lambda s: (s.shift(1) <= 0).rolling(8, min_periods=1).mean()).fillna(0.5)
    df["trend_4_vs_8"] = df["roll_mean_4"] - df["roll_mean_8"]

    if use_yearly_seasonality:
        df["lag_52"] = g.shift(52).fillna(df["roll_mean_8"])

    # Đánh dấu các tuần kể từ lần phát sinh bán đầu tiên (bỏ các tuần 0 trước khi hàng mở bán)
    df["cum_dem"] = df.groupby("product_id", sort=False)["demand"].cumsum()
    df["is_launched"] = (df["cum_dem"] > 0).astype(int)

    tr_only = df[(df["week_start"].isin(train_weeks)) & (df["is_launched"] == 1)]
    sku_stats = tr_only.groupby("product_id", as_index=False).agg(
        sku_mean_tr=("demand", "mean"),
        sku_zero_tr=("demand", lambda s: float((s <= 0).mean())),
    )
    df = df.merge(sku_stats, on="product_id", how="left")
    df["sku_mean_tr"] = df["sku_mean_tr"].fillna(0.0)
    df["sku_zero_tr"] = df["sku_zero_tr"].fillna(0.5)
    df["sku_scale"] = np.maximum(1.0, df["sku_mean_tr"].to_numpy(dtype=float))

    rel_cols = ["lag_1", "lag_2", "lag_4", "roll_mean_4", "roll_mean_8", "roll_std_8", "trend_4_vs_8"]
    if use_yearly_seasonality:
        rel_cols.append("lag_52")
    for col in rel_cols:
        df[f"{col}_rel"] = df[col] / df["sku_scale"]

    num_feats = [f"{c}_rel" for c in rel_cols] + [
        "zero_ratio_8", "week_of_month", "open_days", "is_promo_sku", "sku_mean_tr", "sku_zero_tr",
    ]
    if use_yearly_seasonality:
        num_feats.append("is_pre_tet")
    cat_feats = ["warehouse_code", "category"]
    return df, num_feats, cat_feats


def _fit_predict_global_models(
    panel: pd.DataFrame,
    train_weeks: list[str],
    eval_df_input: pd.DataFrame,
    use_yearly_seasonality: bool,
) -> dict[str, np.ndarray]:
    """Huấn luyện và dự báo đồng thời 3 mô hình toàn kho (`ridge_global`, `lgbm_global`, `catboost_global`)."""
    # Ghép thêm các dòng tương lai (nếu eval_df_input chứa tuần mới chưa có trong panel)
    existing_weeks = set(panel["week_start"].unique())
    future_rows = eval_df_input[~eval_df_input["week_start"].isin(existing_weeks)]
    combined = pd.concat([panel, future_rows], ignore_index=True) if not future_rows.empty else panel

    df, num_feats, cat_feats = _prepare_global_features(combined, train_weeks, use_yearly_seasonality)
    warm_weeks = set(train_weeks[4:]) if len(train_weeks) > 6 else set(train_weeks[1:])
    tr_df = df[(df["week_start"].isin(warm_weeks)) & (df["is_launched"] == 1)].copy()

    ev_keys = eval_df_input[["product_id", "week_start"]].copy()
    ev_df = ev_keys.merge(df, on=["product_id", "week_start"], how="left")
    fallback = _clean_forecast(ev_df["roll_mean_8"].fillna(0.0).to_numpy(dtype=float))

    if tr_df.empty or ev_df.empty or len(tr_df) < 25:
        return {"ridge_global": fallback, "lgbm_global": fallback, "catboost_global": fallback}

    y_tr_rel = np.clip(tr_df["demand"].to_numpy(dtype=float) / tr_df["sku_scale"].to_numpy(dtype=float), 0.0, 10.0)
    w_tr = np.maximum(1.0, tr_df["sku_scale"].to_numpy(dtype=float))
    ev_scale = ev_df["sku_scale"].fillna(1.0).to_numpy(dtype=float)

    X_tr_num = np.nan_to_num(tr_df[num_feats].to_numpy(dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    X_ev_num = np.nan_to_num(ev_df[num_feats].to_numpy(dtype=float), nan=0.0, posinf=0.0, neginf=0.0)

    # 1. Ridge Global
    ridge = Ridge(alpha=2.0)
    ridge.fit(X_tr_num, y_tr_rel, sample_weight=w_tr)
    pred_ridge = _clean_forecast(np.clip(ridge.predict(X_ev_num), 0.0, 10.0) * ev_scale)

    # 2. LightGBM Global (Tweedie)
    if _HAS_LGBM:
        tr_lgb = tr_df[num_feats + cat_feats].copy()
        ev_lgb = ev_df[num_feats + cat_feats].copy()
        for c in cat_feats:
            cats = sorted(set(tr_lgb[c].astype(str)) | set(ev_lgb[c].astype(str)))
            tr_lgb[c] = pd.Categorical(tr_lgb[c].astype(str), categories=cats)
            ev_lgb[c] = pd.Categorical(ev_lgb[c].astype(str), categories=cats)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            lgb_model = lgb.LGBMRegressor(
                objective="tweedie",
                tweedie_variance_power=1.35,
                n_estimators=160,
                learning_rate=0.05,
                num_leaves=23,
                min_child_samples=max(5, min(20, len(tr_lgb) // 10)),
                subsample_for_bin=50000,
                random_state=42,
                n_jobs=2,
                verbose=-1,
            )
            lgb_model.fit(tr_lgb, y_tr_rel, sample_weight=w_tr)
            pred_lgbm = _clean_forecast(np.clip(lgb_model.predict(ev_lgb), 0.0, 10.0) * ev_scale)
    else:
        pred_lgbm = pred_ridge

    # 3. CatBoost Global (Tweedie / MAE)
    if _HAS_CATBOOST:
        tr_cb = tr_df[num_feats + cat_feats].copy()
        ev_cb = ev_df[num_feats + cat_feats].copy()
        for c in cat_feats:
            tr_cb[c] = tr_cb[c].astype(str)
            ev_cb[c] = ev_cb[c].astype(str)
        zero_share = float(np.mean(y_tr_rel <= 0.0))
        loss_fn = "Tweedie:variance_power=1.35" if zero_share >= 0.35 else "MAE"
        cb_model = CatBoostRegressor(
            iterations=180,
            depth=5,
            learning_rate=0.06,
            loss_function=loss_fn,
            random_seed=42,
            verbose=False,
            allow_writing_files=False,
            thread_count=2,
        )
        cb_model.fit(tr_cb, y_tr_rel, cat_features=cat_feats, sample_weight=w_tr)
        pred_cb = _clean_forecast(np.clip(cb_model.predict(ev_cb), 0.0, 10.0) * ev_scale)
    else:
        pred_cb = pred_lgbm

    return {
        "ridge_global": pred_ridge,
        "lgbm_global": pred_lgbm,
        "catboost_global": pred_cb,
    }


# =============================================================================
# 4. ĐÁNH GIÁ 3 TIÊU CHÍ BÌNH DÂN & GIẢI THÍCH TỪNG SKU
# =============================================================================


def _evaluate_predictions(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    yt = _clean_forecast(y_true)
    yp = _clean_forecast(y_pred)
    denom = float(np.sum(np.maximum(yt, yp)))
    if denom <= 1e-9:
        return {
            "wape_pct": 0.0,
            "accuracy_est_pct": 100.0,
            "under_forecast_risk_pct": 0.0,
            "over_forecast_risk_pct": 0.0,
            "avg_error_units": 0.0,
        }
    abs_err = float(np.sum(np.abs(yt - yp)))
    err_pct = abs_err / denom * 100.0
    under = float(np.sum(np.maximum(0.0, yt - yp))) / denom * 100.0
    over = float(np.sum(np.maximum(0.0, yp - yt))) / denom * 100.0
    mae = float(np.mean(np.abs(yt - yp)))
    return {
        "wape_pct": round(err_pct, 2),
        "accuracy_est_pct": round(max(0.0, min(100.0, 100.0 - err_pct)), 1),
        "under_forecast_risk_pct": round(max(0.0, min(100.0, under)), 1),
        "over_forecast_risk_pct": round(max(0.0, min(100.0, over)), 1),
        "avg_error_units": round(mae, 2),
    }


def _evaluate_replenishment_cycles(df_ev: pd.DataFrame, pred_col: str) -> dict[str, float]:
    """Chấm điểm độ sát thực tế theo chu kỳ nhập hàng (gộp cụm tối đa 4 tuần/mặt hàng) để không bị nhiễu ngày lấy sỉ."""
    if df_ev.empty:
        return _evaluate_predictions(np.zeros(0), np.zeros(0))
    wks = sorted(df_ev["week_start"].unique().tolist())
    cyc_len = min(4, max(1, len(wks)))
    wk_to_cyc = {w: idx // cyc_len for idx, w in enumerate(wks)}
    tmp = pd.DataFrame(
        {
            "product_id": df_ev["product_id"].to_numpy(),
            "cyc": df_ev["week_start"].map(wk_to_cyc).to_numpy(),
            "yt": _clean_forecast(df_ev["demand"].to_numpy(dtype=float)),
            "yp": _clean_forecast(df_ev[pred_col].to_numpy(dtype=float)),
        }
    )
    agg = tmp.groupby(["product_id", "cyc"], as_index=False)[["yt", "yp"]].sum()
    res = _evaluate_predictions(agg["yt"].to_numpy(dtype=float), agg["yp"].to_numpy(dtype=float))
    # Giữ sai số đơn vị trung bình trên từng tuần để chủ kho dễ hình dung
    res["avg_error_units"] = round(
        float(np.mean(np.abs(_clean_forecast(df_ev["demand"].to_numpy(dtype=float)) - _clean_forecast(df_ev[pred_col].to_numpy(dtype=float))))),
        2,
    )
    return res


def _fmt_qty(val: float) -> str:
    if val >= 10 or abs(val - round(val)) < 0.05:
        return f"{round(val):,.0f}".replace(",", ".")
    s = f"{val:,.1f}".rstrip("0").rstrip(".")
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def _build_plain_reason_vi(
    model_id: str,
    uom: str,
    avg_4w: float,
    avg_8w: float,
    nonzero_8w: int,
    recent_stockout_days: int,
    eom_uplift_pct: float,
    next_week_is_eom: bool,
    next_week_is_pre_tet: bool,
    tet_enabled: bool,
    demand_group: str,
    routed_model_label: str | None = None,
) -> str:
    """Sinh câu giải thích tiếng Việt bình dân, nêu rõ yếu tố tác động tới con số dự báo."""
    parts: list[str] = []

    if model_id == "ma4":
        parts.append(f"4 tuần gần đây bán trung bình {_fmt_qty(avg_4w)} {uom}/tuần")
    elif model_id == "ma8":
        parts.append(f"8 tuần gần đây bán trung bình {_fmt_qty(avg_8w)} {uom}/tuần")
    elif model_id in ("wma", "ses"):
        parts.append(f"Ưu tiên nhịp bán gần nhất ({_fmt_qty(avg_4w)} {uom}/tuần so với TB 2 tháng {_fmt_qty(avg_8w)})")
    elif model_id in ("damped_ets", "theta"):
        if avg_8w > 0 and avg_4w >= avg_8w * 1.12:
            parts.append(f"Sức bán đang tăng nhẹ ({_fmt_qty(avg_4w)} so với {_fmt_qty(avg_8w)} {uom}/tuần)")
        elif avg_8w > 0 and avg_4w <= avg_8w * 0.88:
            parts.append(f"Sức bán đang chậm lại ({_fmt_qty(avg_4w)} so với {_fmt_qty(avg_8w)} {uom}/tuần)")
        else:
            parts.append(f"Sức bán ổn định quanh {_fmt_qty(avg_4w)} {uom}/tuần")
    elif model_id in ("croston_sba", "tsb"):
        parts.append(f"Hàng bán thưa: có khách lấy {nonzero_8w}/8 tuần gần nhất")
        if model_id == "tsb" and nonzero_8w <= 1:
            parts.append("tự hạ dự báo do nhiều tuần không có đơn")
    elif model_id in ("ridge_global", "lgbm_global", "catboost_global"):
        parts.append(f"4 tuần gần đây bán TB {_fmt_qty(avg_4w)} {uom}/tuần")
        if next_week_is_eom and eom_uplift_pct >= 5.0:
            parts.append(f"Cuối tháng thường tăng thêm ~{round(eom_uplift_pct):.0f}%")
        if tet_enabled and next_week_is_pre_tet:
            parts.append("Đang vào cao điểm cận Tết")
    elif model_id == "best_overall":
        grp_txt = "hàng bán đều" if demand_group == "regular" else "hàng bán thưa"
        parts.append(f"Tự chọn '{routed_model_label or 'tốt nhất'}' cho nhóm {grp_txt}")
        parts.append(f"4 tuần gần đây bán TB {_fmt_qty(avg_4w)} {uom}/tuần")
        if next_week_is_eom and eom_uplift_pct >= 5.0:
            parts.append(f"Cuối tháng thường tăng ~{round(eom_uplift_pct):.0f}%")

    if recent_stockout_days > 0:
        parts.append(f"Đã cộng bù do kỳ trước kho hết hàng {recent_stockout_days} ngày")

    return " · ".join(parts)


# =============================================================================
# 5. CHẠY TOÀN BỘ ĐẤU TRƯỜNG 11 MÔ HÌNH + DỰ BÁO TỨC THÌ CHO TỪNG SKU
# =============================================================================


def run_m1_arena(
    conn: sqlite3.Connection,
    progress_cb: Any | None = None,
) -> dict[str, Any]:
    """Chạy Đấu trường 11 mô hình + `best_overall`, đánh giá 3 tiêu chí và lưu sẵn dự báo 1 tuần & 4 tuần cho từng SKU."""
    def _step(pct: int, msg: str) -> None:
        if callable(progress_cb):
            progress_cb(pct, msg)

    _step(32, "Bước 2/4: Đang tổng hợp sức bán tuần & bù ngày hết hàng...")
    panel, panel_meta = build_weekly_demand_panel(conn)
    n_weeks = int(panel_meta["n_weeks"])
    n_months = float(panel_meta["n_months"])
    tet_enabled = bool(panel_meta.get("tet_seasonality_enabled", False))

    if panel.empty or n_weeks == 0:
        return {
            "status": "NO_SALES_DATA",
            "message_vi": "Chưa có dữ liệu bán hàng để dự báo. Hãy nạp file Sổ chi tiết bán hàng từ MISA.",
            "panel_meta": panel_meta,
            "models": [],
            "leaders": {},
            "recommended_model_id": "ma4",
            "sku_forecasts": {},
            "eval_weekly_series": [],
        }

    all_weeks = sorted(panel["week_start"].unique().tolist())
    last_ws = date.fromisoformat(all_weeks[-1])
    future_weeks = [(last_ws + timedelta(days=7 * h)).isoformat() for h in range(1, 5)]
    next_week_is_eom = date.fromisoformat(future_weeks[0]).day >= 22
    next_week_is_pre_tet = is_pre_tet_week(future_weeks[0]) == 1

    # Tính hiệu ứng tăng cuối tháng toàn kho từ dữ liệu thật (không hardcode)
    eom_mask = panel["week_of_month"] >= 4
    eom_avg = float(panel.loc[eom_mask, "demand"].mean()) if eom_mask.any() else 0.0
    non_eom_avg = float(panel.loc[~eom_mask, "demand"].mean()) if (~eom_mask).any() else 0.0
    eom_uplift_pct = ((eom_avg - non_eom_avg) / non_eom_avg * 100.0) if non_eom_avg > 1e-6 else 0.0

    # Chia train/eval (nếu < 6 tuần thì bỏ qua backtest, chỉ chạy dự báo tương lai)
    can_backtest = n_weeks >= 6
    test_len = min(8, max(2, n_weeks // 5)) if can_backtest else 0
    train_weeks = all_weeks[:-test_len] if can_backtest else all_weeks
    eval_weeks = all_weeks[-test_len:] if can_backtest else []

    # Phân loại hàng bán đều (`regular`) vs hàng bán thưa (`sparse`) thuần trên train_weeks (bỏ tuần 0 trước khi mở bán)
    sku_group_tr: dict[str, str] = {}
    train_sub = panel[panel["week_start"].isin(train_weeks)]
    for pid, grp in train_sub.groupby("product_id", sort=True):
        vals = grp["demand"].to_numpy(dtype=float)
        sku_group_tr[str(pid)] = _classify_demand_group(vals)

    # Bước A: Chọn mô hình tốt nhất riêng cho `regular` và `sparse` trên validation nội bộ
    _step(40, "Bước 2/4: Đang kiểm chứng nội bộ để chọn cách ghép tốt nhất...")
    min_months_by_id = {m["model_id"]: int(m["min_months"]) for m in MODEL_CATALOG}
    eligible_base_ids = [m for m in ALL_BASE_MODEL_IDS if n_months >= min_months_by_id.get(m, 1)] or ["ma4"]
    default_reg = "catboost_global" if n_months >= 14 else ("wma" if n_months >= 2 else "ma4")
    routing_by_group: dict[str, str] = {"regular": default_reg, "sparse": "tsb" if n_months >= 3 else "ma4"}
    if can_backtest and len(train_weeks) >= 8:
        val_len = min(4, max(2, len(train_weeks) // 5))
        inner_tr_wks = train_weeks[:-val_len]
        inner_val_wks = train_weeks[-val_len:]
        val_ordered = panel[panel["week_start"].isin(inner_val_wks)].sort_values(["product_id", "week_start"]).copy()

        g_val_preds = _fit_predict_global_models(panel, inner_tr_wks, val_ordered, tet_enabled)
        for mid, arr in g_val_preds.items():
            val_ordered[f"pred_{mid}"] = arr

        local_val_buf: dict[str, list[float]] = {m: [] for m in LOCAL_MODEL_IDS}
        for pid, grp in panel.sort_values("week_start").groupby("product_id", sort=True):
            vals = grp["demand"].to_numpy(dtype=float)
            wk_arr = grp["week_start"].to_numpy()
            for idx in np.where(np.isin(wk_arr, inner_val_wks))[0]:
                hist = vals[:idx]
                for mid in LOCAL_MODEL_IDS:
                    local_val_buf[mid].append(float(_LOCAL_FN_MAP[mid](hist, 1)[0]))
        for mid in LOCAL_MODEL_IDS:
            val_ordered[f"pred_{mid}"] = local_val_buf[mid]
        val_ordered["grp"] = val_ordered["product_id"].map(lambda x: sku_group_tr.get(str(x), "regular"))

        for g_name in ("regular", "sparse"):
            sub = val_ordered[val_ordered["grp"] == g_name]
            if sub.empty:
                continue
            scores = {
                mid: _evaluate_replenishment_cycles(sub, f"pred_{mid}")["wape_pct"]
                for mid in eligible_base_ids
            }
            routing_by_group[g_name] = min(scores, key=scores.get)

    # Bước B: Backtest 11 mô hình + best_overall trên eval_weeks
    _step(50, "Bước 2/4: Đang chấm điểm đối chứng 11 cách dự báo...")
    models_out: list[dict[str, Any]] = []
    eval_weekly_series: list[dict[str, Any]] = []
    leaders: dict[str, str] = {}

    if can_backtest:
        ev_ordered = panel[panel["week_start"].isin(eval_weeks)].sort_values(["product_id", "week_start"]).copy()
        g_ev_preds = _fit_predict_global_models(panel, train_weeks, ev_ordered, tet_enabled)
        for mid, arr in g_ev_preds.items():
            ev_ordered[f"pred_{mid}"] = arr

        local_ev_buf: dict[str, list[float]] = {m: [] for m in LOCAL_MODEL_IDS}
        for pid, grp in panel.sort_values("week_start").groupby("product_id", sort=True):
            vals = grp["demand"].to_numpy(dtype=float)
            wk_arr = grp["week_start"].to_numpy()
            for idx in np.where(np.isin(wk_arr, eval_weeks))[0]:
                hist = vals[:idx]
                for mid in LOCAL_MODEL_IDS:
                    local_ev_buf[mid].append(float(_LOCAL_FN_MAP[mid](hist, 1)[0]))
        for mid in LOCAL_MODEL_IDS:
            ev_ordered[f"pred_{mid}"] = local_ev_buf[mid]

        # Ghép best_overall theo nhóm SKU đã chọn trên validation nội bộ
        ev_groups = ev_ordered["product_id"].map(lambda x: sku_group_tr.get(str(x), "regular")).to_numpy()
        best_ev_arr = np.zeros(len(ev_ordered), dtype=float)
        for i, g_name in enumerate(ev_groups):
            chosen_mid = routing_by_group.get(str(g_name), "ma4")
            best_ev_arr[i] = float(ev_ordered[f"pred_{chosen_mid}"].iloc[i])
        ev_ordered["pred_best_overall"] = best_ev_arr

        for spec in MODEL_CATALOG:
            mid = spec["model_id"]
            metrics = _evaluate_replenishment_cycles(ev_ordered, f"pred_{mid}")
            ready = n_months >= spec["min_months"]
            acc_vi = str(round(metrics["accuracy_est_pct"], 1)).rstrip("0").rstrip(".").replace(".", ",")
            if ready:
                if mid in ("lgbm_global", "catboost_global"):
                    readiness_txt = f"{spec['horizon_tier_vi']} · Đã đủ ({n_months:.0f} tháng, có mùa vụ)"
                else:
                    readiness_txt = f"{spec['horizon_tier_vi']} · Đủ dùng ngay ({n_months:.0f} tháng)"
            else:
                if mid in ("lgbm_global", "catboost_global"):
                    readiness_txt = (
                        f"Chờ dày sổ ≥ {spec['min_months']} tháng có đủ Lễ/Tết (hiện mới {n_months:.0f} tháng)"
                    )
                else:
                    readiness_txt = f"Khuyên dùng khi sổ ≥ {spec['min_months']} tháng (hiện {n_months:.0f} tháng)"

            models_out.append(
                {
                    "model_id": mid,
                    "label_vi": spec["label_vi"],
                    "short_desc_vi": spec["short_desc_vi"],
                    "demand_style_vi": spec["demand_style_vi"],
                    "style_badge_tone": spec["style_badge_tone"],
                    "best_for_goods_vi": spec["best_for_goods_vi"],
                    "horizon_tier_vi": spec["horizon_tier_vi"],
                    "min_months_recommended": spec["min_months"],
                    "recommended_for_current_data": ready,
                    "data_readiness_vi": readiness_txt,
                    "accuracy_est_pct": metrics["accuracy_est_pct"],
                    "accuracy_label_vi": f"Độ chính xác chu kỳ nhập ~{acc_vi}%",
                    "under_forecast_risk_pct": metrics["under_forecast_risk_pct"],
                    "over_forecast_risk_pct": metrics["over_forecast_risk_pct"],
                    "avg_error_units": metrics["avg_error_units"],
                    "_wape_internal": metrics["wape_pct"],
                }
            )

        ready_pool = [m for m in models_out if m["recommended_for_current_data"]] or models_out
        best_acc = max(ready_pool, key=lambda x: (x["accuracy_est_pct"], -x["_wape_internal"]))
        best_no_stockout = min(ready_pool, key=lambda x: (x["under_forecast_risk_pct"], x["_wape_internal"]))
        best_no_overstock = min(ready_pool, key=lambda x: (x["over_forecast_risk_pct"], x["_wape_internal"]))
        leaders = {
            "most_accurate": best_acc["model_id"],
            "least_stockout_risk": best_no_stockout["model_id"],
            "least_overstock_risk": best_no_overstock["model_id"],
        }
        recommended_model_id = best_acc["model_id"]

        # Chuỗi tổng theo tuần trên kỳ kiểm chứng (đủ cả 12 mô hình để UI đổi so sánh tức thì!)
        agg_spec: dict[str, tuple[str, str]] = {"actual": ("demand", "sum")}
        for mid in ALL_MODEL_IDS:
            agg_spec[mid] = (f"pred_{mid}", "sum")
        wk_agg = ev_ordered.groupby("week_start", as_index=False).agg(**agg_spec).sort_values("week_start")
        for _, r in wk_agg.iterrows():
            item: dict[str, Any] = {"week_start": str(r["week_start"]), "actual": round(float(r["actual"]), 1)}
            for mid in ALL_MODEL_IDS:
                item[mid] = round(float(r[mid]), 1)
            eval_weekly_series.append(item)
    else:
        for spec in MODEL_CATALOG:
            ready = n_months >= spec["min_months"]
            models_out.append(
                {
                    "model_id": spec["model_id"],
                    "label_vi": spec["label_vi"],
                    "short_desc_vi": spec["short_desc_vi"],
                    "demand_style_vi": spec["demand_style_vi"],
                    "style_badge_tone": spec["style_badge_tone"],
                    "best_for_goods_vi": spec["best_for_goods_vi"],
                    "horizon_tier_vi": spec["horizon_tier_vi"],
                    "min_months_recommended": spec["min_months"],
                    "recommended_for_current_data": ready,
                    "data_readiness_vi": f"Dữ liệu còn ngắn ({n_weeks} tuần), chưa đủ để chấm điểm đối chứng",
                    "accuracy_est_pct": None,
                    "accuracy_label_vi": "Chưa đủ tuần để chấm điểm",
                    "under_forecast_risk_pct": None,
                    "over_forecast_risk_pct": None,
                    "avg_error_units": None,
                    "_wape_internal": 999.0,
                }
            )
        recommended_model_id = "ma4"
        leaders = {
            "most_accurate": "ma4",
            "least_stockout_risk": "ma4",
            "least_overstock_risk": "ma4",
        }

    # Bước C: Huấn luyện trên TOÀN BỘ lịch sử để dự báo 1 tuần tới & 4 tuần tới cho TẤT CẢ 12 mô hình
    _step(60, "Bước 2/4: Đang tính dự báo tuần tới & 4 tuần tới cho toàn bộ mặt hàng...")
    sku_meta_df = (
        panel.sort_values("week_start")
        .groupby("product_id", as_index=False)
        .last()[["product_id", "product_name", "sku_anon", "warehouse_code", "category", "base_uom", "box_uom", "units_per_box", "is_promo_sku"]]
    )
    fut_rows = []
    for _, row in sku_meta_df.iterrows():
        for fw in future_weeks:
            fut_rows.append(
                {
                    "product_id": row["product_id"],
                    "week_start": fw,
                    "sold_qty": 0.0,
                    "demand": 0.0,
                    "on_hand": 1.0,
                    "stockout_days": 0,
                    "is_censored": 0,
                    "product_name": row["product_name"],
                    "sku_anon": row["sku_anon"],
                    "warehouse_code": row["warehouse_code"],
                    "category": row["category"],
                    "base_uom": row["base_uom"],
                    "box_uom": row["box_uom"],
                    "units_per_box": row["units_per_box"],
                    "is_promo_sku": row["is_promo_sku"],
                    "open_days": open_days_in_week(fw),
                    "week_of_month": min(5, (date.fromisoformat(fw).day - 1) // 7 + 1),
                    "is_pre_tet": is_pre_tet_week(fw),
                }
            )
    fut_df = pd.DataFrame(fut_rows)

    # Dự báo bước 1 (tuần tới) bằng 3 mô hình toàn kho, nhân 4 tuần điều chỉnh theo tuần cuối tháng
    fut_step1 = fut_df[fut_df["week_start"] == future_weeks[0]].reset_index(drop=True)
    global_step1 = _fit_predict_global_models(panel, all_weeks, fut_step1, tet_enabled)
    # Ước lượng tổng 4 tuần tới cho mô hình toàn kho bằng hệ số cuối tháng (tránh loop 4 lần huấn luyện nặng)
    eom_factor = 1.0 + max(0.0, min(0.25, eom_uplift_pct / 100.0))
    step_mults = [eom_factor if date.fromisoformat(fw).day >= 22 else 1.0 for fw in future_weeks]
    four_week_ratio = sum(step_mults) / max(step_mults[0], 1e-6)

    global_sku_map: dict[str, dict[str, tuple[float, float]]] = {}
    for idx, pid in enumerate(fut_step1["product_id"].astype(str).tolist()):
        global_sku_map[pid] = {}
        for mid in GLOBAL_MODEL_IDS:
            p1 = float(global_step1[mid][idx])
            p4 = max(p1, p1 * four_week_ratio)
            global_sku_map[pid][mid] = (p1, p4)

    label_by_id = {m["model_id"]: m["label_vi"] for m in MODEL_CATALOG}
    sku_forecasts: dict[str, dict[str, Any]] = {}
    group_counts = {"regular": 0, "sparse": 0}
    style_counts = {"stable_regular": 0, "seasonal_spiky": 0, "lumpy_sparse": 0}

    for pid, grp in panel.sort_values("week_start").groupby("product_id", sort=True):
        pid_s = str(pid)
        vals = grp["demand"].to_numpy(dtype=float)
        active_vals = _trim_leading_zeros(vals)
        so_days_recent = int(grp["stockout_days"].tail(4).sum())
        avg_4w = float(np.mean(active_vals[-4:])) if len(active_vals) > 0 else 0.0
        avg_8w = float(np.mean(active_vals[-8:])) if len(active_vals) > 0 else 0.0
        nonzero_8w = int(np.sum(active_vals[-8:] > 0))
        uom = str(grp["base_uom"].iloc[-1])
        d_grp = _classify_demand_group(vals)
        group_counts[d_grp] += 1
        if d_grp == "sparse":
            style_counts["lumpy_sparse"] += 1
            sku_style_vi = "Bán thưa theo đợt"
        else:
            mean_act = float(np.mean(active_vals)) if len(active_vals) > 0 else 0.0
            std_act = float(np.std(active_vals)) if len(active_vals) > 1 else 0.0
            if mean_act > 0 and (std_act / mean_act) >= 0.65:
                style_counts["seasonal_spiky"] += 1
                sku_style_vi = "Bán đều · Có đợt tăng vọt"
            else:
                style_counts["stable_regular"] += 1
                sku_style_vi = "Bán đều ổn định"
        routed_mid = routing_by_group.get(d_grp, "ma4")

        fc_by_model: dict[str, dict[str, Any]] = {}
        for mid in LOCAL_MODEL_IDS:
            arr4 = _clean_forecast(_LOCAL_FN_MAP[mid](vals, horizon=4))
            fc_by_model[mid] = {
                "next_1w": round(float(arr4[0]), 2),
                "next_4w": round(float(np.sum(arr4)), 2),
                "reason_vi": _build_plain_reason_vi(
                    mid, uom, avg_4w, avg_8w, nonzero_8w, so_days_recent,
                    eom_uplift_pct, next_week_is_eom, next_week_is_pre_tet, tet_enabled, d_grp,
                ),
            }
        for mid in GLOBAL_MODEL_IDS:
            p1, p4 = global_sku_map.get(pid_s, {}).get(mid, (avg_4w, avg_4w * 4.0))
            p1 = 0.0 if not math.isfinite(p1) else max(0.0, p1)
            p4 = 0.0 if not math.isfinite(p4) else max(0.0, p4)
            fc_by_model[mid] = {
                "next_1w": round(p1, 2),
                "next_4w": round(p4, 2),
                "reason_vi": _build_plain_reason_vi(
                    mid, uom, avg_4w, avg_8w, nonzero_8w, so_days_recent,
                    eom_uplift_pct, next_week_is_eom, next_week_is_pre_tet, tet_enabled, d_grp,
                ),
            }

        chosen_fc = fc_by_model[routed_mid]
        fc_by_model["best_overall"] = {
            "next_1w": chosen_fc["next_1w"],
            "next_4w": chosen_fc["next_4w"],
            "routed_from": routed_mid,
            "reason_vi": _build_plain_reason_vi(
                "best_overall", uom, avg_4w, avg_8w, nonzero_8w, so_days_recent,
                eom_uplift_pct, next_week_is_eom, next_week_is_pre_tet, tet_enabled, d_grp,
                routed_model_label=label_by_id.get(routed_mid),
            ),
        }

        sku_forecasts[pid_s] = {
            "product_id": pid_s,
            "product_name": str(grp["product_name"].iloc[-1]),
            "sku_anon": str(grp["sku_anon"].iloc[-1]),
            "base_uom": uom,
            "box_uom": str(grp["box_uom"].iloc[-1]),
            "units_per_box": float(grp["units_per_box"].iloc[-1]),
            "demand_group": d_grp,
            "demand_group_vi": sku_style_vi,
            "avg_4w": round(avg_4w, 2),
            "avg_8w": round(avg_8w, 2),
            "recent_stockout_days": so_days_recent,
            "forecasts": fc_by_model,
        }

    # Gỡ trường nội bộ `_wape_internal` khỏi output mặt tiền
    for m in models_out:
        m.pop("_wape_internal", None)

    st_cnt = style_counts["stable_regular"]
    sp_cnt = style_counts["seasonal_spiky"]
    lu_cnt = style_counts["lumpy_sparse"]
    mix_part = (
        f"Trong {len(sku_forecasts):,} mặt hàng có lịch sử bán của kho: "
        f"{st_cnt:,} mã bán đều ổn định, {sp_cnt:,} mã có đợt tăng giảm mạnh và {lu_cnt:,} mã khách lấy thưa theo đợt.".replace(",", ".")
    )
    if n_months < 12:
        depth_part = (
            f"Sổ hiện dày {n_months:.0f} tháng (chưa trọn 12–14 tháng để phủ đủ chu kỳ Lễ/Tết năm trước), "
            f"nên các công thức gọn bám nhịp gần như '{label_by_id.get(recommended_model_id, '')}' đang phù hợp và ít nhiễu hơn. "
            f"Khi bạn nạp nối tiếp các tháng tới cho đủ ≥ 14 tháng qua Tết, nhóm Học máy mùa vụ sẽ tự mở khóa hết lợi thế."
        )
    elif n_months < 14:
        depth_part = (
            f"Sổ đã đạt {n_months:.0f} tháng (trọn 1 năm): nhóm Học máy cây đã bắt đầu dùng được; "
            f"nạp thêm tới ≥ 14 tháng sẽ mở khóa trọn vẹn đối chiếu cận Tết cùng kỳ năm ngoái."
        )
    else:
        depth_part = (
            f"Sổ đã dày {n_months:.0f} tháng (đủ chu kỳ năm & dịp Lễ/Tết): nhóm Học máy theo ngành hàng & mùa vụ "
            f"đã kích hoạt đầy đủ đối chiếu cùng kỳ năm ngoái."
        )

    return {
        "status": "OK",
        "panel_meta": panel_meta,
        "future_weeks": future_weeks,
        "eom_uplift_pct": round(eom_uplift_pct, 1),
        "sku_demand_groups": group_counts,
        "sku_style_breakdown": style_counts,
        "warehouse_forecast_advice_vi": f"{mix_part} {depth_part}",
        "best_overall_routing": {
            "regular": routing_by_group["regular"],
            "regular_label_vi": label_by_id.get(routing_by_group["regular"], ""),
            "sparse": routing_by_group["sparse"],
            "sparse_label_vi": label_by_id.get(routing_by_group["sparse"], ""),
        },
        "recommended_model_id": recommended_model_id,
        "recommended_model_label_vi": label_by_id.get(recommended_model_id, ""),
        "leaders": leaders,
        "models": models_out,
        "eval_weekly_series": eval_weekly_series,
        "sku_forecasts": sku_forecasts,
    }


def select_model_forecasts(
    arena_result: dict[str, Any],
    primary_model_id: str | None = None,
    compare_model_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Đổi mô hình chính và 1-2 mô hình so sánh tức thì (0ms, đọc trực tiếp từ kết quả đã tính sẵn)."""
    valid_ids = set(ALL_MODEL_IDS)
    primary = primary_model_id if primary_model_id in valid_ids else arena_result.get("recommended_model_id", "best_overall")
    compares = [m for m in (compare_model_ids or []) if m in valid_ids and m != primary][:2]

    rows: list[dict[str, Any]] = []
    for pid, info in arena_result.get("sku_forecasts", {}).items():
        fcs = info["forecasts"]
        p_fc = fcs[primary]
        rows.append(
            {
                "product_id": pid,
                "product_name": info["product_name"],
                "sku_anon": info["sku_anon"],
                "base_uom": info["base_uom"],
                "units_per_box": info["units_per_box"],
                "demand_group_vi": info["demand_group_vi"],
                "avg_4w": info["avg_4w"],
                "primary_next_1w": p_fc["next_1w"],
                "primary_next_4w": p_fc["next_4w"],
                "reason_vi": p_fc["reason_vi"],
                "comparisons": {
                    cid: {"next_1w": fcs[cid]["next_1w"], "next_4w": fcs[cid]["next_4w"]}
                    for cid in compares
                },
            }
        )

    return {
        "primary_model_id": primary,
        "compare_model_ids": compares,
        "items": rows,
    }

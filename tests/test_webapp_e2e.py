"""Kiểm thử Phase 4 (End-to-End): FastAPI upload .zip -> polling -> 4 tab API -> đổi mô hình -> xuất CSV UTF-8-BOM -> 0 thiếu CSS & 100% Offline."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import re
import subprocess
import shutil
import time

from fastapi.testclient import TestClient

from app.main import STATIC_DIR, create_app
from misa_companion.demo_generator import generate_demo_misa_zip

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


def _extract_defined_css_classes() -> set[str]:
    css_files = [
        STATIC_DIR / "css" / "tailwind.min.css",
        STATIC_DIR / "css" / "app.css",
    ]
    defined: set[str] = set()
    for p in css_files:
        txt = p.read_text(encoding="utf-8")
        # Bóc toàn bộ class selector kể cả ký tự escape của Tailwind (vd sm\:grid-cols-2)
        for m in re.finditer(r"\.((?:[a-zA-Z_-]|\\[a-zA-Z0-9_:/%-])(?:[a-zA-Z0-9_-]|\\[a-zA-Z0-9_:/%-])*)", txt):
            defined.add(m.group(1).replace("\\", ""))
    return defined


def test_offline_assets_js_syntax_and_zero_missing_css_classes() -> None:
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    app_js = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
    app_css = (STATIC_DIR / "css" / "app.css").read_text(encoding="utf-8")

    # 1. Kiểm chứng 100% Offline (0 link http/https ra ngoài trong HTML, CSS, JS)
    for name, content in (("index.html", index_html), ("app.js", app_js), ("app.css", app_css)):
        ext_urls = re.findall(r"https?://[^\s\"')>]+", content)
        assert ext_urls == [], f"Phát hiện URL gọi ra Internet trong {name}: {ext_urls}"

    # 2. Kiểm chứng 0 thuật ngữ học thuật trên mặt tiền HTML & JS
    for name, content in (("index.html", index_html), ("app.js", app_js)):
        for bad in FORBIDDEN_JARGON:
            assert re.search(rf"\b{re.escape(bad)}\b", content, flags=re.IGNORECASE) is None, (
                f"Phát hiện thuật ngữ cấm '{bad}' trong {name}"
            )

    # 3. Kiểm tra cú pháp JavaScript bằng node --check
    node_bin = shutil.which("node")
    assert node_bin is not None, "Cần có node để chạy node --check"
    proc = subprocess.run([node_bin, "--check", str(STATIC_DIR / "js" / "app.js")], capture_output=True, text=True)
    assert proc.returncode == 0, f"Lỗi cú pháp JS: {proc.stderr}"

    # 4. Kiểm toán 0 CSS class bị thiếu trong tailwind.min.css + app.css
    defined = _extract_defined_css_classes()
    used: set[str] = set()

    for m in re.finditer(r'class="([^"]+)"', index_html):
        for tok in m.group(1).split():
            used.add(tok)

    # Trích các chuỗi tĩnh trong thuộc tính class="..." của app.js
    for m in re.finditer(r'class="([^"]+)"', app_js):
        raw_attr = m.group(1)
        static_parts = re.split(r"""['"]\s*\+.*?\+\s*['"]""", raw_attr)
        for part in static_parts:
            for tok in part.split():
                if re.match(r"^[a-zA-Z_-][a-zA-Z0-9_:-]*$", tok):
                    used.add(tok)

    for m in re.finditer(r'classList\.(?:add|remove|toggle)\("([^"]+)"', app_js):
        used.add(m.group(1))

    # Cộng thêm các class động ghép bằng tiền tố badge-* hoặc điều kiện màu chữ
    for tone in ("critical", "danger", "warning", "success", "neutral", "info"):
        used.add(f"badge-{tone}")
    used.add("text-red-700")

    missing = sorted(c for c in used if c not in defined and not c.startswith("js-"))
    assert missing == [], f"Phát hiện CSS class chưa được định nghĩa (lỗi Tailwind purge): {missing}"


def test_fastapi_e2e_upload_zip_dashboard_switch_model_and_export_csv(tmp_path: Path) -> None:
    db_path = tmp_path / "test_e2e.db"
    zip_path = generate_demo_misa_zip(tmp_path / "misa_e2e.zip", start_date=date(2025, 1, 6), n_weeks=24, seed=21)

    test_app = create_app(db_path=db_path)
    client = TestClient(test_app)

    # 1. Trang chủ & các file tĩnh offline trả 200 OK
    r_home = client.get("/")
    assert r_home.status_code == 200
    assert "Che tên &amp; tiền" in r_home.text
    for asset in (
        "/static/css/tailwind.min.css",
        "/static/css/app.css",
        "/static/vendor/chart.umd.min.js",
        "/static/js/app.js",
    ):
        assert client.get(asset).status_code == 200

    # 2. Khi DB trống -> has_data = False
    r_st0 = client.get("/api/status")
    assert r_st0.status_code == 200
    assert r_st0.json()["has_data"] is False

    # 3. Upload file .zip chuẩn MISA -> nhận job_id + báo cáo kiểm tra file
    with zip_path.open("rb") as f:
        r_up = client.post(
            "/api/upload-zip",
            data={"mode": "merge"},
            files={"file": ("misa_e2e.zip", f, "application/zip")},
        )
    assert r_up.status_code == 200
    up_json = r_up.json()
    assert up_json["ok"] is True
    assert up_json["import_report"]["ar_checksum"]["ok"] is True
    job_id = up_json["job_id"]

    # 4. Polling tiến độ job tới khi hoàn tất 100%
    deadline = time.time() + 30.0
    job_data = client.get(f"/api/job/{job_id}").json()
    while job_data["status"] == "running" and time.time() < deadline:
        time.sleep(0.1)
        job_data = client.get(f"/api/job/{job_id}").json()
    assert job_data["status"] == "completed"
    assert job_data["progress_pct"] == 100

    # 5. Lấy toàn bộ dữ liệu 4 tab từ /api/dashboard
    r_dash = client.get("/api/dashboard")
    assert r_dash.status_code == 200
    dash = r_dash.json()
    assert dash["has_data"] is True
    assert len(dash["m1"]["models"]) == 12
    assert len(dash["m2"]["all_items"]) == 24
    assert len(dash["m3"]["customers"]) == 8
    assert len(dash["m3"]["cashflow"]["monthly_cashflow"]) >= 5

    # Xác minh có đủ tên thật + mã ẩn danh + chỉ số chuẩn hóa cho công tắc "Che tên & tiền"
    sample_prod = dash["m2"]["all_items"][0]
    assert sample_prod["product_name"] != ""
    assert sample_prod["sku_anon"].startswith("SKU_")
    assert sample_prod["warehouse_anon"].startswith("KHO_")
    assert "revenue_index" in sample_prod

    sample_cust = dash["m3"]["customers"][0]
    assert sample_cust["customer_name"] != ""
    assert sample_cust["customer_anon"].startswith("KH_")
    assert "outstanding_index" in sample_cust

    # 6. Đổi mô hình chính & so sánh qua POST /api/settings/models -> phản hồi tức thì
    t0 = time.perf_counter()
    r_save = client.post(
        "/api/settings/models",
        json={"primary_model_id": "theta", "compare_model_ids": ["ma4", "lgbm_global"]},
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert r_save.status_code == 200
    assert elapsed_ms < 250.0
    saved_dash = r_save.json()["dashboard"]
    assert saved_dash["model_settings"]["primary_model_id"] == "theta"
    assert saved_dash["m2"]["active_model_id"] == "theta"

    # 7. Xuất file CSV UTF-8-BOM (cả chế độ tên thật lẫn chế độ Che tên & tiền)
    r_csv_real = client.get("/api/export/reorder-csv?include_all=true")
    assert r_csv_real.status_code == 200
    assert r_csv_real.content.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM cho Excel Windows
    txt_real = r_csv_real.content.decode("utf-8-sig")
    assert "Tên mặt hàng" in txt_real
    assert "Nước ngọt Cola 330ml" in txt_real

    r_csv_anon = client.get("/api/export/reorder-csv?include_all=true&hide_sensitive=true")
    assert r_csv_anon.status_code == 200
    txt_anon = r_csv_anon.content.decode("utf-8-sig")
    assert "Nước ngọt Cola 330ml" not in txt_anon
    assert "Mặt hàng SKU_" in txt_anon
    assert "Đã ẩn" in txt_anon

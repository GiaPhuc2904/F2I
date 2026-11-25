"""FastAPI Backend cho Cộng sự Offline MISA Kế toán (127.0.0.1:8050, không đăng nhập, 100% cục bộ)."""

from __future__ import annotations

import csv
import io
import math
import os
from pathlib import Path
import tempfile
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from misa_companion.analytics.pipeline import (
    build_dashboard_payload,
    get_active_job_for_db,
    get_job_status,
    get_model_settings,
    save_model_settings,
    start_analysis_job,
)
from misa_companion.config import DEFAULT_DB_PATH, MAX_ZIP_FILE_BYTES
from misa_companion.db import get_connection, init_db
from misa_companion.demo_generator import generate_demo_misa_zip
from misa_companion.ingestion.sync_engine import sync_misa_zip

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"


class ModelSettingsPayload(BaseModel):
    primary_model_id: str
    compare_model_ids: list[str] = Field(default_factory=list)


def _resolve_db_path(request: Request) -> Path:
    override = getattr(request.app.state, "db_path", None)
    if override is not None:
        return Path(override)
    env_p = os.environ.get("MISA_COMPANION_DB_PATH")
    return Path(env_p) if env_p else DEFAULT_DB_PATH


def create_app(db_path: str | Path | None = None) -> FastAPI:
    application = FastAPI(
        title="Cộng sự Kho Offline cho MISA Kế toán",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    if db_path is not None:
        application.state.db_path = Path(db_path)

    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @application.get("/", include_in_schema=False)
    def serve_index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/api/status")
    def api_status(request: Request) -> dict[str, Any]:
        target_db = _resolve_db_path(request)
        conn = get_connection(target_db)
        try:
            init_db(conn, reset=False)
            prod_cnt = int(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] or 0)
            meta = dict(conn.execute("SELECT key, value FROM import_metadata").fetchall())
        finally:
            conn.close()

        active_job = get_active_job_for_db(target_db)
        settings = get_model_settings(target_db)
        return {
            "ok": True,
            "has_data": prod_cnt > 0,
            "products_count": prod_cnt,
            "date_range": {
                "min_date": meta.get("min_date", ""),
                "max_date": meta.get("max_date", ""),
            },
            "last_sync_at": meta.get("last_sync_at"),
            "last_sync_mode": meta.get("last_sync_mode"),
            "active_job": active_job,
            "model_settings": settings,
        }

    @application.post("/api/upload-zip")
    async def api_upload_zip(
        request: Request,
        file: UploadFile = File(...),
        mode: str = Form("merge"),
    ) -> dict[str, Any]:
        if mode not in ("merge", "reset"):
            raise HTTPException(status_code=400, detail="Chế độ nạp không hợp lệ (chỉ nhận 'merge' hoặc 'reset').")

        filename = file.filename or "misa_upload.zip"
        if not filename.lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="Vui lòng chọn đúng tệp nén đuôi .zip chứa các sổ Excel MISA.")

        target_db = _resolve_db_path(request)
        with tempfile.TemporaryDirectory(prefix="misa_upload_") as tmp_dir:
            tmp_zip = Path(tmp_dir) / Path(filename).name
            total_read = 0
            with tmp_zip.open("wb") as out_f:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total_read += len(chunk)
                    if total_read > MAX_ZIP_FILE_BYTES:
                        raise HTTPException(status_code=413, detail="Tệp .zip vượt quá giới hạn an toàn 250 MB.")
                    out_f.write(chunk)

            try:
                report = sync_misa_zip(tmp_zip, target_db, mode=mode, is_demo=False)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        job = start_analysis_job(db_path=target_db, import_report=report)
        return {
            "ok": True,
            "job_id": job["job_id"],
            "job": job,
            "import_report": report,
        }

    @application.post("/api/load-demo")
    def api_load_demo(request: Request) -> dict[str, Any]:
        target_db = _resolve_db_path(request)
        with tempfile.TemporaryDirectory(prefix="misa_demo_gen_") as tmp_dir:
            demo_zip = generate_demo_misa_zip(Path(tmp_dir) / "du_lieu_mau_misa_11_thang.zip", n_weeks=48, seed=42)
            report = sync_misa_zip(demo_zip, target_db, mode="reset", is_demo=True)

        job = start_analysis_job(db_path=target_db, import_report=report)
        return {
            "ok": True,
            "job_id": job["job_id"],
            "job": job,
            "import_report": report,
        }

    @application.get("/api/job/{job_id}")
    def api_job_progress(job_id: str) -> dict[str, Any]:
        st = get_job_status(job_id)
        if st is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy tiến trình phân tích này.")
        return {"ok": True, **st}

    @application.get("/api/dashboard")
    def api_dashboard(
        request: Request,
        primary_model: str | None = Query(default=None),
        compare_models: str | None = Query(default=None),
    ) -> dict[str, Any]:
        target_db = _resolve_db_path(request)
        parsed_compares: list[str] | None = None
        if compare_models is not None:
            parsed_compares = [c.strip() for c in compare_models.split(",") if c.strip()]
        payload = build_dashboard_payload(
            db_path=target_db,
            primary_model_id=primary_model,
            compare_model_ids=parsed_compares,
        )
        return {"ok": True, **payload}

    @application.post("/api/settings/models")
    def api_save_model_settings(
        request: Request,
        body: ModelSettingsPayload,
    ) -> dict[str, Any]:
        target_db = _resolve_db_path(request)
        try:
            saved = save_model_settings(
                primary_model_id=body.primary_model_id,
                compare_model_ids=body.compare_model_ids,
                db_path=target_db,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        dash = build_dashboard_payload(
            db_path=target_db,
            primary_model_id=saved["primary_model_id"],
            compare_model_ids=saved["compare_model_ids"],
        )
        return {"ok": True, "model_settings": saved, "dashboard": dash}

    @application.get("/api/export/reorder-csv")
    def api_export_reorder_csv(
        request: Request,
        primary_model: str | None = Query(default=None),
        hide_sensitive: bool = Query(default=False),
        include_all: bool = Query(default=False),
        warehouse: str | None = Query(default=None),
        demand_mult: float = Query(default=1.0),
    ) -> Response:
        target_db = _resolve_db_path(request)
        dash = build_dashboard_payload(db_path=target_db, primary_model_id=primary_model)
        if not dash.get("has_data"):
            raise HTTPException(status_code=400, detail="Chưa có dữ liệu kho để xuất danh sách nhập hàng.")

        mult = max(0.5, min(2.0, float(demand_mult or 1.0)))
        flow_mode = bool(dash["m2"].get("flow_mode_active", False))
        all_items = dash["m2"]["all_items"]
        if warehouse and warehouse != "all":
            all_items = [
                x for x in all_items
                if x["warehouse_code"] == warehouse or x["warehouse_anon"] == warehouse
            ]

        adjusted_items: list[dict[str, Any]] = []
        for orig in all_items:
            it = dict(orig)
            if abs(mult - 1.0) > 1e-4:
                fc1 = round(float(it["forecast_next_1w"]) * mult, 1)
                fc4 = round(float(it["forecast_next_4w"]) * mult, 1)
                it["forecast_next_1w"] = fc1
                it["forecast_next_4w"] = fc4
                upb = max(1.0, float(it["units_per_box"]))
                avail = float(it["est_batch_remaining_qty"]) if flow_mode else float(it["current_stock_qty"] or 0.0)
                if fc1 > 0 and (it["suggested_boxes"] > 0 or avail < fc1 * 1.2) and it["status_code"] not in ("dead_stock", "promo_surplus", "inactive_zero", "no_stock_book"):
                    shortage = max(fc1, fc1 * 2.0 - avail)
                    boxes = max(1, int(math.ceil(shortage / upb)))
                    units = int(round(boxes * upb))
                    it["suggested_boxes"] = boxes
                    it["suggested_units"] = units
                    it["estimated_order_cost_vnd"] = int(round(units * float(it["latest_purchase_cost"])))
                    box_uom = str(it.get("suggested_box_uom") or "thùng").lower()
                    base_uom = str(it.get("base_uom") or "đv").lower()
                    it["action_vi"] = (
                        f"Nên đặt thêm {boxes} {box_uom} ({units} {base_uom} cho 2 tuần tới)"
                        if upb > 1.0
                        else f"Nên đặt thêm {units} {base_uom} (đủ bán 2 tuần tới)"
                    )
            adjusted_items.append(it)

        rows_to_export = adjusted_items if include_all else [x for x in adjusted_items if x["suggested_boxes"] > 0]
        if not rows_to_export:
            rows_to_export = adjusted_items

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "Mã hàng",
                "Tên mặt hàng",
                "Kho",
                "Tồn hiện tại",
                "Đơn vị tính",
                "Bán TB/tuần",
                "Dự kiến tuần tới",
                "Dự kiến 4 tuần tới",
                "Tình trạng",
                "Số thùng đề xuất nhập",
                "Đơn vị thùng",
                "Quy cách (ĐV/thùng)",
                "Tổng số lượng đề xuất nhập",
                "Tiền nhập ước tính (VNĐ)",
                "Lý do & Ghi chú",
            ]
        )

        for item in rows_to_export:
            code = item["sku_anon"] if hide_sensitive else item["sku"]
            name = f"Mặt hàng {item['sku_anon']}" if hide_sensitive else item["product_name"]
            wh = item["warehouse_anon"] if hide_sensitive else item["warehouse_code"]
            stock_txt = item["current_stock_display_vi"] if not item["has_stock_data"] else str(item["current_stock_qty"])
            cost_txt = "Đã ẩn" if hide_sensitive else str(item["estimated_order_cost_vnd"])
            writer.writerow(
                [
                    code,
                    name,
                    wh,
                    stock_txt,
                    item["base_uom"],
                    item["avg_weekly_qty"],
                    item["forecast_next_1w"],
                    item["forecast_next_4w"],
                    item["status_vi"],
                    item["suggested_boxes"],
                    item["suggested_box_uom"],
                    int(round(item["units_per_box"])),
                    item["suggested_units"],
                    cost_txt,
                    f"{item['action_vi']} | {item['forecast_reason_vi']}",
                ]
            )

        csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
        return Response(
            content=csv_bytes,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="danh_sach_nhap_hang_misa.csv"',
                "Cache-Control": "no-store",
            },
        )

    return application


app = create_app()

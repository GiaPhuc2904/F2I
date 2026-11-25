"""Streaming XML parser chuẩn MISA (`.xlsx`) không dùng `openpyxl`, giữ tên thật phục vụ kho cục bộ.

Kế thừa logic đã kiểm chứng từ `reference/misa_excel_importer_ref.py`, bổ sung:
- Không hardcode ngày/tên kho/tên hãng.
- Hỗ trợ đủ 5 file chuẩn + File 2b dự phòng (`Tong_hop_ton_kho*.xlsx`).
- Trả về dữ liệu staging trong RAM để `sync_engine.py` thực hiện Date-Window Upsert không trùng dòng.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import re
from typing import Any, Iterator
import xml.etree.ElementTree as ET
import zipfile

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
EXCEL_EPOCH = date(1899, 12, 30)

_COL_RE = re.compile(r"^([A-Z]+)")
_DOC_PREFIX_RE = re.compile(r"^([A-Za-z]+)")


def norm_header(text: Any) -> str:
    """Chuẩn hóa tên cột tiếng Việt: bỏ khoảng trắng thừa, hạ thường."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text).strip()).lower()


def norm_key(text: Any) -> str:
    """Chuẩn hóa khóa định danh (mã hoặc tên) ổn định giữa các kỳ nạp .zip."""
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def _col_letters_to_idx(ref: str) -> int:
    m = _COL_RE.match(ref or "A")
    letters = m.group(1) if m else "A"
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def parse_excel_date(raw: Any) -> str | None:
    """Chuyển ngày từ Excel serial number hoặc chuỗi `dd/mm/yyyy` / `yyyy-mm-dd` sang `YYYY-MM-DD`."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()

    s = str(raw).strip()
    if not s:
        return None

    try:
        num = float(s)
        if 20_000 <= num <= 80_000:
            return (EXCEL_EPOCH + timedelta(days=int(num))).isoformat()
    except ValueError:
        pass

    m_iso = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m_iso:
        y, mo, da = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        return date(y, mo, da).isoformat()

    m_vn = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})", s)
    if m_vn:
        da, mo, y = int(m_vn.group(1)), int(m_vn.group(2)), int(m_vn.group(3))
        return date(y, mo, da).isoformat()

    return None


def parse_number(raw: Any, default: float = 0.0) -> float:
    """Chuyển đổi giá trị số từ ô Excel sang float an toàn."""
    if raw is None:
        return default
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return default
    if "," in s and "." in s:
        if s.rfind(",") < s.rfind("."):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return default


def _resolve_sheet_paths(zf: zipfile.ZipFile) -> list[str]:
    names = zf.namelist()
    ws_candidates = [n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
    if ws_candidates:
        def _sheet_sort_key(s: str) -> tuple[int, str]:
            m = re.search(r"sheet(\d+)\.xml$", s)
            return (int(m.group(1)) if m else 9999, s)
        return sorted(ws_candidates, key=_sheet_sort_key)
    raise ValueError("Không tìm thấy worksheet nào trong file .xlsx.")


def iter_xlsx_rows(path: str | Path) -> Iterator[tuple[int, dict[int, str]]]:
    """Đọc streaming từng dòng không rỗng `(row_num, {col_idx: str_val})` trên mọi sheet của file `.xlsx`."""
    p = Path(path)
    with zipfile.ZipFile(p, "r") as zf:
        sst: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            with zf.open("xl/sharedStrings.xml") as f_sst:
                for _, elem in ET.iterparse(f_sst, events=("end",)):
                    if elem.tag == f"{{{NS_MAIN}}}si":
                        txt = "".join(t.text or "" for t in elem.iter(f"{{{NS_MAIN}}}t"))
                        sst.append(txt)
                        elem.clear()

        sheet_paths = _resolve_sheet_paths(zf)
        row_tag = f"{{{NS_MAIN}}}row"
        cell_tag = f"{{{NS_MAIN}}}c"
        v_tag = f"{{{NS_MAIN}}}v"
        is_tag = f"{{{NS_MAIN}}}is"
        t_tag = f"{{{NS_MAIN}}}t"

        global_row_offset = 0
        for ws_path in sheet_paths:
            fallback_row_num = 0
            max_r_in_sheet = 0
            with zf.open(ws_path) as f_ws:
                for _, elem in ET.iterparse(f_ws, events=("end",)):
                    if elem.tag != row_tag:
                        continue
                    fallback_row_num += 1
                    raw_r = int(elem.attrib.get("r", fallback_row_num))
                    max_r_in_sheet = max(max_r_in_sheet, raw_r)
                    r_num = global_row_offset + raw_r
                    cells: dict[int, str] = {}
                    for c in elem:
                        if c.tag != cell_tag:
                            continue
                        ref = c.attrib.get("r")
                        col_idx = _col_letters_to_idx(ref) if ref else len(cells)
                        ctype = c.attrib.get("t")
                        val: str | None = None
                        if ctype == "s":
                            v_el = c.find(v_tag)
                            if v_el is not None and v_el.text is not None:
                                s_idx = int(v_el.text)
                                if 0 <= s_idx < len(sst):
                                    val = sst[s_idx]
                        elif ctype == "inlineStr":
                            is_el = c.find(is_tag)
                            if is_el is not None:
                                val = "".join(x.text or "" for x in is_el.iter(t_tag))
                        else:
                            v_el = c.find(v_tag)
                            if v_el is not None and v_el.text is not None:
                                val = v_el.text
                        if val is not None and val != "":
                            cells[col_idx] = val
                    elem.clear()
                    if cells:
                        yield r_num, cells
            global_row_offset += max_r_in_sheet + 10


def find_col(header_map: dict[int, str], *patterns: str, start_idx: int = 0) -> int | None:
    """Tìm chỉ số cột khớp tất cả các từ khóa con (theo thứ tự xuất hiện >= start_idx)."""
    for idx in sorted(header_map.keys()):
        if idx < start_idx:
            continue
        h = header_map[idx]
        if all(pat in h for pat in patterns):
            return idx
    return None


def _find_exact_or_contains(header_map: dict[int, str], exact: str, *fallback_patterns: str) -> int | None:
    for idx in sorted(header_map.keys()):
        if header_map[idx] == exact:
            return idx
    if fallback_patterns:
        return find_col(header_map, *fallback_patterns)
    return find_col(header_map, exact)


def parse_products_xlsx(path: str | Path) -> dict[str, Any]:
    """Đọc `Danh_sach_hang_hoa_dich_vu.xlsx` (header 2 tầng) ra danh sách dict sản phẩm."""
    p = Path(path)
    header_r1: dict[int, str] = {}
    header_r2: dict[int, str] = {}
    header_row_num: int | None = None
    rows: list[dict[str, Any]] = []
    seen_skus: set[str] = set()

    for r_num, cells in iter_xlsx_rows(p):
        norm_vals = {i: norm_header(v) for i, v in cells.items()}
        if header_row_num is None:
            vals_set = set(norm_vals.values())
            if "mã" in vals_set and "tên" in vals_set and ("đơn vị tính chính" in vals_set or "tính chất" in vals_set):
                header_row_num = r_num
                header_r1 = norm_vals
            continue

        if r_num == header_row_num + 1 and (
            "tỷ lệ chuyển đổi" in norm_vals.values()
            or "đơn vị chuyển đổi" in norm_vals.values()
            or "số lượng từ" in norm_vals.values()
        ):
            header_r2 = norm_vals
            continue

        c_sku = _find_exact_or_contains(header_r1, "mã")
        c_name = _find_exact_or_contains(header_r1, "tên")
        if c_sku is None or c_name is None:
            raise ValueError(f"Thiếu cột Mã/Tên trong {p.name}")

        sku_raw = str(cells.get(c_sku, "")).strip()
        if not sku_raw or sku_raw.lower() in ("mã", "cộng", "tổng cộng"):
            continue
        if sku_raw in seen_skus:
            continue
        seen_skus.add(sku_raw)

        c_cat = find_col(header_r1, "nhóm vthh")
        c_uom = find_col(header_r1, "đơn vị tính chính")
        c_wh = find_col(header_r1, "mã kho ngầm định") or find_col(header_r1, "kho ngầm định")
        c_cost = find_col(header_r1, "đơn giá mua gần nhất") or find_col(header_r1, "đơn giá mua cố định")
        c_price = find_col(header_r1, "đơn giá bán 1")
        c_min_stock = find_col(header_r1, "số lượng tồn tối thiểu")
        c_status = find_col(header_r1, "trạng thái")

        c_conv_uom = find_col(header_r2, "đơn vị chuyển đổi") or find_col(header_r1, "đơn vị chuyển đổi")
        c_conv_rate = find_col(header_r2, "tỷ lệ chuyển đổi") or find_col(header_r1, "tỷ lệ chuyển đổi")
        c_conv_op = find_col(header_r2, "phép tính") or find_col(header_r1, "phép tính")

        box_uom = str(cells.get(c_conv_uom, "")).strip() if c_conv_uom is not None else ""
        conv_rate = parse_number(cells.get(c_conv_rate), 1.0) if c_conv_rate is not None else 1.0
        conv_op = norm_header(cells.get(c_conv_op, "")) if c_conv_op is not None else ""
        if conv_rate <= 0:
            conv_rate = 1.0
        elif "chia" in conv_op and conv_rate > 0:
            conv_rate = 1.0 / conv_rate

        is_promo = 1 if sku_raw.upper().endswith("_KM") else 0
        rows.append(
            {
                "product_id": sku_raw,
                "sku": sku_raw,
                "product_name": str(cells.get(c_name, "")).strip() or sku_raw,
                "category": str(cells.get(c_cat, "")).strip() if c_cat is not None else "",
                "warehouse_code": str(cells.get(c_wh, "")).strip() if c_wh is not None else "",
                "base_uom": str(cells.get(c_uom, "")).strip() if c_uom is not None else "",
                "box_uom": box_uom,
                "units_per_box": round(conv_rate, 4),
                "is_promo_sku": is_promo,
                "latest_purchase_cost": parse_number(cells.get(c_cost), 0.0) if c_cost is not None else 0.0,
                "selling_price_1": parse_number(cells.get(c_price), 0.0) if c_price is not None else 0.0,
                "min_stock_qty": parse_number(cells.get(c_min_stock), 0.0) if c_min_stock is not None else 0.0,
                "shelf_life_days": None,
                "status": str(cells.get(c_status, "")).strip() if c_status is not None else "",
            }
        )

    return {
        "rows": rows,
        "meta": {
            "rows_parsed": len(rows),
            "promo_skus": sum(1 for r in rows if r["is_promo_sku"] == 1),
            "has_box_conversion": sum(1 for r in rows if r["units_per_box"] > 1.0),
        },
    }


def parse_inventory_ledger_xlsx(path: str | Path) -> dict[str, Any]:
    """Đọc `So_chi_tiet_vat_tu_hang_hoa.xlsx` (header 2 tầng, hỗ trợ cả dòng tồn đầu kỳ không có ngày chứng từ)."""
    p = Path(path)
    header_r1: dict[int, str] = {}
    header_r2: dict[int, str] = {}
    header_row_num: int | None = None
    rows: list[dict[str, Any]] = []
    pending_openings: list[dict[str, Any]] = []
    skipped_summary = 0
    current_block_sku: str = ""
    current_block_name: str = ""
    current_block_wh: str = ""

    for r_num, cells in iter_xlsx_rows(p):
        norm_vals = {i: norm_header(v) for i, v in cells.items()}
        vals_set = set(norm_vals.values())
        if "mã hàng" in vals_set and ("nhập" in vals_set or "xuất" in vals_set or "tồn" in vals_set):
            header_row_num = r_num
            header_r1 = norm_vals
            continue

        if header_row_num is None:
            continue

        if r_num == header_row_num + 1 and "số lượng" in vals_set:
            header_r2 = norm_vals
            continue

        c_wh = find_col(header_r1, "tên kho")
        if c_wh is None:
            c_wh = find_col(header_r1, "mã kho")
        c_sku = find_col(header_r1, "mã hàng")
        c_name = find_col(header_r1, "tên hàng")
        c_post = find_col(header_r1, "ngày hạch toán")
        c_doc_d = find_col(header_r1, "ngày chứng từ")
        c_doc_no = find_col(header_r1, "số chứng từ")
        c_desc = find_col(header_r1, "diễn giải")
        c_uom = find_col(header_r1, "đvt")
        c_price = find_col(header_r1, "đơn giá")
        c_in_base = find_col(header_r1, "nhập")
        c_out_base = find_col(header_r1, "xuất")
        c_bal_base = find_col(header_r1, "tồn")

        if c_sku is None or c_in_base is None or c_out_base is None or c_bal_base is None:
            raise ValueError(f"Thiếu cột cốt lõi trong {p.name}")

        first_txt = str(cells.get(0, "")).strip()
        if first_txt.lower().startswith("tên kho:"):
            current_block_wh = first_txt.split(":", 1)[1].strip()
            continue
        if first_txt.lower().startswith(("mã hàng:", "vật tư hàng hóa:")):
            after = first_txt.split(":", 1)[1].strip()
            if " - " in after:
                p_code, p_nm = after.split(" - ", 1)
                current_block_sku = p_code.strip()
                current_block_name = p_nm.strip()
            else:
                current_block_sku = after
                current_block_name = after
            continue

        sku = str(cells.get(c_sku, "")).strip() or current_block_sku
        if sku.lower() in ("mã hàng", "cộng", "tổng cộng", "cộng nhóm", "tổng"):
            skipped_summary += 1
            continue

        desc_txt = str(cells.get(c_desc, "")).strip().lower() if c_desc is not None else ""
        if desc_txt in ("cộng", "tổng cộng", "cộng cuối kỳ"):
            skipped_summary += 1
            continue

        doc_d = parse_excel_date(cells.get(c_doc_d if c_doc_d is not None else c_post))
        post_d = parse_excel_date(cells.get(c_post)) if c_post is not None else doc_d
        wh_val = (str(cells.get(c_wh, "")).strip() if c_wh is not None else "") or current_block_wh
        name_val = (str(cells.get(c_name, "")).strip() if c_name is not None else "") or current_block_name or sku

        if sku and not doc_d and ("đầu kỳ" in desc_txt or "tồn đầu" in desc_txt):
            bal_q = parse_number(cells.get(c_bal_base), 0.0)
            bal_v = parse_number(cells.get(c_bal_base + 1), 0.0)
            op_row = {
                "warehouse_code": wh_val,
                "product_id": sku,
                "product_name": name_val,
                "posting_date": None,
                "doc_date": None,
                "doc_id": f"OPEN_{sku}",
                "doc_type": "OPENING",
                "uom": str(cells.get(c_uom, "")).strip() if c_uom is not None else "",
                "unit_cost": parse_number(cells.get(c_price), 0.0) if c_price is not None else 0.0,
                "in_qty": 0.0,
                "in_value": 0.0,
                "out_qty": 0.0,
                "out_value": 0.0,
                "balance_qty": bal_q,
                "balance_value": bal_v,
            }
            rows.append(op_row)
            pending_openings.append(op_row)
            continue

        if not sku or not doc_d:
            skipped_summary += 1
            continue

        raw_doc = str(cells.get(c_doc_no, "")).strip() if c_doc_no is not None else ""
        m_pre = _DOC_PREFIX_RE.match(raw_doc.upper())
        doc_type = m_pre.group(1) if m_pre else "OTHER"

        rows.append(
            {
                "warehouse_code": wh_val,
                "product_id": sku,
                "product_name": name_val,
                "posting_date": post_d,
                "doc_date": doc_d,
                "doc_id": raw_doc or f"INV_{len(rows) + 1:06d}",
                "doc_type": doc_type,
                "uom": str(cells.get(c_uom, "")).strip() if c_uom is not None else "",
                "unit_cost": parse_number(cells.get(c_price), 0.0) if c_price is not None else 0.0,
                "in_qty": parse_number(cells.get(c_in_base), 0.0),
                "in_value": parse_number(cells.get(c_in_base + 1), 0.0),
                "out_qty": parse_number(cells.get(c_out_base), 0.0),
                "out_value": parse_number(cells.get(c_out_base + 1), 0.0),
                "balance_qty": parse_number(cells.get(c_bal_base), 0.0),
                "balance_value": parse_number(cells.get(c_bal_base + 1), 0.0),
            }
        )

    valid_dates = [r["doc_date"] for r in rows if r["doc_date"]]
    fallback_open_d = min(valid_dates) if valid_dates else date.today().replace(month=1, day=1).isoformat()
    for op_r in pending_openings:
        op_r["doc_date"] = fallback_open_d
        op_r["posting_date"] = fallback_open_d

    dates = [r["doc_date"] for r in rows if r["doc_date"]]
    return {
        "rows": rows,
        "meta": {
            "rows_parsed": len(rows),
            "skipped_summary_rows": skipped_summary,
            "zero_balance_events": sum(1 for r in rows if r["balance_qty"] <= 0.0 and r["out_qty"] > 0.0),
            "unique_skus": len({r["product_id"] for r in rows}),
            "min_date": min(dates) if dates else None,
            "max_date": max(dates) if dates else None,
            "header_r2_detected": bool(header_r2),
        },
    }


def parse_inventory_summary_xlsx(path: str | Path) -> dict[str, Any]:
    """Đọc File 2b dự phòng `Tong_hop_ton_kho*.xlsx` (header 2 tầng: Đầu kỳ, Nhập kho, Xuất kho, Cuối kỳ)."""
    p = Path(path)
    header_r1: dict[int, str] = {}
    header_r2: dict[int, str] = {}
    header_row_num: int | None = None
    rows: list[dict[str, Any]] = []
    as_of_date: str | None = None

    for r_num, cells in iter_xlsx_rows(p):
        norm_vals = {i: norm_header(v) for i, v in cells.items()}
        if header_row_num is None:
            # Thử bắt ngày chốt kỳ ở các dòng tiêu đề đầu bảng (vd: "Từ ngày ... đến ngày 31/12/2025")
            for v in cells.values():
                m = re.search(r"đến\s+ngày\s+(\d{1,2}[/-]\d{1,2}[/-]\d{4})", str(v), re.IGNORECASE)
                if m:
                    as_of_date = parse_excel_date(m.group(1))
            vals_set = set(norm_vals.values())
            if "mã hàng" in vals_set and ("cuối kỳ" in vals_set or "đầu kỳ" in vals_set):
                header_row_num = r_num
                header_r1 = norm_vals
            continue

        if r_num == header_row_num + 1 and ("số lượng" in norm_vals.values() or "sl" in norm_vals.values()):
            header_r2 = norm_vals
            continue

        c_wh = find_col(header_r1, "tên kho") or find_col(header_r1, "mã kho")
        c_sku = find_col(header_r1, "mã hàng")
        c_name = find_col(header_r1, "tên hàng")
        c_uom = find_col(header_r1, "đvt")
        c_open = find_col(header_r1, "đầu kỳ")
        c_in = find_col(header_r1, "nhập")
        c_out = find_col(header_r1, "xuất")
        c_close = find_col(header_r1, "cuối kỳ")
        c_price = find_col(header_r1, "đơn giá bán")

        if c_sku is None or c_close is None:
            raise ValueError(f"Thiếu cột Mã hàng / Cuối kỳ trong {p.name}")

        sku = str(cells.get(c_sku, "")).strip()
        if not sku or sku.lower() in ("mã hàng", "cộng", "tổng cộng"):
            continue

        wh = str(cells.get(c_wh, "")).strip() if c_wh is not None else "DEFAULT"
        rows.append(
            {
                "warehouse_code": wh or "DEFAULT",
                "product_id": sku,
                "product_name": str(cells.get(c_name, "")).strip() if c_name is not None else sku,
                "uom": str(cells.get(c_uom, "")).strip() if c_uom is not None else "",
                "opening_qty": parse_number(cells.get(c_open), 0.0) if c_open is not None else 0.0,
                "opening_value": parse_number(cells.get(c_open + 1), 0.0) if c_open is not None else 0.0,
                "in_qty": parse_number(cells.get(c_in), 0.0) if c_in is not None else 0.0,
                "in_value": parse_number(cells.get(c_in + 1), 0.0) if c_in is not None else 0.0,
                "out_qty": parse_number(cells.get(c_out), 0.0) if c_out is not None else 0.0,
                "out_value": parse_number(cells.get(c_out + 1), 0.0) if c_out is not None else 0.0,
                "closing_qty": parse_number(cells.get(c_close), 0.0),
                "closing_value": parse_number(cells.get(c_close + 1), 0.0),
                "selling_price_1": parse_number(cells.get(c_price), 0.0) if c_price is not None else 0.0,
                "as_of_date": as_of_date,
            }
        )

    return {
        "rows": rows,
        "meta": {
            "rows_parsed": len(rows),
            "unique_skus": len({r["product_id"] for r in rows}),
            "as_of_date": as_of_date,
            "header_r2_detected": bool(header_r2),
        },
    }


def parse_purchases_xlsx(path: str | Path) -> dict[str, Any]:
    """Đọc `So_chi_tiet_mua_hang.xlsx` -> danh sách dòng chi tiết, phiếu nhập, nhà cung cấp thật."""
    p = Path(path)
    header: dict[int, str] = {}
    header_row_num: int | None = None
    detail_rows: list[dict[str, Any]] = []
    inbound_agg: dict[str, dict[str, Any]] = {}
    suppliers: dict[str, dict[str, str]] = {}
    product_fallbacks: dict[str, dict[str, Any]] = {}

    for r_num, cells in iter_xlsx_rows(p):
        norm_vals = {i: norm_header(v) for i, v in cells.items()}
        if header_row_num is None:
            vals_set = set(norm_vals.values())
            if "mã hàng" in vals_set and ("số lượng mua" in vals_set or "giá trị mua" in vals_set):
                header_row_num = r_num
                header = norm_vals
            continue

        c_doc_d = find_col(header, "ngày chứng từ") or find_col(header, "ngày hạch toán")
        c_doc_no = find_col(header, "số chứng từ")
        c_sup_code = find_col(header, "mã nhà cung cấp")
        c_sup_name = find_col(header, "tên nhà cung cấp")
        c_sku = find_col(header, "mã hàng")
        c_name = find_col(header, "tên hàng")
        c_uom = find_col(header, "đvt")
        c_qty = find_col(header, "số lượng mua")
        c_price = find_col(header, "đơn giá")
        c_gross = find_col(header, "giá trị mua")
        c_disc = find_col(header, "chiết khấu")
        c_ret_qty = find_col(header, "số lượng trả lại")
        c_ret_val = find_col(header, "giá trị trả lại")
        c_red_val = find_col(header, "giá trị giảm giá")

        if c_sku is None or c_doc_d is None:
            raise ValueError(f"Thiếu cột cốt lõi trong {p.name}")

        sku = str(cells.get(c_sku, "")).strip()
        inb_d = parse_excel_date(cells.get(c_doc_d))
        if not sku or not inb_d:
            continue

        raw_doc = str(cells.get(c_doc_no, "")).strip() if c_doc_no is not None else ""
        inb_id = raw_doc or f"NK_{len(detail_rows) + 1:06d}"

        sup_code = str(cells.get(c_sup_code, "")).strip() if c_sup_code is not None else ""
        sup_name = str(cells.get(c_sup_name, "")).strip() if c_sup_name is not None else ""
        sup_key = norm_key(sup_code or sup_name) or "sup_unknown"
        suppliers[sup_key] = {
            "supplier_id": sup_key,
            "supplier_code": sup_code or sup_key,
            "supplier_name": sup_name or sup_code or "Nhà cung cấp chưa rõ",
        }

        qty_gross = parse_number(cells.get(c_qty), 0.0) if c_qty is not None else 0.0
        qty_ret = parse_number(cells.get(c_ret_qty), 0.0) if c_ret_qty is not None else 0.0
        qty_net = qty_gross - qty_ret
        unit_cost = parse_number(cells.get(c_price), 0.0) if c_price is not None else 0.0
        gross_amt = parse_number(cells.get(c_gross), 0.0) if c_gross is not None else 0.0
        disc_amt = parse_number(cells.get(c_disc), 0.0) if c_disc is not None else 0.0
        ret_amt = (parse_number(cells.get(c_ret_val), 0.0) if c_ret_val is not None else 0.0) + (
            parse_number(cells.get(c_red_val), 0.0) if c_red_val is not None else 0.0
        )
        net_amt = gross_amt - disc_amt - ret_amt
        disc_rate = round(disc_amt / gross_amt, 6) if gross_amt > 0 else 0.0

        uom = str(cells.get(c_uom, "")).strip() if c_uom is not None else ""
        pname = str(cells.get(c_name, "")).strip() if c_name is not None else sku
        product_fallbacks.setdefault(
            sku,
            {"product_id": sku, "sku": sku, "product_name": pname, "base_uom": uom, "latest_purchase_cost": unit_cost},
        )

        detail_rows.append(
            {
                "inbound_id": inb_id,
                "supplier_id": sup_key,
                "inbound_date": inb_d,
                "product_id": sku,
                "uom": uom,
                "qty_gross": qty_gross,
                "qty_return": qty_ret,
                "qty_net": qty_net,
                "unit_cost": unit_cost,
                "gross_amount": gross_amt,
                "discount_amount": disc_amt,
                "discount_rate": disc_rate,
                "return_amount": ret_amt,
                "net_amount": net_amt,
                "expiry_date": None,
            }
        )

        agg = inbound_agg.setdefault(
            inb_id,
            {
                "inbound_id": inb_id,
                "supplier_id": sup_key,
                "inbound_date": inb_d,
                "gross_amount": 0.0,
                "discount_amount": 0.0,
                "return_amount": 0.0,
                "net_amount": 0.0,
            },
        )
        agg["gross_amount"] += gross_amt
        agg["discount_amount"] += disc_amt
        agg["return_amount"] += ret_amt
        agg["net_amount"] += net_amt

    dates = [r["inbound_date"] for r in detail_rows]
    return {
        "inbounds": list(inbound_agg.values()),
        "details": detail_rows,
        "suppliers": list(suppliers.values()),
        "product_fallbacks": list(product_fallbacks.values()),
        "meta": {
            "inbounds_count": len(inbound_agg),
            "detail_rows": len(detail_rows),
            "suppliers_count": len(suppliers),
            "discounted_lines": sum(1 for r in detail_rows if r["discount_amount"] > 0),
            "min_date": min(dates) if dates else None,
            "max_date": max(dates) if dates else None,
        },
    }


def parse_ar_ledger_xlsx(path: str | Path) -> dict[str, Any]:
    """Đọc `Chi_tiet_cong_no_phai_thu_khach_hang.xlsx` -> block KH thật, đối chiếu checksum `Cộng`."""
    p = Path(path)
    header_r1: dict[int, str] = {}
    header_r2: dict[int, str] = {}
    header_row_num: int | None = None

    current_cust_key: str | None = None
    current_cust_name: str = ""
    block_seq = 0
    block_debit_sum = 0.0
    block_credit_sum = 0.0
    checksum_total_blocks = 0
    checksum_matched_blocks = 0

    ledger_rows: list[dict[str, Any]] = []
    pending_openings: list[dict[str, Any]] = []
    cust_info: dict[str, dict[str, Any]] = {}
    doc_to_customer: dict[str, dict[str, str]] = {}

    for r_num, cells in iter_xlsx_rows(p):
        norm_vals = {i: norm_header(v) for i, v in cells.items()}
        if header_row_num is None:
            vals_set = set(norm_vals.values())
            if "ngày hạch toán" in vals_set and ("phát sinh" in vals_set or "tk đối ứng" in vals_set):
                header_row_num = r_num
                header_r1 = norm_vals
            continue

        if r_num == header_row_num + 1 and "nợ" in norm_vals.values() and "có" in norm_vals.values():
            header_r2 = norm_vals
            continue

        c_post = find_col(header_r1, "ngày hạch toán") or 0
        c_doc_d = find_col(header_r1, "ngày chứng từ") or 1
        c_doc_no = find_col(header_r1, "số chứng từ") or 2
        c_desc = find_col(header_r1, "diễn giải") or 5
        c_ar_acc = find_col(header_r1, "tk công nợ") or 6
        c_contra = find_col(header_r1, "tk đối ứng") or 7
        c_ps = find_col(header_r1, "phát sinh") or 8
        c_sd = find_col(header_r1, "số dư") or 10

        first_cell = str(cells.get(c_post, "")).strip()
        desc_cell = str(cells.get(c_desc, "")).strip()

        # 1. Dòng đầu block khách hàng
        if first_cell.lower().startswith("tên khách hàng"):
            block_seq += 1
            raw_cust = first_cell.split(":", 1)[1].strip() if ":" in first_cell else ""
            if not raw_cust:
                raw_cust = str(cells.get(c_doc_d, "")).strip()
            current_cust_name = raw_cust or f"Khách hàng #{block_seq}"
            current_cust_key = norm_key(current_cust_name) or f"ar_block_{block_seq}"
            cust_info.setdefault(
                current_cust_key,
                {
                    "customer_id": current_cust_key,
                    "customer_code": "",
                    "customer_name": current_cust_name,
                    "dates": [],
                },
            )
            block_debit_sum = 0.0
            block_credit_sum = 0.0
            continue

        # 1b. Dòng Số dư đầu kỳ
        if "số dư đầu kỳ" in desc_cell.lower() or "dư đầu kỳ" in desc_cell.lower():
            if current_cust_key is not None:
                open_dr = parse_number(cells.get(c_sd), 0.0)
                open_cr = parse_number(cells.get(c_sd + 1), 0.0)
                if open_dr > 0 or open_cr > 0:
                    open_d = parse_excel_date(cells.get(c_doc_d)) or parse_excel_date(cells.get(c_post))
                    row = {
                        "customer_id": current_cust_key,
                        "posting_date": open_d,
                        "doc_date": open_d,
                        "doc_id": f"OPEN_{current_cust_key}",
                        "ar_account": "131",
                        "contra_account": "OPENING",
                        "debit_amount": open_dr,
                        "credit_amount": open_cr,
                        "balance_debit": open_dr,
                        "balance_credit": open_cr,
                    }
                    ledger_rows.append(row)
                    if open_d is None:
                        pending_openings.append(row)
            continue

        # 2. Dòng cuối block (`Cộng`) -> đối chiếu checksum
        if desc_cell.lower() in ("cộng", "tổng cộng") or first_cell.lower() in ("cộng", "tổng cộng"):
            if current_cust_key is not None:
                reported_debit = parse_number(cells.get(c_ps), 0.0)
                reported_credit = parse_number(cells.get(c_ps + 1), 0.0)
                checksum_total_blocks += 1
                if abs(reported_debit - block_debit_sum) <= 1.0 and abs(reported_credit - block_credit_sum) <= 1.0:
                    checksum_matched_blocks += 1
                current_cust_key = None
                current_cust_name = ""
                block_debit_sum = 0.0
                block_credit_sum = 0.0
            continue

        # 3. Dòng giao dịch thật
        doc_d = parse_excel_date(cells.get(c_doc_d)) or parse_excel_date(cells.get(c_post))
        post_d = parse_excel_date(cells.get(c_post)) or doc_d
        if not doc_d:
            continue

        if current_cust_key is None:
            block_seq += 1
            current_cust_name = f"Khách hàng #{block_seq}"
            current_cust_key = f"ar_block_{block_seq}"
            cust_info.setdefault(
                current_cust_key,
                {
                    "customer_id": current_cust_key,
                    "customer_code": "",
                    "customer_name": current_cust_name,
                    "dates": [],
                },
            )

        raw_doc = str(cells.get(c_doc_no, "")).strip()
        doc_id = raw_doc.upper() if raw_doc else f"AR_{len(ledger_rows) + 1:06d}"
        if raw_doc:
            doc_to_customer[doc_id] = {
                "customer_id": current_cust_key,
                "customer_name": current_cust_name,
            }

        ar_acc = str(cells.get(c_ar_acc, "")).strip()
        contra_acc = str(cells.get(c_contra, "")).strip()
        debit = parse_number(cells.get(c_ps), 0.0)
        credit = parse_number(cells.get(c_ps + 1), 0.0)
        bal_dr = parse_number(cells.get(c_sd), 0.0)
        bal_cr = parse_number(cells.get(c_sd + 1), 0.0)

        block_debit_sum += debit
        block_credit_sum += credit
        cust_info[current_cust_key]["dates"].append(doc_d)

        ledger_rows.append(
            {
                "customer_id": current_cust_key,
                "posting_date": post_d,
                "doc_date": doc_d,
                "doc_id": doc_id,
                "ar_account": ar_acc,
                "contra_account": contra_acc,
                "debit_amount": debit,
                "credit_amount": credit,
                "balance_debit": bal_dr,
                "balance_credit": bal_cr,
            }
        )

    # Gán ngày đầu kỳ động theo ngày giao dịch sớm nhất trong file (không hardcode năm!)
    valid_dates = [r["doc_date"] for r in ledger_rows if r["doc_date"]]
    fallback_open_date = min(valid_dates) if valid_dates else date.today().replace(month=1, day=1).isoformat()
    for row in pending_openings:
        row["doc_date"] = fallback_open_date
        row["posting_date"] = fallback_open_date
        cust_info[row["customer_id"]]["dates"].append(fallback_open_date)

    customers_out = []
    for cid, info in cust_info.items():
        ds = info["dates"]
        customers_out.append(
            {
                "customer_id": cid,
                "customer_code": info["customer_code"],
                "customer_name": info["customer_name"],
                "source_flags": "AR_131",
                "first_date": min(ds) if ds else None,
                "last_date": max(ds) if ds else None,
            }
        )

    all_dates = [r["doc_date"] for r in ledger_rows if r["doc_date"]]
    return {
        "rows": ledger_rows,
        "customers": customers_out,
        "doc_to_customer": doc_to_customer,
        "meta": {
            "rows_parsed": len(ledger_rows),
            "customers_count": len(customers_out),
            "checksum_blocks_total": checksum_total_blocks,
            "checksum_blocks_matched": checksum_matched_blocks,
            "header_r2_detected": bool(header_r2),
            "min_date": min(all_dates) if all_dates else None,
            "max_date": max(all_dates) if all_dates else None,
        },
    }


def parse_sales_xlsx(
    path: str | Path,
    ar_doc_to_customer: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Đọc `So_chi_tiet_ban_hang.xlsx` -> `outbounds`, `outbound_details` (giữ `cogs`, tách dòng hàng tặng 0đ)."""
    p = Path(path)
    ar_links = ar_doc_to_customer or {}
    header: dict[int, str] = {}
    header_row_num: int | None = None
    detail_rows: list[dict[str, Any]] = []
    out_agg: dict[str, dict[str, Any]] = {}
    customers: dict[str, dict[str, Any]] = {}
    product_fallbacks: dict[str, dict[str, Any]] = {}

    for r_num, cells in iter_xlsx_rows(p):
        norm_vals = {i: norm_header(v) for i, v in cells.items()}
        if header_row_num is None:
            vals_set = set(norm_vals.values())
            if "mã hàng" in vals_set and ("doanh số bán" in vals_set or "tổng số lượng bán" in vals_set):
                header_row_num = r_num
                header = norm_vals
            continue

        c_doc_d = find_col(header, "ngày chứng từ") or find_col(header, "ngày hạch toán")
        c_doc_no = find_col(header, "số chứng từ")
        c_cust_code = find_col(header, "mã khách hàng")
        c_cust_name = find_col(header, "tên khách hàng")
        c_sku = find_col(header, "mã hàng")
        c_name = find_col(header, "tên hàng")
        c_uom = find_col(header, "đvt")
        c_qty = find_col(header, "tổng số lượng bán") or find_col(header, "số lượng bán")
        c_price = find_col(header, "đơn giá")
        c_base_uom = find_col(header, "đơn vị chính")
        c_conv = find_col(header, "tỷ lệ chuyển đổi")
        c_rev = find_col(header, "doanh số bán")
        c_disc = find_col(header, "chiết khấu")
        c_ret_qty = find_col(header, "tổng số lượng trả lại") or find_col(header, "số lượng trả lại")
        c_ret_val = find_col(header, "giá trị trả lại")
        c_red_val = find_col(header, "giá trị giảm giá")
        c_cogs = find_col(header, "giá vốn")
        c_wh = find_col(header, "mã kho")

        if c_sku is None or c_doc_d is None:
            raise ValueError(f"Thiếu cột cốt lõi trong {p.name}")

        sku = str(cells.get(c_sku, "")).strip()
        out_d = parse_excel_date(cells.get(c_doc_d))
        if not sku or not out_d:
            continue

        raw_doc = str(cells.get(c_doc_no, "")).strip() if c_doc_no is not None else ""
        out_id = raw_doc.upper() if raw_doc else f"BH_{len(detail_rows) + 1:06d}"

        raw_cust_code = str(cells.get(c_cust_code, "")).strip() if c_cust_code is not None else ""
        raw_cust_name = str(cells.get(c_cust_name, "")).strip() if c_cust_name is not None else ""

        if out_id in ar_links:
            cust_id = ar_links[out_id]["customer_id"]
            cust_name = raw_cust_name or ar_links[out_id]["customer_name"]
        else:
            key_src = raw_cust_name or raw_cust_code
            cust_id = norm_key(key_src) if key_src else "kh_le_cod"
            cust_name = raw_cust_name or raw_cust_code or "Khách lẻ"

        c_rec = customers.setdefault(
            cust_id,
            {
                "customer_id": cust_id,
                "customer_code": raw_cust_code,
                "customer_name": cust_name,
                "source_flags": "SALES",
                "dates": [],
            },
        )
        if raw_cust_code and not c_rec["customer_code"]:
            c_rec["customer_code"] = raw_cust_code
        if raw_cust_name and (not c_rec["customer_name"] or c_rec["customer_name"] == "Khách lẻ"):
            c_rec["customer_name"] = raw_cust_name
        c_rec["dates"].append(out_d)

        wh = str(cells.get(c_wh, "")).strip() if c_wh is not None else ""
        uom = str(cells.get(c_uom, "")).strip() if c_uom is not None else ""
        base_uom = str(cells.get(c_base_uom, "")).strip() if c_base_uom is not None else uom
        conv = parse_number(cells.get(c_conv), 1.0) if c_conv is not None else 1.0
        if conv <= 0:
            conv = 1.0

        qty_gross = parse_number(cells.get(c_qty), 0.0) if c_qty is not None else 0.0
        qty_ret = parse_number(cells.get(c_ret_qty), 0.0) if c_ret_qty is not None else 0.0
        qty_net = qty_gross - qty_ret
        base_qty_net = qty_net * conv

        unit_price = parse_number(cells.get(c_price), 0.0) if c_price is not None else 0.0
        gross_rev = parse_number(cells.get(c_rev), 0.0) if c_rev is not None else 0.0
        disc_amt = parse_number(cells.get(c_disc), 0.0) if c_disc is not None else 0.0
        ret_amt = (parse_number(cells.get(c_ret_val), 0.0) if c_ret_val is not None else 0.0) + (
            parse_number(cells.get(c_red_val), 0.0) if c_red_val is not None else 0.0
        )
        net_rev = gross_rev - disc_amt - ret_amt
        cogs = parse_number(cells.get(c_cogs), 0.0) if c_cogs is not None else 0.0
        is_free_gift = 1 if (qty_net > 0 and gross_rev <= 0.0) else 0

        pname = str(cells.get(c_name, "")).strip() if c_name is not None else sku
        product_fallbacks.setdefault(
            sku,
            {
                "product_id": sku,
                "sku": sku,
                "product_name": pname,
                "warehouse_code": wh,
                "base_uom": base_uom,
                "selling_price_1": unit_price,
            },
        )

        detail_rows.append(
            {
                "outbound_id": out_id,
                "customer_id": cust_id,
                "outbound_date": out_d,
                "warehouse_code": wh,
                "product_id": sku,
                "uom": uom,
                "base_uom": base_uom,
                "conversion_rate": conv,
                "qty_gross": qty_gross,
                "qty_return": qty_ret,
                "qty_net": qty_net,
                "base_qty_net": base_qty_net,
                "unit_price": unit_price,
                "gross_revenue": gross_rev,
                "discount_amount": disc_amt,
                "return_amount": ret_amt,
                "net_revenue": net_rev,
                "cogs": cogs,
                "is_free_gift_line": is_free_gift,
            }
        )

        agg = out_agg.setdefault(
            out_id,
            {
                "outbound_id": out_id,
                "customer_id": cust_id,
                "outbound_date": out_d,
                "warehouse_code": wh,
                "gross_revenue": 0.0,
                "discount_amount": 0.0,
                "return_amount": 0.0,
                "net_revenue": 0.0,
                "total_cogs": 0.0,
            },
        )
        agg["gross_revenue"] += gross_rev
        agg["discount_amount"] += disc_amt
        agg["return_amount"] += ret_amt
        agg["net_revenue"] += net_rev
        agg["total_cogs"] += cogs

    outbounds_out = []
    for agg in out_agg.values():
        agg["gross_profit"] = agg["net_revenue"] - agg["total_cogs"]
        outbounds_out.append(agg)

    customers_out = []
    for c in customers.values():
        ds = c["dates"]
        customers_out.append(
            {
                "customer_id": c["customer_id"],
                "customer_code": c["customer_code"],
                "customer_name": c["customer_name"],
                "source_flags": c["source_flags"],
                "first_date": min(ds) if ds else None,
                "last_date": max(ds) if ds else None,
            }
        )

    dates = [r["outbound_date"] for r in detail_rows]
    return {
        "outbounds": outbounds_out,
        "details": detail_rows,
        "customers": customers_out,
        "product_fallbacks": list(product_fallbacks.values()),
        "meta": {
            "outbounds_count": len(outbounds_out),
            "detail_rows": len(detail_rows),
            "free_gift_lines": sum(1 for r in detail_rows if r["is_free_gift_line"] == 1),
            "matched_via_ar_doc_no": sum(1 for o in outbounds_out if o["outbound_id"] in ar_links),
            "min_date": min(dates) if dates else None,
            "max_date": max(dates) if dates else None,
        },
    }

"""Trình sinh file `.zip` mẫu giả lập đúng khuôn 5 sổ Excel MISA (100% dữ liệu giả lập, không cần openpyxl).

Dùng cho:
- Bộ kiểm thử `pytest` (Phase 1–4).
- Nút "Dùng thử dữ liệu mẫu" trên giao diện khi mới cài đặt.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import random
import tempfile
from typing import Any
from xml.sax.saxutils import escape
import zipfile

EXCEL_EPOCH = date(1899, 12, 30)


def _col_letter(col_idx: int) -> str:
    """Chuyển chỉ số cột 0-based sang ký tự Excel (0->A, 25->Z, 26->AA)."""
    n = col_idx + 1
    out = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out.append(chr(ord("A") + rem))
    return "".join(reversed(out))


def _to_excel_serial(d: date) -> int:
    return (d - EXCEL_EPOCH).days


def write_minimal_xlsx(path: str | Path, rows: list[list[Any]]) -> Path:
    """Ghi một file `.xlsx` hợp lệ chuẩn OOXML bằng thư viện chuẩn Python (`zipfile`, không cần openpyxl)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    row_xml_parts: list[str] = []
    for r_idx, row in enumerate(rows, start=1):
        cell_parts: list[str] = []
        for c_idx, val in enumerate(row):
            if val is None or val == "":
                continue
            ref = f"{_col_letter(c_idx)}{r_idx}"
            if isinstance(val, bool):
                cell_parts.append(f'<c r="{ref}" t="n"><v>{1 if val else 0}</v></c>')
            elif isinstance(val, (int, float)):
                cell_parts.append(f'<c r="{ref}"><v>{val}</v></c>')
            elif isinstance(val, date):
                cell_parts.append(f'<c r="{ref}"><v>{_to_excel_serial(val)}</v></c>')
            else:
                txt = escape(str(val))
                cell_parts.append(f'<c r="{ref}" t="inlineStr"><is><t>{txt}</t></is></c>')
        if cell_parts:
            row_xml_parts.append(f'<row r="{r_idx}">{"".join(cell_parts)}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_xml_parts)}</sheetData>'
        "</worksheet>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    wb_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    root_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )

    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", root_rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return p


def generate_demo_misa_zip(
    out_zip_path: str | Path,
    start_date: date = date(2025, 1, 6),
    n_weeks: int = 48,
    seed: int = 42,
    include_inventory_summary_only: bool = False,
    partial_stock_coverage: bool = False,
    extra_ar_payment_on_last_week: bool = False,
) -> Path:
    """Sinh file `.zip` giả lập chuẩn 5 sổ MISA (11 tháng mặc định), đủ hàng `_KM`, dòng tặng 0đ, đứt hàng, 4 nhóm công nợ."""
    rng = random.Random(seed)
    out_zip = Path(out_zip_path)
    out_zip.parent.mkdir(parents=True, exist_ok=True)

    # 1. Danh mục 24 mặt hàng giả lập đa dạng (bán đều, bán thưa, _KM, tồn chết)
    sku_defs = [
        # (sku, name, cat, wh, uom, box_uom, upb, cost, price, base_wk_demand, pattern)
        ("SP001", "Nước ngọt Cola 330ml", "Đồ uống", "KHO_CHINH", "Lon", "Thùng", 24, 6500, 9000, 140, "smooth"),
        ("SP002", "Trà xanh không độ 500ml", "Đồ uống", "KHO_CHINH", "Chai", "Thùng", 24, 7000, 9800, 120, "smooth"),
        ("SP003", "Nước tăng lực lon vàng", "Đồ uống", "KHO_CHINH", "Lon", "Thùng", 24, 8200, 11500, 110, "smooth_stockout"),
        ("SP004", "Sữa tươi ít đường 180ml", "Sữa", "KHO_CHINH", "Hộp", "Thùng", 48, 5200, 7200, 180, "smooth"),
        ("SP005", "Sữa đặc có đường 380g", "Sữa", "KHO_CHINH", "Lon", "Thùng", 48, 17000, 22000, 95, "smooth"),
        ("SP006", "Mì tôm chua cay gói", "Thực phẩm khô", "KHO_CHINH", "Gói", "Thùng", 30, 3100, 4200, 260, "smooth"),
        ("SP007", "Dầu ăn thực vật 1L", "Gia vị", "KHO_CHINH", "Chai", "Thùng", 12, 34000, 43000, 65, "smooth"),
        ("SP008", "Nước mắm nhĩ 500ml", "Gia vị", "KHO_CHINH", "Chai", "Thùng", 12, 24000, 32000, 58, "smooth"),
        ("SP009", "Hạt nêm thịt thăn 400g", "Gia vị", "KHO_CHINH", "Gói", "Thùng", 24, 21000, 28000, 50, "smooth"),
        ("SP010", "Bánh quy bơ hộp thiếc", "Bánh kẹo", "KHO_PHU", "Hộp", "Thùng", 12, 55000, 75000, 24, "intermittent"),
        ("SP011", "Kẹo trái cây gói 200g", "Bánh kẹo", "KHO_PHU", "Gói", "Thùng", 20, 14000, 19500, 30, "intermittent"),
        ("SP012", "Cà phê hòa tan 3in1 hộp", "Đồ uống", "KHO_PHU", "Hộp", "Thùng", 24, 38000, 50000, 42, "smooth"),
        ("SP013", "Nước giặt đậm đặc 3.6kg", "Hóa mỹ phẩm", "KHO_PHU", "Can", "Thùng", 4, 115000, 148000, 18, "intermittent"),
        ("SP014", "Nước rửa chén chanh 750g", "Hóa mỹ phẩm", "KHO_PHU", "Chai", "Thùng", 12, 22000, 29000, 45, "smooth"),
        ("SP015", "Giấy ăn rút gói 280 tờ", "Hóa mỹ phẩm", "KHO_PHU", "Gói", "Thùng", 30, 11000, 15500, 60, "intermittent"),
        ("SP016", "Snack khoai tây vị muối", "Bánh kẹo", "KHO_CHINH", "Gói", "Thùng", 24, 7500, 10500, 85, "smooth_stockout"),
        ("SP017", "Tương ớt chai 500g", "Gia vị", "KHO_CHINH", "Chai", "Thùng", 24, 12500, 17000, 40, "intermittent"),
        ("SP018", "Bột ngọt gói 454g", "Gia vị", "KHO_CHINH", "Gói", "Thùng", 20, 27000, 35000, 36, "smooth"),
        ("SP019", "Nước khoáng thiên nhiên 500ml", "Đồ uống", "KHO_CHINH", "Chai", "Thùng", 24, 3200, 4800, 150, "smooth"),
        ("SP020", "Bánh bông lan cuộn hộp", "Bánh kẹo", "KHO_PHU", "Hộp", "Thùng", 12, 32000, 44000, 16, "intermittent"),
        ("SP021", "Trà đào hộp giấy 1L", "Đồ uống", "KHO_PHU", "Hộp", "Thùng", 12, 19000, 26000, 8, "dead_stock"),
        ("SP022", "Bột chiên giòn gói 150g", "Gia vị", "KHO_PHU", "Gói", "Thùng", 24, 6500, 9500, 10, "dead_stock"),
        ("SP001_KM", "Nước ngọt Cola 330ml (Tặng KM)", "Đồ uống", "KHO_CHINH", "Lon", "Thùng", 24, 0, 9000, 12, "intermittent"),
        ("SP006_KM", "Mì tôm chua cay (Hàng KM)", "Thực phẩm khô", "KHO_CHINH", "Gói", "Thùng", 30, 0, 4200, 15, "intermittent"),
    ]

    # 2. Khách hàng thuộc đủ 4 nhóm công nợ (A: trả đều, B: hay trả chậm nhưng có trả, B-New: khách mới, C: nợ lâu cần đòi)
    customers = [
        # (code, name, group, pay_delay_days)
        ("KH01", "Tạp hóa Cô Lan", "A", 1),
        ("KH02", "Siêu thị mini Minh Anh", "A", 2),
        ("KH03", "Đại lý Hùng Phát", "A", 3),
        ("KH04", "Cửa hàng Tiện lợi Hoa Mai", "B", 18),
        ("KH05", "Tạp hóa Chú Tư", "B", 22),
        ("KH06", "Nhà phân phối Nam Việt", "C", 999),   # Nợ đọng lâu chưa trả
        ("KH07", "Tạp hóa Ngọc Hân", "C", 999),         # Nợ đọng nhiều đơn
        ("KH08", "Cửa hàng Mới Mở An Khang", "B_NEW", 5),  # Chỉ có 2 đơn mới gần đây
    ]

    suppliers = [
        ("NCC01", "Công ty CP Nước giải khát Miền Nam"),
        ("NCC02", "Công ty TNHH Thực phẩm & Gia vị Việt"),
        ("NCC03", "Nhà máy Hóa mỹ phẩm Sạch"),
    ]

    # --- File 1: Danh_sach_hang_hoa_dich_vu.xlsx ---
    prod_rows: list[list[Any]] = [
        ["DANH SÁCH HÀNG HÓA DỊCH VỤ"],
        [""],
        [
            "Mã", "Tên", "Tính chất", "Nhóm VTHH", "Đơn vị tính chính", "Mã kho ngầm định",
            "Đơn giá mua gần nhất", "Đơn giá bán 1", "Số lượng tồn tối thiểu", "Trạng thái",
            "Đơn vị chuyển đổi", "", "",
        ],
        [
            "", "", "", "", "", "", "", "", "", "",
            "Đơn vị chuyển đổi", "Tỷ lệ chuyển đổi", "Phép tính",
        ],
    ]
    for s in sku_defs:
        prod_rows.append([
            s[0], s[1], "Vật tư hàng hóa", s[2], s[4], s[3],
            s[7], s[8], s[6] * 2, "Đang theo dõi",
            s[5], s[6], "Phép nhân",
        ])

    # --- Sinh dữ liệu giao dịch theo tuần ---
    sales_rows: list[list[Any]] = [
        ["SỔ CHI TIẾT BÁN HÀNG"],
        [""],
        [""],
        [
            "Ngày hạch toán", "Ngày chứng từ", "Số chứng từ", "Ngày hóa đơn", "Số hóa đơn",
            "Diễn giải chung", "Tên hàng trên chứng từ ", "Mã khách hàng", "Tên khách hàng", "Địa chỉ",
            "Mã hàng", "Tên hàng", "ĐVT", "Tổng số lượng bán", "Đơn giá", "Đơn giá cố định",
            "Đơn vị chính (ĐVC)", "Tỷ lệ chuyển đổi", "Doanh số bán", "Chiết khấu",
            "Tổng số lượng trả lại", "Giá trị trả lại", "Giá trị giảm giá", "Giá vốn",
            "Tên nhân viên bán hàng", "Mã kho",
        ],
    ]

    purch_rows: list[list[Any]] = [
        ["SỔ CHI TIẾT MUA HÀNG"],
        [""],
        [""],
        [
            "Ngày hạch toán", "Ngày chứng từ", "Số chứng từ", "Ngày hóa đơn", "Số hóa đơn",
            "Mã nhà cung cấp", "Tên nhà cung cấp", "Mã hàng", "Tên hàng", "ĐVT",
            "Số lượng mua", "Đơn giá", "Giá trị mua", "Chiết khấu",
            "Số lượng trả lại", "Giá trị trả lại", "Giá trị giảm giá",
        ],
    ]

    inv_led_rows: list[list[Any]] = [
        ["SỔ CHI TIẾT VẬT TƯ HÀNG HÓA"],
        [""],
        [""],
        [
            "Tên kho", "Mã hàng", "Tên hàng", "Mô tả", "Ngày hạch toán", "Ngày chứng từ",
            "Số chứng từ", "Diễn giải", "ĐVT", "Đơn giá",
            "Nhập", "", "Xuất", "", "Tồn", "",
        ],
        [
            "", "", "", "", "", "", "", "", "", "",
            "Số lượng", "Giá trị", "Số lượng", "Giá trị", "Số lượng", "Giá trị",
        ],
    ]

    # Theo dõi tồn kho & công nợ theo khách
    stock_bal: dict[str, float] = {s[0]: float(s[9] * 3.0) for s in sku_defs}
    stock_open: dict[str, float] = dict(stock_bal)
    stock_in_tot: dict[str, float] = {s[0]: 0.0 for s in sku_defs}
    stock_out_tot: dict[str, float] = {s[0]: 0.0 for s in sku_defs}

    # Để test partial_stock_coverage: chỉ ghi 50% SKU vào Sổ kho nếu bật cờ
    covered_set = {s[0] for idx, s in enumerate(sku_defs) if (not partial_stock_coverage) or (idx % 2 == 0)}

    # Công nợ 131 theo từng khách hàng
    ar_by_cust: dict[str, list[dict[str, Any]]] = {c[1]: [] for c in customers}

    tag = start_date.strftime("%y%m")
    bh_seq = 1
    nk_seq = 1
    pt_seq = 1
    end_date = start_date + timedelta(days=n_weeks * 7 - 1)

    for wk in range(n_weeks):
        ws = start_date + timedelta(days=wk * 7)
        eom_boost = 1.18 if ws.day >= 22 else 1.0

        # Nhập hàng đầu mỗi 2 tuần
        if wk % 2 == 0:
            inb_d = ws
            for sup_idx, (sup_code, sup_name) in enumerate(suppliers):
                nk_no = f"NK{tag}{nk_seq:04d}"
                nk_seq += 1
                for s_idx, s in enumerate(sku_defs):
                    if s_idx % len(suppliers) != sup_idx:
                        continue
                    sku, name, cat, wh, uom, _, upb, cost, price, base_d, pat = s
                    dead_cutoff_wk = max(4, n_weeks - 14)
                    if pat == "dead_stock" and wk > dead_cutoff_wk:
                        continue
                    # Bỏ qua 1 đợt nhập để tạo sự kiện đứt hàng thật sự trên smooth_stockout
                    if pat == "smooth_stockout" and wk in (14, 30):
                        continue
                    buy_qty = max(upb * 4, int(round(base_d * 6.0))) if (pat == "dead_stock" and wk == dead_cutoff_wk - (dead_cutoff_wk % 2)) else max(upb, int(round(base_d * 2.1)))
                    gross_buy = buy_qty * max(cost, 1000)
                    disc_buy = round(gross_buy * 0.03, 0) if buy_qty >= 100 else 0.0
                    purch_rows.append([
                        inb_d, inb_d, nk_no, inb_d, f"HDM{nk_seq:04d}",
                        sup_code, sup_name, sku, name, uom,
                        buy_qty, max(cost, 1000), gross_buy, disc_buy, 0, 0, 0,
                    ])
                    stock_bal[sku] += buy_qty
                    stock_in_tot[sku] += buy_qty
                    if sku in covered_set:
                        inv_led_rows.append([
                            wh, sku, name, "", inb_d, inb_d, nk_no, f"Nhập mua {sup_name}", uom,
                            max(cost, 1000), buy_qty, gross_buy, 0, 0, stock_bal[sku], stock_bal[sku] * max(cost, 1000),
                        ])

        # Bán hàng 2 ngày trong tuần (Thứ 3 và Thứ 6)
        for day_offset in (1, 4):
            sale_d = ws + timedelta(days=day_offset)
            if sale_d > end_date:
                continue

            # Chọn khách hàng mua trong ngày
            day_custs = [c for c in customers if c[2] != "B_NEW"]
            if wk >= n_weeks - 3 and day_offset == 1:
                day_custs.append(customers[-1])  # KH08 mới mở chỉ mua 2-3 đơn cuối

            chosen_cust = day_custs[(wk * 2 + day_offset) % len(day_custs)]
            c_code, c_name, c_grp, pay_delay = chosen_cust
            bh_no = f"BH{tag}{bh_seq:04d}"
            bh_seq += 1
            order_total_ar = 0.0

            for s in sku_defs:
                sku, name, cat, wh, uom, _, upb, cost, price, base_d, pat = s
                if pat == "dead_stock" and wk >= max(4, n_weeks - 14):
                    continue  # Nằm im >= 60 ngày cuối
                if pat == "intermittent" and rng.random() > 0.35:
                    continue

                raw_q = max(1, int(round((base_d / 2.0) * eom_boost * rng.uniform(0.75, 1.25))))
                if pat == "smooth_stockout" and wk in (15, 31):
                    # Đẩy tồn về 0 -> sự kiện đứt hàng thực tế
                    raw_q = max(1, int(stock_bal[sku])) if stock_bal[sku] > 0 else 0
                    if raw_q == 0:
                        continue

                gross_rev = raw_q * price
                disc = round(gross_rev * 0.02, 0) if raw_q >= 50 else 0.0
                cogs_val = raw_q * (cost if cost > 0 else round(price * 0.72, 0))
                sales_rows.append([
                    sale_d, sale_d, bh_no, sale_d, f"HD{bh_seq:05d}",
                    f"Bán hàng {c_name}", name, c_code, c_name, "TP.HCM",
                    sku, name, uom, raw_q, price, price, uom, 1,
                    gross_rev, disc, 0, 0, 0, cogs_val, "NV01", wh,
                ])
                order_total_ar += (gross_rev - disc)
                stock_bal[sku] = max(0.0, stock_bal[sku] - raw_q)
                stock_out_tot[sku] += raw_q
                if sku in covered_set:
                    inv_led_rows.append([
                        wh, sku, name, "", sale_d, sale_d, f"XK{bh_seq:05d}", f"Xuất bán {c_name}", uom,
                        cost, 0, 0, raw_q, cogs_val, stock_bal[sku], stock_bal[sku] * max(cost, 1000),
                    ])

                # Dòng hàng tặng kèm 0đ ("Mua X tặng Y") trên SP001 & SP004: Doanh số = 0 nhưng Giá vốn > 0!
                if sku in ("SP001", "SP004") and raw_q >= 40:
                    gift_qty = max(2, raw_q // 15)
                    gift_cogs = gift_qty * cost
                    sales_rows.append([
                        sale_d, sale_d, bh_no, sale_d, f"HD{bh_seq:05d}",
                        f"Hàng tặng kèm {c_name}", f"{name} (Tặng)", c_code, c_name, "TP.HCM",
                        sku, name, uom, gift_qty, 0, 0, uom, 1,
                        0, 0, 0, 0, 0, gift_cogs, "NV01", wh,
                    ])

            if order_total_ar > 0:
                ar_by_cust[c_name].append({
                    "date": sale_d,
                    "doc": bh_no,
                    "desc": f"Bán hàng {bh_no}",
                    "contra": "5112",
                    "debit": order_total_ar,
                    "credit": 0.0,
                })
                # Lịch thanh toán theo nhóm khách hàng
                if c_grp in ("A", "B", "B_NEW"):
                    pay_d = sale_d + timedelta(days=pay_delay)
                    if pay_d <= end_date:
                        pt_no = f"PT{tag}{pt_seq:04d}"
                        pt_seq += 1
                        contra = "1121" if c_grp == "A" else "1111"
                        ar_by_cust[c_name].append({
                            "date": pay_d,
                            "doc": pt_no,
                            "desc": f"Thu tiền {c_name}",
                            "contra": contra,
                            "debit": 0.0,
                            "credit": order_total_ar,
                        })
                elif c_grp == "C":
                    # Nhóm C chỉ trả lác đác các đơn rất cũ, để nợ treo nhiều đơn cuối kỳ
                    if wk < n_weeks - 10:
                        pay_d = sale_d + timedelta(days=35)
                        if pay_d <= end_date:
                            pt_no = f"PT{tag}{pt_seq:04d}"
                            pt_seq += 1
                            ar_by_cust[c_name].append({
                                "date": pay_d,
                                "doc": pt_no,
                                "desc": f"Thu nợ cũ {c_name}",
                                "contra": "1111",
                                "debit": 0.0,
                                "credit": order_total_ar,
                            })
                    elif extra_ar_payment_on_last_week and wk == n_weeks - 10:
                        # Mô phỏng khách vừa trả thêm tiền ở kỳ cập nhật mới!
                        pt_no = f"PT{tag}{pt_seq:04d}"
                        pt_seq += 1
                        ar_by_cust[c_name].append({
                            "date": end_date,
                            "doc": pt_no,
                            "desc": f"Thu tiền bổ sung {c_name}",
                            "contra": "1121",
                            "debit": 0.0,
                            "credit": order_total_ar,
                        })

    # --- File 5: Chi_tiet_cong_no_phai_thu_khach_hang.xlsx (đúng chuẩn block + Cộng checksum) ---
    ar_rows: list[list[Any]] = [
        ["CHI TIẾT CÔNG NỢ PHẢI THU KHÁCH HÀNG"],
        [""],
        [""],
        [
            "Ngày hạch toán", "Ngày chứng từ", "Số chứng từ", "Ngày hóa đơn", "Số hóa đơn",
            "Diễn giải", "TK công nợ", "TK đối ứng", "Phát sinh", "", "Số dư", "",
        ],
        ["", "", "", "", "", "", "", "", "Nợ", "Có", "Nợ", "Có"],
    ]
    for c_name, evs in ar_by_cust.items():
        if not evs:
            continue
        evs_sorted = sorted(evs, key=lambda x: (x["date"], 0 if x["debit"] > 0 else 1))
        tot_dr = sum(e["debit"] for e in evs_sorted)
        tot_cr = sum(e["credit"] for e in evs_sorted)
        # Dòng đầu block: Tên khách hàng: ... (chứa tổng block ở cột 8-11)
        ar_rows.append([f"Tên khách hàng: {c_name}", "", "", "", "", "", "", "", tot_dr, tot_cr, max(0.0, tot_dr - tot_cr), 0.0])
        running = 0.0
        for e in evs_sorted:
            running += e["debit"] - e["credit"]
            ar_rows.append([
                e["date"], e["date"], e["doc"], e["date"], e["doc"],
                e["desc"], "131", e["contra"], e["debit"], e["credit"],
                max(0.0, running), max(0.0, -running),
            ])
        # Dòng cuối block: Cộng (checksum audit)
        ar_rows.append(["", "", "", "", "", "Cộng", "", "", tot_dr, tot_cr, max(0.0, running), max(0.0, -running)])

    # --- File 2b dự phòng: Tong_hop_ton_kho.xlsx ---
    inv_sum_rows: list[list[Any]] = [
        ["TỔNG HỢP TỒN KHO"],
        [f"Từ ngày {start_date.strftime('%d/%m/%Y')} đến ngày {end_date.strftime('%d/%m/%Y')}"],
        [""],
        ["Tên kho", "Mã hàng", "Tên hàng", "ĐVT", "Đầu kỳ", "", "Nhập kho", "", "Xuất kho", "", "Cuối kỳ", "", "Đơn giá bán 1"],
        ["", "", "", "", "Số lượng", "Giá trị", "Số lượng", "Giá trị", "Số lượng", "Giá trị", "Số lượng", "Giá trị", ""],
    ]
    for s in sku_defs:
        sku, name, _, wh, uom, _, _, cost, price, _, _ = s
        if sku not in covered_set:
            continue
        unit_c = max(cost, 1000)
        inv_sum_rows.append([
            wh, sku, name, uom,
            stock_open[sku], stock_open[sku] * unit_c,
            stock_in_tot[sku], stock_in_tot[sku] * unit_c,
            stock_out_tot[sku], stock_out_tot[sku] * unit_c,
            stock_bal[sku], stock_bal[sku] * unit_c,
            price,
        ])

    with tempfile.TemporaryDirectory(prefix="demo_misa_") as tmp:
        tdir = Path(tmp)
        files_to_zip: list[Path] = [
            write_minimal_xlsx(tdir / "Danh_sach_hang_hoa_dich_vu.xlsx", prod_rows),
            write_minimal_xlsx(tdir / "So_chi_tiet_mua_hang.xlsx", purch_rows),
            write_minimal_xlsx(tdir / "So_chi_tiet_ban_hang.xlsx", sales_rows),
            write_minimal_xlsx(tdir / "Chi_tiet_cong_no_phai_thu_khach_hang.xlsx", ar_rows),
        ]
        if include_inventory_summary_only:
            files_to_zip.append(write_minimal_xlsx(tdir / "Tong_hop_ton_kho.xlsx", inv_sum_rows))
        else:
            files_to_zip.append(write_minimal_xlsx(tdir / "So_chi_tiet_vat_tu_hang_hoa.xlsx", inv_led_rows))

        with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in files_to_zip:
                zf.write(f, arcname=f"MISA_Export/{f.name}")

    return out_zip

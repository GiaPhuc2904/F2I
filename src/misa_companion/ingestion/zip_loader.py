"""Giải nén file `.zip` MISA an toàn (chống ZipSlip/ZipBomb) và nhận diện tự động 5 file chuẩn + Tổng hợp tồn kho."""

from __future__ import annotations

from pathlib import Path
import re
import unicodedata
import zipfile

from misa_companion.config import MAX_EXTRACTED_BYTES, MAX_ZIP_FILE_BYTES, MAX_ZIP_MEMBERS
from misa_companion.ingestion.misa_parser import iter_xlsx_rows, norm_header


def _strip_accents(text: str) -> str:
    s = unicodedata.normalize("NFKD", text)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("đ", "d").replace("Đ", "D")
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def extract_zip_safely(zip_path: str | Path, dest_dir: str | Path) -> list[Path]:
    """Giải nén an toàn các file `.xlsx` từ `.zip` vào `dest_dir`, chặn ZipSlip & ZipBomb."""
    zp = Path(zip_path)
    if not zp.exists() or not zp.is_file():
        raise FileNotFoundError(f"Không tìm thấy file zip: {zp}")
    if zp.stat().st_size > MAX_ZIP_FILE_BYTES:
        raise ValueError("File .zip vượt quá giới hạn dung lượng cho phép (250 MB).")
    if not zipfile.is_zipfile(zp):
        raise ValueError("Tệp tải lên không phải là file .zip hợp lệ.")

    dest = Path(dest_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    extracted: list[Path] = []
    total_bytes = 0

    with zipfile.ZipFile(zp, "r") as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise ValueError(f"File .zip chứa quá nhiều tệp ({len(infos)} > {MAX_ZIP_MEMBERS}).")

        for idx, info in enumerate(infos):
            if info.is_dir():
                continue

            raw_name = info.filename.replace("\\", "/")
            parts = [p for p in raw_name.split("/") if p]
            if not parts:
                continue
            if any(p == ".." for p in parts) or raw_name.startswith("/"):
                raise ValueError(f"Phát hiện đường dẫn không an toàn trong file .zip: {info.filename}")

            # Bỏ qua thư mục/file tạm của macOS, LibreOffice, Excel
            if any(p.startswith("__MACOSX") or p.startswith(".") for p in parts):
                continue
            fname = parts[-1]
            if fname.startswith(".~lock") or fname.startswith("~$") or not fname.lower().endswith(".xlsx"):
                continue

            total_bytes += info.file_size
            if total_bytes > MAX_EXTRACTED_BYTES:
                raise ValueError("Tổng dung lượng sau giải nén vượt quá giới hạn an toàn.")

            # Tránh ghi đè nếu 2 thư mục con trùng tên file
            safe_name = fname
            out_path = (dest / safe_name).resolve()
            if not str(out_path).startswith(str(dest)):
                raise ValueError(f"Đường dẫn giải nén vượt ra ngoài thư mục tạm: {fname}")
            if out_path.exists():
                out_path = dest / f"{idx}_{safe_name}"

            with zf.open(info, "r") as src, open(out_path, "wb") as dst:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    dst.write(chunk)
            extracted.append(out_path)

    return sorted(extracted)


def _sniff_xlsx_role_by_header(path: Path) -> str | None:
    """Kiểm tra 12 dòng đầu của file `.xlsx` để nhận diện chính xác loại sổ MISA kể cả khi bị đổi tên."""
    try:
        rows_checked = 0
        combined_headers: set[str] = set()
        for _, cells in iter_xlsx_rows(path):
            rows_checked += 1
            vals = {norm_header(v) for v in cells.values()}
            combined_headers.update(vals)
            if "ngày hạch toán" in vals and ("phát sinh" in vals or "tk đối ứng" in vals or "tk công nợ" in vals):
                return "ar_ledger"
            if "mã hàng" in vals and ("doanh số bán" in vals or "tổng số lượng bán" in vals):
                return "sales"
            if "mã hàng" in vals and ("số lượng mua" in vals or "giá trị mua" in vals):
                return "purchases"
            if "mã" in vals and "tên" in vals and ("đơn vị tính chính" in vals or "tính chất" in vals):
                return "products"
            if "mã hàng" in vals and ("đầu kỳ" in vals and "cuối kỳ" in vals):
                return "inventory_summary"
            if "mã hàng" in vals and ("nhập" in vals and "xuất" in vals and "tồn" in vals):
                return "inventory_ledger"
            if rows_checked >= 12:
                break
        if "đầu kỳ" in combined_headers and "cuối kỳ" in combined_headers and "mã hàng" in combined_headers:
            return "inventory_summary"
    except Exception:
        return None
    return None


def classify_misa_files(xlsx_paths: list[Path]) -> dict[str, Path | None]:
    """Nhận diện 5 file MISA chuẩn + file dự phòng `Tong_hop_ton_kho` bằng tên file và xác minh header."""
    roles: dict[str, Path | None] = {
        "products": None,
        "inventory_ledger": None,
        "inventory_summary": None,
        "purchases": None,
        "sales": None,
        "ar_ledger": None,
    }
    remaining: list[Path] = []

    for p in sorted(xlsx_paths):
        if p.name.startswith(".~lock") or p.name.startswith("~$"):
            continue
        norm = _strip_accents(p.stem)
        matched_role: str | None = None
        if ("hang_hoa" in norm and "dich_vu" in norm) or "danh_sach" in norm or "danh_muc" in norm or "vthh" in norm:
            matched_role = "products"
        elif "tong_hop" in norm and "ton_kho" in norm:
            matched_role = "inventory_summary"
        elif ("vat_tu" in norm and "hang_hoa" in norm) or "chi_tiet_ton" in norm:
            matched_role = "inventory_ledger"
        elif "mua_hang" in norm:
            matched_role = "purchases"
        elif "ban_hang" in norm:
            matched_role = "sales"
        elif "cong_no" in norm or "phai_thu" in norm or "131" in norm:
            matched_role = "ar_ledger"

        if matched_role and roles[matched_role] is None:
            # Xác minh nhẹ tránh nhầm lẫn (vd: file tên chứa "ton_kho" nhưng thực chất là tổng hợp)
            sniffed = _sniff_xlsx_role_by_header(p)
            if sniffed and roles[sniffed] is None:
                roles[sniffed] = p
            else:
                roles[matched_role] = p
        else:
            remaining.append(p)

    # Với file tên lạ (người dùng đổi tên), dò bằng header
    for p in remaining:
        sniffed = _sniff_xlsx_role_by_header(p)
        if sniffed and roles[sniffed] is None:
            roles[sniffed] = p

    return roles

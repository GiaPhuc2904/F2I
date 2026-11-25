"""Cấu hình trung tâm, đường dẫn SQLite cục bộ và lịch nghỉ lễ Việt Nam (100% offline)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "app.db"

# Ngưỡng an toàn giải nén .zip (chống ZipBomb / ZipSlip)
MAX_ZIP_FILE_BYTES = 250 * 1024 * 1024       # 250 MB file nén
MAX_EXTRACTED_BYTES = 800 * 1024 * 1024      # 800 MB tổng sau giải nén
MAX_ZIP_MEMBERS = 200

# Mùng 1 Tết Nguyên Đán dương lịch (2020..2030) để tự nhận diện tuần cận Tết & nghỉ Tết
TET_DAY1_BY_YEAR: dict[int, date] = {
    2020: date(2020, 1, 25),
    2021: date(2021, 2, 12),
    2022: date(2022, 2, 1),
    2023: date(2023, 1, 22),
    2024: date(2024, 2, 10),
    2025: date(2025, 1, 29),
    2026: date(2026, 2, 17),
    2027: date(2027, 2, 6),
    2028: date(2028, 1, 26),
    2029: date(2029, 2, 13),
    2030: date(2030, 2, 3),
}

FIXED_HOLIDAYS_MD: set[tuple[int, int]] = {
    (1, 1),   # Tết Dương lịch
    (4, 30),  # Giải phóng miền Nam
    (5, 1),   # Quốc tế Lao động
    (9, 2),   # Quốc khánh
}


def _tet_closed_dates(year: int) -> set[date]:
    """Các ngày nghỉ Tết chính thức của kho FMCG (29/30 Tết đến hết mùng 5)."""
    d1 = TET_DAY1_BY_YEAR.get(year)
    if d1 is None:
        return set()
    return {d1 + timedelta(days=offset) for offset in range(-1, 6)}


def is_vn_holiday(d: date) -> bool:
    """Kiểm tra ngày `d` có phải ngày nghỉ lễ/Tết lớn ở Việt Nam hay không."""
    if (d.month, d.day) in FIXED_HOLIDAYS_MD:
        return True
    return d in _tet_closed_dates(d.year) or d in _tet_closed_dates(d.year + 1)


def open_days_in_week(week_start: str | date) -> int:
    """Số ngày kho mở cửa trong tuần bắt đầu từ `week_start` (mặc định kho nghỉ Chủ Nhật + ngày lễ)."""
    ws = date.fromisoformat(week_start) if isinstance(week_start, str) else week_start
    open_cnt = 0
    for i in range(7):
        d = ws + timedelta(days=i)
        if d.weekday() == 6:  # Chủ nhật
            continue
        if is_vn_holiday(d):
            continue
        open_cnt += 1
    return max(0, open_cnt)


def is_pre_tet_week(week_start: str | date) -> int:
    """Trả về 1 nếu tuần này rơi vào 3 tuần cao điểm trước Tết Nguyên Đán."""
    ws = date.fromisoformat(week_start) if isinstance(week_start, str) else week_start
    mid = ws + timedelta(days=3)
    for yr in (mid.year, mid.year + 1):
        d1 = TET_DAY1_BY_YEAR.get(yr)
        if d1 is not None:
            delta = (d1 - mid).days
            if 1 <= delta <= 24:
                return 1
    return 0

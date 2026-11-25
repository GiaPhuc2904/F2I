"""Tải sẵn toàn bộ wheel Windows (win_amd64) vào `offline_wheels/` để mang USB cài 100% offline."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Tải sẵn wheel cho cài đặt offline Windows.")
    parser.add_argument("--python-version", default="3.12", help="Phiên bản Python trên máy Windows đích (vd: 3.11, 3.12)")
    parser.add_argument("--out-dir", default="offline_wheels", help="Thư mục lưu file .whl")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    req_file = root / "requirements.txt"
    out_dir = root / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "-r",
        str(req_file),
        "-d",
        str(out_dir),
        "--platform",
        "win_amd64",
        "--python-version",
        args.python_version,
        "--only-binary=:all:",
    ]
    print("Đang tải wheel Windows:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"Hoàn tất! Đã lưu {len(list(out_dir.glob('*.whl')))} file .whl tại {out_dir}")


if __name__ == "__main__":
    main()

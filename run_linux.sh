#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[LOI] Khong tim thay python3. Vui long cai Python 3.11+ truoc khi chay."
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "[1/2] Dang tao moi truong ao .venv ..."
    python3 -m venv .venv
fi

if ! .venv/bin/python -c "import uvicorn, fastapi" >/dev/null 2>&1; then
    echo "[2/2] Dang cai dat thu vien ..."
    if compgen -G "offline_wheels/*.whl" >/dev/null; then
        echo "-> Phat hien offline_wheels/, cai dat 100% OFFLINE!"
        .venv/bin/python -m pip install --no-index --find-links=offline_wheels -r requirements.txt
    else
        .venv/bin/python -m pip install --upgrade pip
        .venv/bin/python -m pip install "fastapi>=0.115.0" "uvicorn>=0.32.0" "python-multipart>=0.0.12" "pandas>=2.2.0" "numpy>=1.26.0" "scikit-learn>=1.5.0" "httpx>=0.27.0" "pytest>=8.3.0"
        .venv/bin/python -m pip install "lightgbm>=4.5.0" "catboost>=1.2.7" || \
            echo "[LUU Y] Bo qua goi hoc may mo rong do phien ban Python chua co wheel san; app van chay binh thuong."
    fi
fi

export PYTHONPATH="$PWD/src:$PWD"
export PYTHONUTF8=1

# Neu app dang chay san o cong 8050 thi mo trinh duyet ngay, khong bao loi trung cong
if .venv/bin/python -c "import socket, sys; s=socket.socket(); ok=(s.connect_ex(('127.0.0.1', 8050))==0); s.close(); sys.exit(0 if ok else 1)" >/dev/null 2>&1; then
    echo "[THONG BAO] Ung dung dang chay san tai http://127.0.0.1:8050"
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "http://127.0.0.1:8050" >/dev/null 2>&1 &
    fi
    exit 0
fi

echo "========================================================"
echo "  DANG MO CONG SU OFFLINE MISA KE TOAN (127.0.0.1:8050)"
echo "  Trinh duyet se tu mo ngay khi may chu san sang..."
echo "========================================================"

# Cho cong 8050 thuc su san sang roi moi mo trinh duyet
(
    .venv/bin/python -c "import socket, time, webbrowser; ok = lambda: (lambda s: (s.connect_ex(('127.0.0.1', 8050)) == 0, s.close())[0])(socket.socket()); any(ok() or time.sleep(0.4) for _ in range(120)) and webbrowser.open('http://127.0.0.1:8050')" >/dev/null 2>&1
) &

exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8050 "$@"

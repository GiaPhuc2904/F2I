@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ========================================================
echo   CAI DAT LAN DAU - CONG SU OFFLINE MISA KE TOAN
echo ========================================================
echo.

set "PY_CMD="
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py -3"
) else (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "PY_CMD=python"
    )
)

if "%PY_CMD%"=="" (
    echo [LOI] Khong tim thay Python hop le tren may.
    echo Vui long tai Python 3.11 hoac 3.12 tai https://www.python.org/downloads/
    echo va nho tick o "Add python.exe to PATH" khi cai.
    pause
    exit /b 1
)

echo -> Dang dung Python:
%PY_CMD% --version
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Dang tao moi truong ao .venv ...
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        echo [LOI] Tao moi truong ao .venv that bai.
        pause
        exit /b 1
    )
)

echo [2/3] Dang cai dat thu vien cot loi ...
if exist "offline_wheels\*.whl" (
    echo -> Phat hien thu muc offline_wheels\, cai dat 100%% OFFLINE khong can mang!
    ".venv\Scripts\python.exe" -m pip install --no-index --find-links=offline_wheels -r requirements.txt
    if errorlevel 1 (
        echo [LOI] Cai dat tu offline_wheels\ that bai.
        pause
        exit /b 1
    )
) else (
    echo -> Dang cai dat tu Internet ^(chi can mang duy nhat lan dau^)...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install "fastapi>=0.115.0" "uvicorn>=0.32.0" "python-multipart>=0.0.12" "pandas>=2.2.0" "numpy>=1.26.0" "scikit-learn>=1.5.0" "httpx>=0.27.0" "pytest>=8.3.0"
    if errorlevel 1 (
        echo [LOI] Cai dat thu vien cot loi that bai. Vui long kiem tra ket noi mang.
        pause
        exit /b 1
    )
    echo -> Dang cai dat goi hoc may mo rong ^(lightgbm, catboost^)...
    ".venv\Scripts\python.exe" -m pip install "lightgbm>=4.5.0" "catboost>=1.2.7"
    if errorlevel 1 (
        echo [LUU Y] Bo qua goi hoc may mo rong do phien ban Python chua co ban build san; app van chay binh thuong.
    )
)

echo.
echo [3/3] Dang kiem tra khoi dong thu ...
set "PYTHONPATH=%~dp0src;%~dp0"
set "PYTHONUTF8=1"
".venv\Scripts\python.exe" -c "import uvicorn, fastapi, app.main; print('Kiem tra OK!')"
if errorlevel 1 (
    echo [LOI] Khong nap duoc ung dung sau khi cai. Vui long chup man hinh loi nay gui lai.
    pause
    exit /b 1
)

echo.
echo [THANH CONG] Da cai dat xong!
echo Tu nay ban co the tat Wi-Fi va bam dup 2_Mo_App_Offline.bat de mo app.
pause

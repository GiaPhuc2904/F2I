@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ========================================================
echo   CAI DAT LAN DAU - CONG SU OFFLINE MISA KE TOAN
echo ========================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [LOI] Khong tim thay Python tren may. Vui long cai Python 3.11+ va tick "Add Python to PATH".
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] Dang tao moi truong ao .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [LOI] Tao .venv that bai.
        pause
        exit /b 1
    )
)

echo [2/2] Dang cai dat thu vien ...
if exist "offline_wheels\*.whl" (
    echo -> Phat hien thu muc offline_wheels\, cai dat 100%% OFFLINE khong can mang!
    .venv\Scripts\python.exe -m pip install --no-index --find-links=offline_wheels -r requirements.txt
) else (
    echo -> Dang cai dat tu Internet (chi can mang duy nhat lan dau)...
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)

if errorlevel 1 (
    echo [LOI] Cai dat thu vien that bai. Kiem tra ket noi mang hoac thu muc offline_wheels\.
    pause
    exit /b 1
)

echo.
echo [THANH CONG] Da cai dat xong! Tu nay ban co the tat Wi-Fi va bam 2_Mo_App_Offline.bat de dung.
pause

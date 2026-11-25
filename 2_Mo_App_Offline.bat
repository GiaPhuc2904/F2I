@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [THONG BAO] Chua cai dat lan dau. Vui long chay 1_Cai_Dat_Lan_Dau.bat truoc!
    pause
    exit /b 1
)

echo ========================================================
echo   DANG MO CONG SU OFFLINE MISA KE TOAN (127.0.0.1:8050)
echo   (Giu cua so den nay mo trong luc xem bao cao)
echo ========================================================
echo.

set PYTHONPATH=src
start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8050"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8050
pause

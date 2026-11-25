@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [THONG BAO] Chua cai dat lan dau. Vui long chay 1_Cai_Dat_Lan_Dau.bat truoc!
    pause
    exit /b 1
)

set "PYTHONPATH=%~dp0src;%~dp0"
set "PYTHONUTF8=1"

".venv\Scripts\python.exe" -c "import uvicorn, fastapi" >nul 2>&1
if errorlevel 1 (
    echo [LOI] Moi truong .venv thieu thu vien. Vui long chay lai 1_Cai_Dat_Lan_Dau.bat!
    pause
    exit /b 1
)

REM Neu app dang chay san o cong 8050 thi mo trinh duyet ngay, khong bao loi trung cong
".venv\Scripts\python.exe" -c "import socket, sys; s=socket.socket(); ok=(s.connect_ex(('127.0.0.1', 8050))==0); s.close(); sys.exit(0 if ok else 1)" >nul 2>&1
if not errorlevel 1 (
    echo [THONG BAO] Ung dung dang chay san tai http://127.0.0.1:8050 - dang mo trinh duyet...
    start "" "http://127.0.0.1:8050"
    exit /b 0
)

echo ========================================================
echo   DANG MO CONG SU OFFLINE MISA KE TOAN (127.0.0.1:8050)
echo   (Giu cua so den nay mo trong luc xem bao cao)
echo   Trinh duyet se tu mo ngay khi may chu khoi dong xong...
echo ========================================================
echo.

REM Cho den khi cong 8050 thuc su san sang roi moi mo trinh duyet (tranh loi "Khong the truy cap trang web")
start "" /b ".venv\Scripts\python.exe" -c "import socket, time, webbrowser; ok = lambda: (lambda s: (s.connect_ex(('127.0.0.1', 8050)) == 0, s.close())[0])(socket.socket()); any(ok() or time.sleep(0.4) for _ in range(120)) and webbrowser.open('http://127.0.0.1:8050')"

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8050
if errorlevel 1 (
    echo.
    echo [LOI] Khong khoi dong duoc may chu. Vui long chup man hinh loi phia tren de kiem tra.
)
pause

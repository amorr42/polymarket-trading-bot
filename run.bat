@echo off
REM ============================================================================
REM Polymarket Trading Bot - Calistirma Scripti
REM ============================================================================

echo ======================================
echo Polymarket Trading Bot
echo ======================================
echo.

REM Sanal ortam kontrol
if not exist venv (
    echo HATA: Sanal ortam bulunamadi!
    echo Once setup.bat scriptini calistirin.
    echo.
    pause
    exit /b 1
)

REM Sanal ortami aktif et
call venv\Scripts\activate.bat

echo Veritabani baglantisi kontrol ediliyor...
python -c "from lib.db import connect; conn = connect(); print('Veritabani baglantisi basarili!'); conn.close()" 2>nul
if %errorlevel% neq 0 (
    echo.
    echo UYARI: Veritabani baglantisi yapilamadi!
    echo .env dosyasindaki DATABASE_URL ayarini kontrol edin.
    echo.
    pause
    exit /b 1
)

echo.
echo Hangi uygulamayi calistirmak istiyorsunuz?
echo.
echo 1. Alert Watcher (Varsayilan)
echo 2. Event Orderbook Viewer
echo 3. Flash Crash Strategy
echo 4. Orderbook Viewer
echo 5. Market Ingest (Verileri guncelle)
echo.

set /p choice="Seciminiz (1-5): "

if "%choice%"=="1" (
    echo.
    echo Alert Watcher baslatiliyor...
    python apps/db_alert_watcher.py
) else if "%choice%"=="2" (
    echo.
    echo Event Orderbook Viewer baslatiliyor...
    python apps/event_orderbook_viewer.py
) else if "%choice%"=="3" (
    echo.
    echo Flash Crash Strategy baslatiliyor...
    python apps/flash_crash_runner.py
) else if "%choice%"=="4" (
    echo.
    echo Orderbook Viewer baslatiliyor...
    python apps/orderbook_viewer.py
) else if "%choice%"=="5" (
    echo.
    echo Market verileri guncelleniyor...
    python apps/ingest_markets_pg.py
) else (
    echo.
    echo Varsayilan olarak Alert Watcher baslatiliyor...
    python apps/db_alert_watcher.py
)

pause

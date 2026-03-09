@echo off
REM ============================================================================
REM Kurulum Test Scripti
REM ============================================================================

echo ======================================
echo Kurulum Test Ediliyor
echo ======================================
echo.

set ERROR_COUNT=0

REM Python kontrolu
echo [1/7] Python kontrolu...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   [X] HATA: Python bulunamadi!
    set /a ERROR_COUNT+=1
) else (
    python --version
    echo   [OK] Python bulundu!
)
echo.

REM PostgreSQL kontrolu
echo [2/7] PostgreSQL kontrolu...
psql --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   [X] HATA: PostgreSQL bulunamadi!
    set /a ERROR_COUNT+=1
) else (
    psql --version
    echo   [OK] PostgreSQL bulundu!
)
echo.

REM Sanal ortam kontrolu
echo [3/7] Sanal ortam kontrolu...
if not exist venv (
    echo   [X] HATA: venv klasoru bulunamadi!
    echo   Cozum: setup.bat calistirin
    set /a ERROR_COUNT+=1
) else (
    echo   [OK] venv klasoru mevcut!
)
echo.

REM .env dosyasi kontrolu
echo [4/7] .env dosyasi kontrolu...
if not exist .env (
    echo   [X] HATA: .env dosyasi bulunamadi!
    echo   Cozum: .env.example dosyasini kopyalayin
    set /a ERROR_COUNT+=1
) else (
    echo   [OK] .env dosyasi mevcut!
)
echo.

REM config.yaml kontrolu
echo [5/7] config.yaml kontrolu...
if not exist config.yaml (
    echo   [X] HATA: config.yaml dosyasi bulunamadi!
    echo   Cozum: config.example.yaml dosyasini kopyalayin
    set /a ERROR_COUNT+=1
) else (
    echo   [OK] config.yaml dosyasi mevcut!
)
echo.

REM Python paketleri kontrolu
echo [6/7] Python paketleri kontrolu...
call venv\Scripts\activate.bat
python -c "import web3, psycopg2, yaml, dotenv" >nul 2>&1
if %errorlevel% neq 0 (
    echo   [X] HATA: Gerekli Python paketleri eksik!
    echo   Cozum: setup.bat calistirin veya pip install -r requirements.txt
    set /a ERROR_COUNT+=1
) else (
    echo   [OK] Gerekli paketler yuklu!
)
echo.

REM Veritabani baglantisi kontrolu
echo [7/7] Veritabani baglantisi kontrolu...
python -c "from lib.db import connect; conn = connect(); conn.close(); print('   [OK] Veritabani baglantisi basarili!')" 2>nul
if %errorlevel% neq 0 (
    echo   [X] HATA: Veritabani baglantisi yapilamadi!
    echo   Cozum: setup_database.bat calistirin veya .env dosyasindaki DATABASE_URL'i kontrol edin
    set /a ERROR_COUNT+=1
)
echo.

echo ======================================
echo Test Sonucu
echo ======================================
if %ERROR_COUNT% equ 0 (
    echo [OK] Tum testler basarili!
    echo Projeyi calistirmaya hazirsiniz!
    echo.
    echo Baslatmak icin: run.bat
) else (
    echo [X] %ERROR_COUNT% test basarisiz!
    echo Detayli kurulum icin SETUP_TR.md dosyasina bakin.
)
echo.
pause

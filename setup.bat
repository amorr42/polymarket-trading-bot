@echo off
REM ============================================================================
REM Polymarket Trading Bot - Windows Kurulum Scripti
REM ============================================================================

echo ======================================
echo Polymarket Trading Bot Kurulumu
echo ======================================
echo.

REM Python versiyonunu kontrol et
echo Python versiyonu kontrol ediliyor...
python --version
if %errorlevel% neq 0 (
    echo HATA: Python bulunamadi! Python 3.11 veya uzeri yuklemeniz gerekiyor.
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)
echo.

REM Sanal ortam olustur
echo Sanal ortam olusturuluyor...
if exist venv (
    echo Mevcut venv klasoru siliniyor...
    rmdir /s /q venv
)
python -m venv venv
if %errorlevel% neq 0 (
    echo HATA: Sanal ortam olusturulamadi!
    pause
    exit /b 1
)
echo Sanal ortam basariyla olusturuldu.
echo.

REM Sanal ortami aktif et
echo Sanal ortam aktif ediliyor...
call venv\Scripts\activate.bat
if %errorlevel% neq 0 (
    echo HATA: Sanal ortam aktif edilemedi!
    pause
    exit /b 1
)
echo.

REM pip'i guncelleyin
echo pip guncelleniyor...
python -m pip install --upgrade pip
echo.

REM Gereksinimleri yukle
echo Python paketleri yukleniyor (bu biraz zaman alabilir)...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo HATA: Paketler yuklenemedi!
    pause
    exit /b 1
)
echo.

echo ======================================
echo Kurulum basariyla tamamlandi!
echo ======================================
echo.
echo SONRAKI ADIMLAR:
echo 1. PostgreSQL'i yukleyin (SETUP.md dosyasina bakin)
echo 2. setup_database.bat scriptini calistirin
echo 3. .env dosyasini duzenleyin (wallet bilgilerinizi ekleyin)
echo 4. config.yaml dosyasini duzenleyin
echo.
echo PROJEYI CALISTIRMAK ICIN:
echo   1. venv\Scripts\activate.bat
echo   2. python apps\db_alert_watcher.py
echo.
pause

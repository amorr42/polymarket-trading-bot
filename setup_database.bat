@echo off
REM ============================================================================
REM PostgreSQL Veritabani Kurulum Scripti
REM ============================================================================

echo ======================================
echo PostgreSQL Veritabani Kurulumu
echo ======================================
echo.

REM PostgreSQL kurulu mu kontrol et
where psql >nul 2>nul
if %errorlevel% neq 0 (
    echo HATA: PostgreSQL bulunamadi!
    echo PostgreSQL yuklemek icin:
    echo 1. https://www.postgresql.org/download/windows/ adresine gidin
    echo 2. PostgreSQL yukleyiciyi indirin ve kurun
    echo 3. Kurulum sirasinda bir sifre belirleyin (ornek: postgres123)
    echo 4. Port olarak varsayilan 5432'yi kullanin
    echo.
    pause
    exit /b 1
)
echo PostgreSQL bulundu!
echo.

REM Kullanicidan veritabani bilgilerini al
set /p DB_USER="PostgreSQL kullanici adi (varsayilan: postgres): "
if "%DB_USER%"=="" set DB_USER=postgres

set /p DB_PASSWORD="PostgreSQL sifresi: "
if "%DB_PASSWORD%"=="" (
    echo HATA: Sifre bos olamaz!
    pause
    exit /b 1
)

set /p DB_HOST="PostgreSQL host (varsayilan: localhost): "
if "%DB_HOST%"=="" set DB_HOST=localhost

set /p DB_PORT="PostgreSQL port (varsayilan: 5432): "
if "%DB_PORT%"=="" set DB_PORT=5432

set DB_NAME=polymarket

echo.
echo Veritabani olusturuluyor: %DB_NAME%
echo.

REM Veritabani olustur
set PGPASSWORD=%DB_PASSWORD%
psql -U %DB_USER% -h %DB_HOST% -p %DB_PORT% -c "CREATE DATABASE %DB_NAME%;" 2>nul
if %errorlevel% equ 0 (
    echo Veritabani basariyla olusturuldu!
) else (
    echo Veritabani zaten mevcut veya olusturulamadi.
    echo Devam ediliyor...
)
echo.

REM .env dosyasini guncelle
echo .env dosyasi guncelleniyor...
set DATABASE_URL=postgresql://%DB_USER%:%DB_PASSWORD%@%DB_HOST%:%DB_PORT%/%DB_NAME%

REM .env dosyasinda DATABASE_URL'i guncelle
powershell -Command "(Get-Content .env) -replace '^DATABASE_URL=.*', 'DATABASE_URL=%DATABASE_URL%' | Set-Content .env"

echo DATABASE_URL guncellendi: %DATABASE_URL%
echo.

REM Sanal ortami aktif et ve veritabani tablolarini olustur
echo Veritabani tablolari olusturuluyor...
call venv\Scripts\activate.bat

REM Python ile veritabani semayi olustur
python -c "from lib.db import connect, ensure_schema; conn = connect(); ensure_schema(conn); conn.close(); print('Veritabani tablolari basariyla olusturuldu!')"

if %errorlevel% neq 0 (
    echo.
    echo UYARI: Tablolar otomatik olarak olusturulamadi.
    echo Uygulamayi calistirdiginizda otomatik olarak olusturulacaklar.
)

echo.
echo ======================================
echo Veritabani kurulumu tamamlandi!
echo ======================================
echo.
echo DATABASE_URL: %DATABASE_URL%
echo.
echo SONRAKI ADIMLAR:
echo 1. .env dosyasini kontrol edin
echo 2. config.yaml dosyasini duzenleyin (wallet bilgilerinizi ekleyin)
echo 3. python apps\ingest_markets_pg.py ile market verilerini yukleyin
echo.
pause

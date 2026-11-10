@echo off
REM Windows Görev Zamanlayıcı bu dosyayı her gün çalıştırır.
cd /d "%~dp0"
python tarayici.py >> tarama.log 2>&1

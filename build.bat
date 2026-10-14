@echo off
setlocal
cd /d "%~dp0"

echo === Cerrando aplicacionjunior.exe si esta abierto ===
taskkill /F /IM "aplicacionjunior.exe" /T >nul 2>&1
ping -n 3 127.0.0.1 >nul

echo === Limpiando basura ===
if exist "utilidades\build" rmdir /s /q "utilidades\build"
if exist "utilidades\__pycache__" rmdir /s /q "utilidades\__pycache__"
del /q "ultimo_*.html" 2>nul
del /q "ultimo_*.js" 2>nul
del /q "_extra_*.js" 2>nul

echo === Instalando dependencias Python ===
python -m pip install -r "utilidades\requirements.txt" pyinstaller
if errorlevel 1 (
  echo Error: no se pudieron instalar las dependencias.
  goto :fin_error
)

echo.
echo === Compilando aplicacionjunior.exe en la carpeta principal ===
pushd "utilidades"
python -m PyInstaller --noconfirm --clean --distpath ".." --workpath "build" aplicacionjunior.spec
set "ERR=%ERRORLEVEL%"
popd
if not "%ERR%"=="0" (
  echo Error: PyInstaller fallo.
  goto :fin_error
)

echo.
echo === Limpiando archivos de compilacion ===
if exist "utilidades\build" rmdir /s /q "utilidades\build"
if exist "utilidades\__pycache__" rmdir /s /q "utilidades\__pycache__"
for /d /r "utilidades" %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"

echo.
if exist "%CD%\aplicacionjunior.exe" (
  echo Listo: %CD%\aplicacionjunior.exe
  echo El .env y la base de datos siguen en esta misma carpeta.
) else (
  echo Error: no se genero el ejecutable.
  goto :fin_error
)

if /I "%~1"=="nopause" goto :eof
pause
endlocal
exit /b 0

:fin_error
if /I "%~1"=="nopause" exit /b 1
pause
endlocal
exit /b 1

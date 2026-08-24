@echo off
rem Dupla kattintassal indithato a Fajlkezelobol. A mappa barhova masolhato:
rem minden beallitas a data almappaban marad, semmit nem ir rajta kivulre.
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Nem talalok Pythont. Telepitsd a python.org oldalrol, es a telepitonel
  echo pipald ki az "Add python.exe to PATH" jelolonegyzetet.
  echo.
  pause
  exit /b 1
)

rem Az elso indulasnal a Windows tuzfala rakerdez a bejovo kapcsolatra:
rem engedelyezni kell, kulonben a TV nem eri el a fajlokat.
%PY% -u server.py %*

echo.
pause

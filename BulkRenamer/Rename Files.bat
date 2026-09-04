@echo off
rem Bulk Renamer launcher.
rem
rem Uses the Python that ships inside this folder, so nothing has to be
rem installed. If that folder is ever removed, it falls back to a Python on the
rem system PATH rather than simply failing.

cd /d "%~dp0"

if exist "runtime\python.exe" (
	"runtime\python.exe" "app\server.py"
	goto :end
)

where python >nul 2>nul
if %errorlevel%==0 (
	python "app\server.py"
	goto :end
)

echo.
echo   The bundled runtime folder is missing, and no Python was found either.
echo   Re-extract the whole BulkRenamer folder from the zip and try again.
echo.
pause

:end

@echo off
setlocal
cd /d "%~dp0"

if defined HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH (
    echo [INFO] Project picker preview handoff:
    echo        HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH=%HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH%
    if not exist "%HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH%" (
        echo [WARN] Project picker preview file does not exist yet.
        echo        Haypile GUI will start and show the missing preview state.
    )
) else (
    echo [INFO] Project picker preview handoff not set.
)

python app_gui.py
exit /b %ERRORLEVEL%

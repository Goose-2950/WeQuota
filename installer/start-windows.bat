@echo off
chcp 65001 >nul
rem WeQuota 启动脚本（Windows）：双击即可运行打包产物，控制台会打印大屏地址
setlocal
cd /d "%~dp0.."
if exist "dist\WeQuota\WeQuota.exe" (
  start "" "dist\WeQuota\WeQuota.exe" %*
) else if exist "dist\WeQuota.exe" (
  start "" "dist\WeQuota.exe" %*
) else (
  echo 未找到打包产物，请先运行：python -m installer.build_release
  pause
)
endlocal

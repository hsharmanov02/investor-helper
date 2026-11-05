@echo off
REM Starts your normal Chrome profile with the debug port Investor Helper reads from.
REM Close all Chrome windows first, otherwise Chrome ignores the flag.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --profile-directory=Default

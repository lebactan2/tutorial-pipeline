& python -c "print('hello'); print('world')" 2>&1 | Tee-Object -Variable outVar
Write-Host "Last is:" ($outVar | Select-Object -Last 1).ToString().Trim()

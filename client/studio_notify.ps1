param([string]$Message = 'AI Studio 任务状态已更新')
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$studioNotice = New-Object System.Windows.Forms.NotifyIcon
$studioNotice.Icon = [System.Drawing.SystemIcons]::Information
$studioNotice.Visible = $true
$studioNotice.BalloonTipTitle = 'AI Studio 游戏工作室'
$studioNotice.BalloonTipText = $Message
$studioNotice.ShowBalloonTip(5000)
Start-Sleep -Seconds 6
$studioNotice.Dispose()

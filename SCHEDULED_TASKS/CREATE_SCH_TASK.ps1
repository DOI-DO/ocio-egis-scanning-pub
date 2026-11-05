#create a 'folder' in task scheduler to keep things organized
$TaskPath   = '\PTERODACTYL\'
#doesn't matter what you call it
$TaskName   = 'ORG_PORTAL_ScanAttachmentsSize'
#this is the full account name of your gMSA
$UserId     = 'ORG\IORG_gSMA_ACCOUNT'
#path to python executable
$PythonExe  = 'D:\User\TomPy\python.exe'
$WorkingDir = 'D:\User\PTERODACTYL_ORG'
#path to python script
$ScriptPath = 'D:\User\PTERODACTYL_ORG\ATTACHMENTS\SCAN_ATTACHMENTS_SIZE.py'
#path to the config.toml: you can have many configs for many orgs or different scan output scenarios
$ConfigPath = 'D:\User\PTERODACTYL_ORG\UTILITIES\ORG.config.toml'
#be sure to use valid and legal parameters here
#the most common reason (99%) of scheduled task failure is due to invalid parameters
$PyArgs = @(
  "`"$ScriptPath`""
  '--config', "`"$ConfigPath`""
  '--target', 'portal'
  '--threshold-mb', '6'
  '--no-test'
) -join ' '

$action    = New-ScheduledTaskAction  -Execute $PythonExe -Argument $PyArgs -WorkingDirectory $WorkingDir
$trigger   = New-ScheduledTaskTrigger -Daily -At '06:00'
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Password -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RunOnlyIfNetworkAvailable:$false

Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
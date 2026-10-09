#this emulates a gMSA in order to seed encrypted credentials in the Windows Credential Manager
#so they're available to the gMSA during a scheduled task
#create a 'folder' in task scheduler to keep things organized
$TaskPath  = '\PTERODACTYL\'
#this is a temp task, doesn't matter what you call it
$TaskName  = 'Seed-Password'
#this is the full account name of your gMSA
$GmsaUser  = 'ORG\IORG_gSMA_ACCOUNT'
#this is the password for the credential you are seeding
#this is not save in plaintext anywhere unless you save this ps1 file
$PortalPw  = 'some password'   # replace
#generic: is the 'Intenet or network addresss' in credential manager and the same as 'keyring-service' for portal or agol in your config.toml file
#user: is the 'User Name' in credential manager and the same as 'username' for portal or agol in your config.toml fil
$action    = New-ScheduledTaskAction  -Execute 'cmd.exe' -Argument "/c cmdkey /generic:PORTAL /user:test_user /pass:$PortalPw"
$trigger   = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
$principal = New-ScheduledTaskPrincipal -UserId $GmsaUser -LogonType Password -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
Start-ScheduledTask    -TaskPath $TaskPath -TaskName $TaskName
Start-Sleep -Seconds 10
#this task will be deleted after you run it. The password is not retained anywhere unless you have have powershell logging enabled as a local policy: if so, delete that log
Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false
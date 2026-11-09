
#checks to see that the proper credentials are actually stored in windows credential manager
#this task will persist so you can run it when ever you want to check. It does NOT expose the password
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c cmdkey /list > "c:\temp\gmsa_creds.txt" 2>&1'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
#this is the full account name of your gMSA
$principal = New-ScheduledTaskPrincipal -UserId 'ORG\IORG_gSMA_ACCOUNT' -LogonType Password -RunLevel Highest
Register-ScheduledTask -TaskPath '\pterodactyl\' -TaskName 'List-GMSA-Creds' -Action $action -Trigger $trigger -Principal $principal -Force
Start-ScheduledTask -TaskPath '\pterodactyl\' -TaskName 'List-GMSA-Creds'

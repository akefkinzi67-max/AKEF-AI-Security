param([switch]$SmokeTest, [switch]$PilotSmokeTest)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Set-Location -LiteralPath $PSScriptRoot

$form = New-Object Windows.Forms.Form
$form.Text = 'NAVIQRA Station Setup'
$form.ClientSize = New-Object Drawing.Size(500, 415)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$description = New-Object Windows.Forms.Label
$description.Location = New-Object Drawing.Point(20, 20)
$description.Size = New-Object Drawing.Size(460, 65)
$description.Text = 'Install the station application and open the local setup wizard. Use a private Windows folder for this installation. Frigate is installed and configured separately.'
$form.Controls.Add($description)
$install = New-Object Windows.Forms.Button
$install.Text = 'Install and open NAVIQRA'
$install.Location = New-Object Drawing.Point(20, 205)
$install.Size = New-Object Drawing.Size(235, 33)
$form.Controls.Add($install)
$open = New-Object Windows.Forms.Button
$open.Text = 'Open running station'
$open.Location = New-Object Drawing.Point(265, 205)
$open.Size = New-Object Drawing.Size(210, 33)
$form.Controls.Add($open)
$status = New-Object Windows.Forms.Label
$status.Location = New-Object Drawing.Point(20, 295)
$status.Size = New-Object Drawing.Size(460, 50)
$form.Controls.Add($status)
$tokenBox = New-Object Windows.Forms.TextBox
$tokenBox.ReadOnly = $true
$tokenBox.Location = New-Object Drawing.Point(20, 360)
$tokenBox.Width = 455
$tokenBox.Visible = $false
$form.Controls.Add($tokenBox)
$backupButton = New-Object Windows.Forms.Button
$backupButton.Text = 'Back up station data'
$backupButton.Location = New-Object Drawing.Point(20, 250)
$backupButton.Size = New-Object Drawing.Size(235, 33)
$form.Controls.Add($backupButton)
$restoreButton = New-Object Windows.Forms.Button
$restoreButton.Text = 'Create recovery copy'
$restoreButton.Location = New-Object Drawing.Point(265, 250)
$restoreButton.Size = New-Object Drawing.Size(210, 33)
$form.Controls.Add($restoreButton)
$urlLabel = New-Object Windows.Forms.Label
$urlLabel.Location = New-Object Drawing.Point(20, 85)
$urlLabel.AutoSize = $true
$urlLabel.Text = 'Frigate service URL (optional)'
$form.Controls.Add($urlLabel)
$frigateUrl = New-Object Windows.Forms.TextBox
$frigateUrl.Location = New-Object Drawing.Point(20, 105)
$frigateUrl.Width = 455
$form.Controls.Add($frigateUrl)
$credentialLabel = New-Object Windows.Forms.Label
$credentialLabel.Location = New-Object Drawing.Point(20, 133)
$credentialLabel.AutoSize = $true
$credentialLabel.Text = 'Frigate session token (optional)'
$form.Controls.Add($credentialLabel)
$frigateToken = New-Object Windows.Forms.TextBox
$frigateToken.Location = New-Object Drawing.Point(20, 153)
$frigateToken.Width = 455
$frigateToken.UseSystemPasswordChar = $true
$form.Controls.Add($frigateToken)
$directory = Join-Path $PSScriptRoot 'data'
$url = 'http://127.0.0.1:8080/'
$listen = '127.0.0.1:8080'
if ($PilotSmokeTest) {
    $directory = Join-Path ([IO.Path]::GetTempPath()) ('naviqra-pilot-' + [guid]::NewGuid().ToString('N'))
    $url = 'http://127.0.0.1:28687/'
    $listen = '127.0.0.1:28687'
}
$secretsFile = Join-Path $directory 'windows-setup.json'
$backupFile = Join-Path $PSScriptRoot 'backup.py'
if (Test-Path -LiteralPath $secretsFile) {
    $previous = Get-Content -LiteralPath $secretsFile -Raw | ConvertFrom-Json
    $frigateUrl.Text = [string]$previous.frigateUrl
}
$open.Add_Click({
    try {
        $page = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2
        if (-not $page.Content.Contains('<title>NAVIQRA Gas Station AI Management</title>')) { throw 'Different local service.' }
        if ($page.Content.Contains('Set up your station')) {
            $saved = Get-Content -LiteralPath $secretsFile -Raw | ConvertFrom-Json
            $tokenBox.Text = [Net.NetworkCredential]::new('', (ConvertTo-SecureString $saved.token)).Password
            $tokenBox.Visible = $true
            $status.Text = 'Copy this setup token into the browser wizard, then create the owner account.'
        }
        Start-Process $url
    } catch { $status.Text = 'NAVIQRA is not running on local port 8080. Use Install and open NAVIQRA.' }
})

function Save-StationDatabaseCopy([string]$Source, [string]$Title) {
    $python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw 'Install NAVIQRA before copying the database.' }
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { throw 'The selected station database does not exist.' }
    $destination = New-Object Windows.Forms.SaveFileDialog
    $destination.Title = $Title
    $destination.Filter = 'Station databases (*.sqlite3)|*.sqlite3'
    $destination.FileName = "naviqra-$(Get-Date -Format yyyyMMdd-HHmmss).sqlite3"
    try {
        if ($destination.ShowDialog($form) -ne [Windows.Forms.DialogResult]::OK) { return }
        if (Test-Path -LiteralPath $destination.FileName) { throw 'Choose a new filename to protect the existing copy.' }
        & $python $backupFile $Source $destination.FileName
        if ($LASTEXITCODE -ne 0) { throw 'The database could not be copied and verified.' }
        $status.Text = 'Verified database copy saved at the selected destination.'
    } finally { $destination.Dispose() }
}

$backupButton.Add_Click({
    try { Save-StationDatabaseCopy (Join-Path $directory 'station.sqlite3') 'Save verified station backup' }
    catch { $status.Text = $_.Exception.Message }
})
$restoreButton.Add_Click({
    $source = New-Object Windows.Forms.OpenFileDialog
    $source.Title = 'Choose a station backup'
    $source.Filter = 'Station databases (*.sqlite3)|*.sqlite3'
    try {
        if ($source.ShowDialog($form) -eq [Windows.Forms.DialogResult]::OK) {
            Save-StationDatabaseCopy $source.FileName 'Save recovery copy to a new file'
        }
    } catch { $status.Text = $_.Exception.Message }
    finally { $source.Dispose() }
})

function New-SetupSecret {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    [Convert]::ToBase64String($bytes)
}

$install.Add_Click({
    $install.Enabled = $false
    $status.Text = 'Preparing Python and installing application files...'
    $form.Refresh()
    try {
        $python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
        if (-not (Test-Path -LiteralPath $python)) {
            $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
            if ($launcher) { & $launcher.Source -3.12 --version 2>$null }
            if (-not $launcher -or $LASTEXITCODE -ne 0) {
                $choice = [Windows.Forms.MessageBox]::Show('Install Python 3.12 from Microsoft WinGet for the current Windows user?', 'NAVIQRA Station Setup', 'YesNo', 'Question')
                if ($choice -ne 'Yes') { throw 'Python 3.12 is required to continue.' }
                $winget = Get-Command winget.exe -ErrorAction Stop
                $bootstrap = Start-Process -FilePath $winget.Source -ArgumentList 'install','--id','Python.Python.3.12','--exact','--scope','user','--silent','--accept-package-agreements','--accept-source-agreements' -WindowStyle Hidden -PassThru -Wait
                if ($bootstrap.ExitCode -ne 0) { throw 'WinGet could not install Python 3.12.' }
                $basePython = Join-Path $env:LOCALAPPDATA 'Programs/Python/Python312/python.exe'
                if (-not (Test-Path -LiteralPath $basePython)) { throw 'Python was installed in a custom location. Reopen setup after signing in again.' }
                & $basePython -m venv .venv
                if ($LASTEXITCODE -ne 0) { throw 'Could not create the application environment.' }
            }
        }
        $shell = (Get-Process -Id $PID).Path
        $manifest = Join-Path $PSScriptRoot '.venv/naviqra-requirements.sha256'
        $requirementsHash = (Get-FileHash -LiteralPath 'requirements.txt' -Algorithm SHA256).Hash
        if (-not (Test-Path -LiteralPath $manifest) -or (Get-Content -LiteralPath $manifest -Raw).Trim() -ne $requirementsHash) {
            $installer = Start-Process -FilePath $shell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','install-station.ps1' -WindowStyle Hidden -PassThru -Wait
            if ($installer.ExitCode -ne 0) { throw 'Dependency setup failed. Check the internet connection and retry.' }
            Set-Content -LiteralPath $manifest -Value $requirementsHash -Encoding ascii
        }
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        $saved = if (Test-Path -LiteralPath $secretsFile) {
            Get-Content -LiteralPath $secretsFile -Raw | ConvertFrom-Json
        } else {
            [pscustomobject]@{
                secret = (ConvertTo-SecureString (New-SetupSecret) -AsPlainText -Force | ConvertFrom-SecureString)
                token = (ConvertTo-SecureString (New-SetupSecret) -AsPlainText -Force | ConvertFrom-SecureString)
            }
        }
        $nextOrigin = $frigateUrl.Text.Trim()
        $nextToken = $null
        if ($nextOrigin) {
            $uri = [uri]$nextOrigin
            if (-not $uri.IsAbsoluteUri -or $uri.Scheme -notin @('http','https') -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne '/') {
                throw 'Enter the Frigate service origin, such as https://frigate.example.com.'
            }
            if ($frigateToken.Text) {
                if ($uri.Scheme -ne 'https' -or $frigateToken.Text -notmatch '^[A-Za-z0-9_.-]+$') { throw 'A valid Frigate token requires HTTPS.' }
                $nextToken = ConvertTo-SecureString $frigateToken.Text -AsPlainText -Force | ConvertFrom-SecureString
            } elseif ($saved.frigateUrl -eq $nextOrigin) { $nextToken = $saved.frigateToken }
        }
        @{
            secret = $saved.secret
            token = $saved.token
            frigateUrl = $nextOrigin
            frigateToken = $nextToken
        } | ConvertTo-Json | Set-Content -LiteralPath $secretsFile -Encoding UTF8
        $env:NAVIQRA_SECRET = [Net.NetworkCredential]::new('', (ConvertTo-SecureString $saved.secret)).Password
        $env:NAVIQRA_SETUP_TOKEN = [Net.NetworkCredential]::new('', (ConvertTo-SecureString $saved.token)).Password
        $env:NAVIQRA_FRIGATE_URL = $nextOrigin
        if ($nextToken) { $env:NAVIQRA_FRIGATE_TOKEN = [Net.NetworkCredential]::new('', (ConvertTo-SecureString $nextToken)).Password }
        $env:NAVIQRA_DB = Join-Path $directory 'station.sqlite3'
        $started = Start-Process -FilePath $python -ArgumentList '-m','waitress',"--listen=$listen",'--threads=24','--call','station:create_app' -WindowStyle Hidden -PassThru
        if ($PilotSmokeTest) { $script:pilotProcess = $started }
        $ready = $null
        for ($attempt = 0; $attempt -lt 12; $attempt++) {
            Start-Sleep -Milliseconds 400
            if ($started.HasExited) { break }
            try {
                $ready = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2
                if ($ready.Content.Contains('<title>NAVIQRA Gas Station AI Management</title>')) { break }
            } catch { $ready = $null }
        }
        if ($started.HasExited -or -not $ready -or -not $ready.Content.Contains('<title>NAVIQRA Gas Station AI Management</title>')) {
            throw 'NAVIQRA could not start. Check that local port 8080 is free.'
        }
        if ($nextOrigin) {
            Start-Process -FilePath $python -ArgumentList '-m','monitor' -WindowStyle Hidden
        }
        if ($ready.Content.Contains('Set up your station')) {
            $status.Text = 'Copy this setup token into the browser wizard, then create the owner account.'
            $tokenBox.Text = $env:NAVIQRA_SETUP_TOKEN
            $tokenBox.Visible = $true
        } else { $status.Text = 'NAVIQRA is running. Sign in to your station.' }
        if ($PilotSmokeTest) {
            $script:pilotPython = $python
            $script:pilotToken = $env:NAVIQRA_SETUP_TOKEN
        } else { Start-Process $url }
    } catch {
        $status.Text = $_.Exception.Message
        $install.Enabled = $true
    } finally {
        Remove-Item Env:NAVIQRA_SECRET,Env:NAVIQRA_SETUP_TOKEN,Env:NAVIQRA_FRIGATE_TOKEN -ErrorAction SilentlyContinue
    }
})
if ($PilotSmokeTest) {
    try {
        $form.Show()
        $install.PerformClick()
        if (-not $script:pilotToken) { throw $status.Text }
        $env:NAVIQRA_SETUP_TOKEN = $script:pilotToken
        & $script:pilotPython pilot_http.py seed --base $url
        if ($LASTEXITCODE -ne 0) { throw 'Windows first-run station setup failed.' }
        $active = Join-Path $directory 'station.sqlite3'
        $backedUp = Join-Path $directory 'pilot-backup.sqlite3'
        $restored = Join-Path $directory 'pilot-restored.sqlite3'
        & $script:pilotPython backup.py $active $backedUp
        if ($LASTEXITCODE -ne 0) { throw 'Windows Pilot backup failed.' }
        & $script:pilotPython backup.py $backedUp $restored
        if ($LASTEXITCODE -ne 0) { throw 'Windows Pilot restore failed.' }
        Stop-Process -InputObject $script:pilotProcess
        $script:pilotProcess.WaitForExit()
        $pilotSettings = Get-Content -LiteralPath $secretsFile -Raw | ConvertFrom-Json
        $env:NAVIQRA_SECRET = [Net.NetworkCredential]::new('', (ConvertTo-SecureString $pilotSettings.secret)).Password
        $env:NAVIQRA_DB = $restored
        $script:pilotProcess = Start-Process -FilePath $script:pilotPython -ArgumentList '-m','waitress',"--listen=$listen",'--threads=24','--call','station:create_app' -WindowStyle Hidden -PassThru
        $ready = $false
        for ($attempt = 0; $attempt -lt 12; $attempt++) {
            Start-Sleep -Milliseconds 400
            if ($script:pilotProcess.HasExited) { break }
            try {
                $page = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2
                if ($page.Content.Contains('Sign in')) { $ready = $true; break }
            } catch { }
        }
        if (-not $ready) { throw 'Windows Pilot could not open the restored station.' }
        & $script:pilotPython pilot_http.py verify --base $url
        if ($LASTEXITCODE -ne 0) { throw 'Windows Pilot restore data verification failed.' }
    } finally {
        if ($script:pilotProcess -and -not $script:pilotProcess.HasExited) {
            Stop-Process -InputObject $script:pilotProcess
        }
        $form.Dispose()
        Remove-Item Env:NAVIQRA_SETUP_TOKEN,Env:NAVIQRA_SECRET,Env:NAVIQRA_DB -ErrorAction SilentlyContinue
    }
} elseif ($SmokeTest) {
    if ($form.Controls.Count -lt 8 -or $install.Text -ne 'Install and open NAVIQRA') {
        throw 'Station setup controls are missing.'
    }
    $form.Dispose()
    Write-Output 'Windows station setup controls initialized.'
} else {
    [void]$form.ShowDialog()
}

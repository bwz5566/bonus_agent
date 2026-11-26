param(
    [ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$Model = 'deepseek-flash',
    [string]$DataDir = 'D:\bonus_data'
)
$ErrorActionPreference = 'Stop'
$project = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..')).FullName
$data = (Get-Item -LiteralPath $DataDir).FullName
$listeners = @(Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique)
if ($listeners.Count -gt 1) { throw 'Multiple services own port 8080.' }
$python = (Get-Command python -CommandType Application | Select-Object -First 1).Source
$startArgs = '-u -X utf8 -m bonus_agent.cli serve --data "' + $data + '" --host 127.0.0.1 --port 8080 --model ' + $Model
if ($listeners.Count) {
    $service = Get-CimInstance Win32_Process -Filter "ProcessId=$($listeners[0])"
    $old = Get-Process -Id $listeners[0]
    $command = [regex]::Match($service.CommandLine, '^(?:"[^"]+"|\S+)\s+(?<args>(?:-\S+\s+|utf8\s+)*-m\s+bonus_agent\.cli\s+serve\s+.+)$')
    $dataFlag = [regex]::Match($service.CommandLine, '(?:^|\s)--data(?:\s+|=)(?:"([^"]+)"|(\S+))')
    $existingData = ($dataFlag.Groups[1].Value + $dataFlag.Groups[2].Value)
    if (-not $command.Success -or -not $existingData -or [IO.Path]::GetFullPath($existingData) -ne $data -or
        $service.CommandLine -notmatch '\s--host(?:\s+|=)"?127\.0\.0\.1"?(?:\s|$)' -or
        $service.CommandLine -notmatch '\s--port(?:\s+|=)"?8080"?(?:\s|$)' -or
        [IO.Path]::GetFileName($service.ExecutablePath) -ne 'python.exe') {
        throw 'Port 8080 is not owned by the expected local bonus service and data directory.'
    }
    $python = $service.ExecutablePath
    $oldArgs = $command.Groups['args'].Value
    # Use the selected model's checker. Preserve all other options, including case-specific limits.
    $startArgs = [regex]::Replace($oldArgs, '\s+--(?:model|checker)(?:\s+|=)(?:"[^"]+"|\S+)', '') + ' --model ' + $Model
}

function Start-BonusService([string]$Arguments) {
    $child = Start-Process -FilePath $python -ArgumentList $Arguments -WorkingDirectory $project -WindowStyle Hidden -PassThru
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        if ($child.HasExited) { throw "Server exited with code $($child.ExitCode)." }
        try { $page = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8080/login' -TimeoutSec 2 } catch { continue }
        if ($page.StatusCode -eq 200 -and (Get-NetTCPConnection -LocalPort 8080 -State Listen).OwningProcess -eq $child.Id) {
            return $child.Id
        }
    }
    if (-not $child.HasExited) { $child.Kill(); $child.WaitForExit() }
    throw 'Server did not become ready on port 8080.'
}

Push-Location -LiteralPath $project
try {
    & $python -X utf8 -c "import sys; from bonus_agent.llm import LLM; print(LLM.from_config(sys.argv[1]).name)" $Model
    if ($LASTEXITCODE -ne 0) { throw 'Model configuration check failed.' }
    if ($listeners.Count) {
        foreach ($case in (Get-ChildItem -LiteralPath (Join-Path $data 'cases') -Directory -ErrorAction SilentlyContinue)) {
            if ($case.Name -notmatch '^[0-9a-f]{12}$') { continue }
            foreach ($rel in @('.model_budget\.case.lock', 'out\.case.lock')) {
                $lock = Join-Path $case.FullName $rel
                if (-not (Test-Path -LiteralPath $lock -PathType Leaf)) { continue }
                $file = [IO.File]::Open($lock, 'Open', 'ReadWrite', 'ReadWrite')
                try { $file.Lock(0, 1); $file.Unlock(0, 1) }
                catch { throw "An active job holds case $($case.Name). Wait for it before restarting." }
                finally { $file.Dispose() }
            }
        }
        if ($old.HasExited -or (Get-NetTCPConnection -LocalPort 8080 -State Listen).OwningProcess -ne $old.Id) {
            throw 'The listener changed during the check. Run the command again.'
        }
        $old.Kill()
        $old.WaitForExit()
    }
    try { $newId = Start-BonusService $startArgs }
    catch {
        if ($listeners.Count) { $null = Start-BonusService $oldArgs }
        throw
    }
    Write-Output "$Model  PID=$newId  http://127.0.0.1:8080"
}
finally { Pop-Location }

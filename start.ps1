param(
    [ValidateSet("backend", "studio", "all")]
    [string]$Target = "all",
    [int]$BackendPort = 8000,
    [int]$StudioPort = 3000,
    [int]$OtelGrpcPort = 4317,
    [switch]$NoReload
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$backendRoot = Join-Path $projectRoot "backend"

function Get-ListenerProcessId([int]$Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) {
        return [int]$listener.OwningProcess
    }
    return 0
}

function Resolve-StudioCommand {
    $studio = Get-Command as_studio -ErrorAction SilentlyContinue
    if (-not $studio) {
        throw "AgentScope Studio is not installed. Run: npm install -g @agentscope/studio"
    }
    $studioRoot = Split-Path $studio.Source -Parent
    $node = Join-Path $studioRoot "node.exe"
    $cli = Join-Path $studioRoot "node_modules\@agentscope\studio\bin\cli.js"
    if (-not (Test-Path $node) -or -not (Test-Path $cli)) {
        throw "Cannot resolve the AgentScope Studio Node.js entry point."
    }
    return @{ FilePath = $node; Arguments = @("`"$cli`"") }
}

function Resolve-PythonCommand {
    $candidates = [System.Collections.Generic.List[string]]::new()
    if ($env:PLAN_GENERATOR_PYTHON) {
        $candidates.Add($env:PLAN_GENERATOR_PYTHON)
    }
    if ($env:CONDA_PREFIX -and $env:CONDA_DEFAULT_ENV -eq "plan-generator") {
        $candidates.Add((Join-Path $env:CONDA_PREFIX "python.exe"))
    }
    $condaEnvironments = Join-Path $env:USERPROFILE ".conda\environments.txt"
    if (Test-Path $condaEnvironments) {
        foreach ($environmentPath in Get-Content $condaEnvironments) {
            if ((Split-Path $environmentPath.Trim() -Leaf) -eq "plan-generator") {
                $candidates.Add((Join-Path $environmentPath.Trim() "python.exe"))
            }
        }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        $candidates.Add($python.Source)
    }

    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (-not (Test-Path $candidate)) {
            continue
        }
        & $candidate -c "import uvicorn, agentscope, fastapi, alibabacloud_bailian20231229" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return $candidate
        }
    }
    throw "Cannot find a Python environment with uvicorn and agentscope. Activate plan-generator or set PLAN_GENERATOR_PYTHON."
}

function Get-BackendArguments {
    $arguments = @(
        "-m", "uvicorn", "main:app",
        "--host", "0.0.0.0",
        "--port", "$BackendPort"
    )
    if (-not $NoReload) {
        $arguments += "--reload"
    }
    return $arguments
}

function Stop-ProcessTree([int]$RootProcessId) {
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $RootProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree -RootProcessId ([int]$child.ProcessId)
    }
    Stop-Process -Id $RootProcessId -Force -ErrorAction SilentlyContinue
}

function Start-StudioForeground {
    if (Get-ListenerProcessId -Port $StudioPort) {
        Write-Host "AgentScope Studio is already running at http://127.0.0.1:$StudioPort"
        return
    }
    $studio = Resolve-StudioCommand
    $env:PORT = "$StudioPort"
    $env:OTEL_GRPC_PORT = "$OtelGrpcPort"
    Write-Host "Starting AgentScope Studio at http://127.0.0.1:$StudioPort"
    & $studio.FilePath $studio.Arguments
}

function Start-BackendForeground {
    if (Get-ListenerProcessId -Port $BackendPort) {
        throw "Backend port $BackendPort is already in use."
    }
    $python = Resolve-PythonCommand
    $arguments = Get-BackendArguments
    Write-Host "Starting backend at http://127.0.0.1:$BackendPort"
    Push-Location $backendRoot
    try {
        & $python $arguments
    }
    finally {
        Pop-Location
    }
}

function Start-AllServices {
    if (Get-ListenerProcessId -Port $BackendPort) {
        throw "Backend port $BackendPort is already in use."
    }

    $managedProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
    $studioProcess = $null
    $existingStudioPid = Get-ListenerProcessId -Port $StudioPort
    try {
        if ($existingStudioPid) {
            Write-Host "Using existing AgentScope Studio at http://127.0.0.1:$StudioPort"
        }
        else {
            $studio = Resolve-StudioCommand
            $env:PORT = "$StudioPort"
            $env:OTEL_GRPC_PORT = "$OtelGrpcPort"
            $studioStart = @{
                FilePath = $studio.FilePath
                ArgumentList = $studio.Arguments
                NoNewWindow = $true
                PassThru = $true
            }
            $studioProcess = Start-Process @studioStart
            $managedProcesses.Add($studioProcess)
            Write-Host "Started AgentScope Studio at http://127.0.0.1:$StudioPort (PID $($studioProcess.Id))"
            Start-Sleep -Seconds 2
            if ($studioProcess.HasExited) {
                throw "AgentScope Studio exited during startup."
            }
        }

        $env:AGENTSCOPE_OBSERVABILITY_ENABLED = "true"
        $env:AGENTSCOPE_STUDIO_URL = "http://127.0.0.1:$StudioPort"
        $python = Resolve-PythonCommand
        $backendStart = @{
            FilePath = $python
            ArgumentList = (Get-BackendArguments)
            WorkingDirectory = $backendRoot
            NoNewWindow = $true
            PassThru = $true
        }
        $backendProcess = Start-Process @backendStart
        $managedProcesses.Add($backendProcess)
        Write-Host "Started backend at http://127.0.0.1:$BackendPort (PID $($backendProcess.Id))"
        Write-Host "Press Ctrl+C to stop services started by this command."

        while (-not $backendProcess.HasExited) {
            if ($studioProcess -and $studioProcess.HasExited) {
                throw "AgentScope Studio stopped unexpectedly."
            }
            Start-Sleep -Milliseconds 500
        }
    }
    finally {
        foreach ($process in $managedProcesses) {
            if (-not $process.HasExited) {
                Stop-ProcessTree -RootProcessId $process.Id
            }
        }
        Write-Host "Managed services stopped."
    }
}

switch ($Target) {
    "backend" { Start-BackendForeground }
    "studio" { Start-StudioForeground }
    "all" { Start-AllServices }
}

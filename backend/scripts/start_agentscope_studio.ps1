param(
    [int]$Port = 3000,
    [int]$OtelGrpcPort = 4317
)

$studio = Get-Command as_studio -ErrorAction SilentlyContinue
if (-not $studio) {
    throw "AgentScope Studio is not installed. Run: npm install -g @agentscope/studio"
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Host "AgentScope Studio is already available at http://127.0.0.1:$Port"
    exit 0
}

$env:PORT = "$Port"
$env:OTEL_GRPC_PORT = "$OtelGrpcPort"
Write-Host "Starting AgentScope Studio at http://127.0.0.1:$Port"
Write-Host "Keep this terminal open. Press Ctrl+C to stop Studio."
& $studio.Source

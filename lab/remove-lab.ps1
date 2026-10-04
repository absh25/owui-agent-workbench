$ErrorActionPreference = "Stop"

$labDir = Split-Path -Parent $MyInvocation.MyCommand.Path
docker compose --project-directory $labDir down --volumes --remove-orphans --rmi local

Write-Host "Removed the lab containers, network, named volume, and locally built image."
Write-Host "The source directory was preserved: $labDir"

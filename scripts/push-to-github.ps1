# Push proyek ke GitHub. Sekali saja login: gh auth login
# Contoh:
#   .\scripts\push-to-github.ps1
#   .\scripts\push-to-github.ps1 -Owner username -Name sembako-kuningan -Public

param(
    [string] $Owner = "",
    [string] $Name = "sembako-kuningan",
    [switch] $Public
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "Instal GitHub CLI: https://cli.github.com/" -ForegroundColor Red
    exit 1
}

$ErrorActionPreference = "Continue"
gh auth status 2>&1 | Out-Null
$authOk = $LASTEXITCODE -eq 0
$ErrorActionPreference = "Stop"
if (-not $authOk) {
    Write-Host "`nBelum login. Jalankan:`n  gh auth login`n(Pilih GitHub.com, HTTPS, login lewat browser)`nLalu ulang:`n  .\scripts\push-to-github.ps1`n" -ForegroundColor Yellow
    exit 1
}

if (-not $Owner) {
    $Owner = gh api user -q .login
}

$vis = if ($Public) { "--public" } else { "--private" }
Write-Host "Akun: $Owner | Repo: $Name | $($vis.TrimStart('-'))" -ForegroundColor Cyan

$remotes = @(git remote 2>$null)
$hasOrigin = $remotes -contains 'origin'

if (-not $hasOrigin) {
    gh repo create "$Owner/$Name" $vis --source=. --remote=origin --description "Sembako POS (Flask)" --push
} else {
    gh repo view "$Owner/$Name" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Remote origin sudah ada tapi repo $Owner/$Name tidak ditemukan di GitHub. Periksa URL: git remote -v" -ForegroundColor Red
        exit 1
    }
    git push -u origin main
}

Write-Host "`nRepo: https://github.com/$Owner/$Name" -ForegroundColor Green

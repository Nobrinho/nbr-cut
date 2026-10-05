<#
  Gera o NbrCortador.exe (um arquivo só, sem console).

  Uso (na raiz do repositório):
      powershell -ExecutionPolicy Bypass -File build_exe.ps1
      powershell -ExecutionPolicy Bypass -File build_exe.ps1 -PastaUnica   # pasta em vez de 1 arquivo

  Cria/usa .venv (ambiente próprio, sem nada do bot). Nenhum segredo entra no executável: a
  configuração do usuário fica em %APPDATA%\NbrCortador (segredos protegidos pela DPAPI do Windows).
  Saída: dist\NbrCortador.exe
#>
param([switch]$PastaUnica)

$ErrorActionPreference = "Stop"
$raiz = $PSScriptRoot
Set-Location $raiz

$venv = Join-Path $raiz ".venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Criando o ambiente de build em $venv ..."
    python -m venv $venv
}
Write-Host "Instalando dependências ..."
& $py -m pip install -q --disable-pip-version-check -r requirements-dev.txt

$icone = Join-Path $raiz "cortador\assets\icone.ico"
$modo = if ($PastaUnica) { "--onedir" } else { "--onefile" }

Write-Host "Gerando o executável ($modo) ..."
& $py -m PyInstaller --noconfirm --clean $modo --windowed `
    --name NbrCortador `
    --icon $icone `
    --paths $raiz `
    --collect-all customtkinter `
    --collect-binaries psycopg_binary `
    --add-data "${icone};." `
    --distpath (Join-Path $raiz "dist") `
    --workpath (Join-Path $raiz "build") `
    --specpath (Join-Path $raiz "build") `
    (Join-Path $raiz "cortador\__main__.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falhou ($LASTEXITCODE)" }

$saida = if ($PastaUnica) { "dist\NbrCortador\NbrCortador.exe" } else { "dist\NbrCortador.exe" }
$tamanho = [math]::Round((Get-Item (Join-Path $raiz $saida)).Length / 1MB, 1)
Write-Host "Pronto: $saida ($tamanho MB)"

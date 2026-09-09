# Upload dist/* to PyPI using a token from the environment. The token is never echoed.
# Usage:
#   $env:PYPI_API_TOKEN = "<pypi-...>"
#   ./scripts/publish_pypi.ps1

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$token = $env:PYPI_API_TOKEN
if (-not $token) { $token = $env:TWINE_PASSWORD }
if (-not $token) {
    throw "Set PYPI_API_TOKEN (or TWINE_PASSWORD) in the environment. Do not paste the token into chat or git."
}
if (-not ($token.StartsWith("pypi-") -or $token.StartsWith("pypi_"))) {
    throw "Refusing to upload: expected a PyPI API token."
}

$artifacts = @(Get-ChildItem dist\*.whl) + @(Get-ChildItem dist\*.tar.gz)
if ($artifacts.Count -lt 2) {
    throw "dist/ is missing the wheel or sdist. Run python -m build first."
}

$env:TWINE_USERNAME = "__token__"
$env:TWINE_PASSWORD = $token
try {
    python -m twine check --strict dist/*.whl dist/*.tar.gz
    python -m twine upload --non-interactive dist/*.whl dist/*.tar.gz
} finally {
    Remove-Item Env:TWINE_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:TWINE_USERNAME -ErrorAction SilentlyContinue
}

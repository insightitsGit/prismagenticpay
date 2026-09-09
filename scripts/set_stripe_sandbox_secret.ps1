# Stores a Stripe test secret in GitHub Actions without echoing it.
# Usage: paste the sk_test_ key when prompted, then press Enter.

param(
    [string]$Repo = "insightitsGit/prismagenticpay"
)

$ErrorActionPreference = "Stop"
$secure = Read-Host "Stripe sandbox secret (sk_test_...)" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
}

if (-not $key.StartsWith("sk_test_")) {
    throw "Refusing to store a non-sandbox key. Expected an sk_test_ secret."
}

$key | gh secret set STRIPE_API_KEY --repo $Repo --env stripe-sandbox
Write-Host "Stored STRIPE_API_KEY on $Repo environment stripe-sandbox."
Write-Host "Dispatch the Stripe sandbox workflow to run tests/test_stripe_sandbox.py."

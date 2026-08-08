# Generate SSH key pair if not exists, then copy to server
$keyPath = "$env:USERPROFILE\.ssh\id_ed25519_vps"

if (-not (Test-Path $keyPath)) {
    ssh-keygen -t ed25519 -f $keyPath -N '""' -q
    Write-Host "Key generated at $keyPath"
} else {
    Write-Host "Key already exists at $keyPath"
}

Write-Host "Public key content:"
Get-Content "$keyPath.pub"

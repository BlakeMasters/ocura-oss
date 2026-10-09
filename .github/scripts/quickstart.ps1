# Run the README's workflow against the installed package, as a user would type it.
# Works in Windows PowerShell 5.1 and in PowerShell 7.
$ErrorActionPreference = "Stop"

function Assert-Step([string] $What) {
    if ($LASTEXITCODE -ne 0) { throw "$What exited with status $LASTEXITCODE" }
}

function Read-Json([string] $Path) {
    Get-Content -Raw -Path $Path | ConvertFrom-Json
}

$work = Join-Path ([System.IO.Path]::GetTempPath()) ("ocura-quickstart-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $work | Out-Null
Set-Location $work

ocura-oss init --name example --json > init.json
Assert-Step "init"
ocura-oss run --json --param count=1 -- python -c "print(1)" > baseline.json
Assert-Step "baseline run"
$chokepoint = (Read-Json baseline.json).chokepoint_id
ocura-oss branch --json --from $chokepoint --reason "try count 2" --param count=2 > branch.json
Assert-Step "branch"
$pathway = (Read-Json branch.json).id
ocura-oss run --json --pathway $pathway -- python -c "print(2)" > child.json
Assert-Step "child run"
ocura-oss run --json --param batch=4 --substitute -- python -c "import sys; print(sys.argv[1])" "{batch}" > substituted.json
Assert-Step "substituted run"
$printed = (Get-Content -Raw -Path (Read-Json substituted.json).stdout_log).Trim()
if ($printed -ne "4") { throw "--substitute passed '$printed', expected '4'" }
ocura-oss verify --json > verify.json
Assert-Step "verify"
if ((Read-Json verify.json).status -ne "ok") { throw "verify did not report ok" }
ocura-oss compare --json > compare.json
Assert-Step "compare"
if ((Read-Json compare.json).state -ne "ready") { throw "compare is not ready" }
# The child run declared nothing: its count=2 label comes from the branch.
$changed = (Read-Json compare.json).children[0].run_parameters.changed.PSObject.Properties['count'].Value
if ($changed.source -ne "1" -or $changed.child -ne "2") { throw "the child run did not record the branch's count" }
# A redirected manifest is UTF-16 in Windows PowerShell 5.1; verify must accept it.
ocura-oss manifest > retained.manifest
Assert-Step "manifest"
ocura-oss verify --against retained.manifest
Assert-Step "verify --against"
ocura-oss demo --root (Join-Path $work "ocura-oss-demo")
Assert-Step "demo"
Write-Output "quickstart passed in $work"

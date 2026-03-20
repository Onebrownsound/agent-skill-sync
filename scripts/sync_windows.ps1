param(
    [switch]$Check,
    [switch]$Clean,
    [switch]$Pull,
    [switch]$NoBackup,
    [string]$Rollback,
    [ValidateSet("shared", "codex", "claude")]
    [string]$Bucket,
    [string[]]$Target
)

$argsList = @("scripts/sync_skills.py", "--host", "windows")

if ($Pull) {
    $argsList += "--pull"
}

if ($Rollback) {
    $argsList += "--rollback"
    $argsList += $Rollback
}

if ($Check) {
    $argsList += "--check"
} else {
    $argsList += "--apply"
}

if ($Clean) {
    $argsList += "--clean"
}

if ($NoBackup) {
    $argsList += "--no-backup"
}

if ($Bucket) {
    $argsList += "--bucket"
    $argsList += $Bucket
}

foreach ($item in $Target) {
    $argsList += "--target"
    $argsList += $item
}

python @argsList

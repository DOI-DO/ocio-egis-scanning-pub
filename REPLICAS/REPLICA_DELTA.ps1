$base = "C:\Users\tUser\OneDrive - OTHER_ORG\Documents\PTERODACTYL_ORG"
$output = Join-Path $base "MEGA_README.md"

$files = @(
    "CREDITS\readme.md",
    "DEPCRECATED_CONTENT\readme.md",
    "DUPLICATE_DATA\readme.md",
    "EMPTY_FEATURE_SERVICES_INVENTORY\readme.md",
    "FEATURE_SERVICES\readme.md",
    "FIELD_NAMES\readme.md",
    "GROUPS\readme.md",
    "HOSTING_SVR_DATASTORE\readme.md",
    "ITEMS\readme.md",
    "LICENSE\readme.md",
    "LOG_DUMP\readme.md",
    "PORTAL_CLEAN_UP\readme.md",
    "PORTAL_ORPHAN_FEATURE_SERVICES\readme.md",
    "readme.md",
    "RELATED_ITEMS\readme.md",
    "REPLICAS\readme.md",
    "SCHEDULED_TASKS\readme.md",
    "USERS\readme.md",
    "UTILITIES\readme.md",
    "WEB_MAPS\readme.md",
    "FEATURE_CLASS_NAMES\readme.md",
    "EMBEDDED_CREDENTIALS\readme.md",
    "PUBLIC_DATA_COLLECTION\readme.md",
    "ROLES\readme.md",
    "ACCOUNT_MANAGEMENT_AUDIT\readme.md"
) | ForEach-Object { Join-Path $base $_ }

$sb = [System.Text.StringBuilder]::new()
[void]$sb.AppendLine("# PTERODACTYL_ORG - Combined README")
[void]$sb.AppendLine()
[void]$sb.AppendLine("_Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')_")
[void]$sb.AppendLine()

# Table of contents
[void]$sb.AppendLine("## Table of Contents")
[void]$sb.AppendLine()
foreach ($file in $files) {
    $section = Split-Path (Split-Path $file -Parent) -Leaf
    if ($file -eq (Join-Path $base "readme.md")) { $section = "PTERODACTYL_ORG (root)" }
    $anchor = ($section.ToLower() -replace '[^a-z0-9 -]', '') -replace ' ', '-'
    [void]$sb.AppendLine("- [$section](#$anchor)")
}
[void]$sb.AppendLine()

foreach ($file in $files) {
    $section = Split-Path (Split-Path $file -Parent) -Leaf
    if ($file -eq (Join-Path $base "readme.md")) { $section = "PTERODACTYL_ORG (root)" }

    [void]$sb.AppendLine("---")
    [void]$sb.AppendLine()
    [void]$sb.AppendLine("## $section")
    [void]$sb.AppendLine()

    if (Test-Path -LiteralPath $file) {
        $content = Get-Content -LiteralPath $file -Raw
        [void]$sb.AppendLine($content.TrimEnd())
    } else {
        [void]$sb.AppendLine("> **WARNING:** File not found: ``$file``")
        Write-Warning "File not found: $file"
    }
    [void]$sb.AppendLine()
}

$sb.ToString() | Set-Content -LiteralPath $output -Encoding UTF8
Write-Host "Mega-readme written to: $output"
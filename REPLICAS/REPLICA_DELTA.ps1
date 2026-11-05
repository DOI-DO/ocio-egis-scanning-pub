#requires -Version 5.1

<#
.SYNOPSIS
    Compares Feature Service and SDE replica inventories.

.DESCRIPTION
    Performs a bidirectional comparison of normalized replica_id values.

    MATCHED:
        Present in both inventory files.

    ORPHANED_IN_FS:
        Present in the Feature Service inventory only.

    ORPHANED_IN_SDE:
        Present in the SDE inventory only.

    "Orphaned" means absent from the other supplied inventory.
    It does not establish the cause or prove that deletion is appropriate.

    Input paths may include surrounding single or double quotes.
    OutputPath may be an existing directory, a new directory, or a CSV path.

    Directory output receives a timestamped filename.
    An explicit CSV output path is overwritten if it already exists.

.PARAMETER FsInventoryPath
    Feature Service inventory CSV.

.PARAMETER SdeInventoryPath
    SDE inventory CSV.

.PARAMETER OutputPath
    Output directory or complete CSV filename.

.EXAMPLE
    .\REPLICA_DELTA.ps1 `
        -FsInventoryPath "C:\Temp\fs_inventory.csv" `
        -SdeInventoryPath "C:\Temp\sde_inventory.csv" `
        -OutputPath "C:\Temp"

.EXAMPLE
    .\REPLICA_DELTA.ps1 `
        -FsInventoryPath "C:\Temp\fs_inventory.csv" `
        -SdeInventoryPath "C:\Temp\sde_inventory.csv" `
        -OutputPath "C:\Temp\replica_comparison.csv"
#>

[CmdletBinding()]
param(
    [Parameter(
        Mandatory = $true,
        HelpMessage = 'Path to the Feature Service inventory CSV'
    )]
    [ValidateNotNullOrEmpty()]
    [string]$FsInventoryPath,

    [Parameter(
        Mandatory = $true,
        HelpMessage = 'Path to the SDE inventory CSV'
    )]
    [ValidateNotNullOrEmpty()]
    [string]$SdeInventoryPath,

    [Parameter(
        Mandatory = $true,
        HelpMessage = 'Output directory or complete CSV filename'
    )]
    [ValidateNotNullOrEmpty()]
    [string]$OutputPath
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function ConvertTo-CleanPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $clean = $Value.Trim()

    # Interactive parameter input can retain surrounding quotation marks.
    while ($clean.Length -ge 2) {
        $first = $clean.Substring(0, 1)
        $last = $clean.Substring($clean.Length - 1, 1)

        if (
            (($first -eq '"') -and ($last -eq '"')) -or
            (($first -eq "'") -and ($last -eq "'"))
        ) {
            $clean = $clean.Substring(1, $clean.Length - 2).Trim()
        }
        else {
            break
        }
    }

    $clean = [Environment]::ExpandEnvironmentVariables($clean)

    if ([string]::IsNullOrWhiteSpace($clean)) {
        throw 'A supplied path is empty after removing surrounding quotes.'
    }

    return $clean
}

function Resolve-InputCsvPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $clean = ConvertTo-CleanPath -Value $Value

    if (-not (Test-Path -LiteralPath $clean -PathType Leaf)) {
        throw (
            "{0} inventory file not found: {1}" -f $Label, $clean
        )
    }

    $item = Get-Item -LiteralPath $clean

    if ($item.PSProvider.Name -ne 'FileSystem') {
        throw "$Label inventory must be a filesystem file."
    }

    return $item.FullName
}

function Resolve-OutputCsvPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $clean = ConvertTo-CleanPath -Value $Value
    $isDirectory = $false

    if (Test-Path -LiteralPath $clean) {
        $item = Get-Item -LiteralPath $clean

        if ($item.PSProvider.Name -ne 'FileSystem') {
            throw 'OutputPath must be a filesystem path.'
        }

        $isDirectory = $item.PSIsContainer

        if (
            (-not $isDirectory) -and
            ([IO.Path]::GetExtension($clean) -ine '.csv')
        ) {
            throw 'An existing output file must have a .csv extension.'
        }
    }
    else {
        # A nonexistent path without an extension is treated as a directory.
        $extension = [IO.Path]::GetExtension($clean)

        if (
            $clean.EndsWith('\') -or
            $clean.EndsWith('/') -or
            [string]::IsNullOrWhiteSpace($extension)
        ) {
            $isDirectory = $true
        }
        elseif ($extension -ine '.csv') {
            throw 'Specify an output directory or a filename ending in .csv.'
        }
    }

    if ($isDirectory) {
        if (-not (Test-Path -LiteralPath $clean -PathType Container)) {
            $directoryPath =
                $ExecutionContext.SessionState.Path.
                GetUnresolvedProviderPathFromPSPath($clean)

            [void][IO.Directory]::CreateDirectory($directoryPath)
        }

        $directory = Get-Item -LiteralPath $clean
        $filename = 'REPLICA_DELTA_{0}.csv' -f (
            Get-Date -Format 'yyyyMMdd_HHmmss_fff'
        )

        return Join-Path -Path $directory.FullName -ChildPath $filename
    }

    $fullPath =
        $ExecutionContext.SessionState.Path.
        GetUnresolvedProviderPathFromPSPath($clean)

    $parent = [IO.Path]::GetDirectoryName($fullPath)

    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        [void][IO.Directory]::CreateDirectory($parent)
    }

    return $fullPath
}

function Resolve-ReplicaIdColumn {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$Rows,

        [Parameter(Mandatory = $true)]
        [string]$FileLabel
    )

    if ($Rows.Count -eq 0) {
        throw (
            "The {0} file contains no data rows. " +
            'Comparison stopped to avoid treating an empty export as evidence ' +
            'that every replica is missing.'
        ) -f $FileLabel
    }

    $candidates = @(
        'replica_id',
        'replicaid',
        'replica id',
        'replica_guid',
        'guid'
    )

    $headers = @($Rows[0].PSObject.Properties.Name)

    foreach ($candidate in $candidates) {
        foreach ($header in $headers) {
            if ($header.Trim() -ieq $candidate) {
                return $header
            }
        }
    }

    throw (
        "Could not locate a replica ID column in the {0} file. " +
        "Found columns: {1}"
    ) -f $FileLabel, ($headers -join ', ')
}

function ConvertTo-NormalizedReplicaId {
    param(
        [AllowNull()]
        [AllowEmptyString()]
        [string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ''
    }

    $text = $Value.Trim()
    $text = $text.Trim([char[]]@([char]34, [char]39)).Trim()

    if ([string]::IsNullOrWhiteSpace($text)) {
        return ''
    }

    $parsedGuid = [Guid]::Empty

    if ([Guid]::TryParse($text, [ref]$parsedGuid)) {
        return $parsedGuid.ToString('D').ToUpperInvariant()
    }

    return $text.ToUpperInvariant()
}

function Build-ReplicaLookup {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Rows,

        [Parameter(Mandatory = $true)]
        [string]$IdColumn
    )

    $lookup = @{}
    $blankCount = 0

    foreach ($row in $Rows) {
        $id = ConvertTo-NormalizedReplicaId -Value (
            [string]$row.$IdColumn
        )

        if ([string]::IsNullOrWhiteSpace($id)) {
            $blankCount++
            continue
        }

        if (-not $lookup.ContainsKey($id)) {
            $lookup[$id] = New-Object System.Collections.ArrayList
        }

        [void]$lookup[$id].Add($row)
    }

    return [pscustomobject]@{
        Lookup     = $lookup
        BlankCount = $blankCount
    }
}

function Get-RowValue {
    param(
        [AllowNull()]
        [object]$Row,

        [Parameter(Mandatory = $true)]
        [string[]]$CandidateNames
    )

    if ($null -eq $Row) {
        return ''
    }

    foreach ($candidate in $CandidateNames) {
        foreach ($property in $Row.PSObject.Properties) {
            if ($property.Name.Trim() -ieq $candidate) {
                $value = [string]$property.Value

                if (-not [string]::IsNullOrWhiteSpace($value)) {
                    return $value.Trim()
                }
            }
        }
    }

    return ''
}

function Get-ServiceName {
    param(
        [AllowNull()]
        [object]$Row
    )

    $name = Get-RowValue -Row $Row -CandidateNames @(
        'service_name',
        'service'
    )

    if (-not [string]::IsNullOrWhiteSpace($name)) {
        return $name
    }

    $url = Get-RowValue -Row $Row -CandidateNames @(
        'service_url',
        'fs_server_url',
        'location'
    )

    if (
        $url -match
        '(?i)/rest/services/(?<Service>.+?)/(?:FeatureServer|MapServer)(?:/|[?#]|$)'
    ) {
        return [Uri]::UnescapeDataString($Matches['Service'])
    }

    return ''
}

# Resolve and validate input paths before importing either file.
$FsInventoryPath = Resolve-InputCsvPath `
    -Value $FsInventoryPath `
    -Label 'Feature Service'

$SdeInventoryPath = Resolve-InputCsvPath `
    -Value $SdeInventoryPath `
    -Label 'SDE'

Write-Host "Reading Feature Service inventory: $FsInventoryPath" `
    -ForegroundColor Cyan
$fsRows = @(Import-Csv -LiteralPath $FsInventoryPath -Encoding UTF8)

Write-Host "Reading SDE inventory: $SdeInventoryPath" `
    -ForegroundColor Cyan
$sdeRows = @(Import-Csv -LiteralPath $SdeInventoryPath -Encoding UTF8)

Write-Host ('  FS rows loaded : {0}' -f $fsRows.Count)
Write-Host ('  SDE rows loaded: {0}' -f $sdeRows.Count)

$fsIdColumn = Resolve-ReplicaIdColumn `
    -Rows $fsRows `
    -FileLabel 'Feature Service'

$sdeIdColumn = Resolve-ReplicaIdColumn `
    -Rows $sdeRows `
    -FileLabel 'SDE'

Write-Host ("  FS ID column  : '{0}'" -f $fsIdColumn)
Write-Host ("  SDE ID column : '{0}'" -f $sdeIdColumn)

$fsData = Build-ReplicaLookup -Rows $fsRows -IdColumn $fsIdColumn
$sdeData = Build-ReplicaLookup -Rows $sdeRows -IdColumn $sdeIdColumn

$fsLookup = $fsData.Lookup
$sdeLookup = $sdeData.Lookup

if ($fsData.BlankCount -gt 0) {
    Write-Warning (
        '{0} FS row(s) had blank replica IDs and were skipped.' -f
        $fsData.BlankCount
    )
}

if ($sdeData.BlankCount -gt 0) {
    Write-Warning (
        '{0} SDE row(s) had blank replica IDs and were skipped.' -f
        $sdeData.BlankCount
    )
}

$allIds = @(
    @($fsLookup.Keys) + @($sdeLookup.Keys) |
        Sort-Object -Unique
)

if ($allIds.Count -eq 0) {
    throw 'Neither inventory contains a nonblank replica ID. No report written.'
}

$OutputPath = Resolve-OutputCsvPath -Value $OutputPath

if (
    [string]::Equals(
        $OutputPath,
        $FsInventoryPath,
        [StringComparison]::OrdinalIgnoreCase
    ) -or
    [string]::Equals(
        $OutputPath,
        $SdeInventoryPath,
        [StringComparison]::OrdinalIgnoreCase
    )
) {
    throw 'OutputPath cannot be the same as either input CSV.'
}

$replicaNameCandidates = @('replica_name', 'name')
$serviceUrlCandidates = @('service_url', 'fs_server_url', 'location')
$ownerCandidates = @('service_owner', 'replica_owner', 'owner')
$dateCandidates = @('date_created', 'creation_date')
$syncCandidates = @(
    'last_sync_date',
    'fs_last_sync_date',
    'lastReceive',
    'lastSend'
)

$results = @(
    foreach ($id in $allIds) {
        $inFs = $fsLookup.ContainsKey($id)
        $inSde = $sdeLookup.ContainsKey($id)

        $fsRow = $null
        $sdeRow = $null
        $fsCount = 0
        $sdeCount = 0

        if ($inFs) {
            $fsRow = $fsLookup[$id][0]
            $fsCount = $fsLookup[$id].Count
        }

        if ($inSde) {
            $sdeRow = $sdeLookup[$id][0]
            $sdeCount = $sdeLookup[$id].Count
        }

        if ($inFs -and $inSde) {
            $status = 'MATCHED'
            $reason = 'Normalized replica_id is present in both inventories.'
        }
        elseif ($inFs) {
            $status = 'ORPHANED_IN_FS'
            $reason = (
                'Normalized replica_id is present in the supplied FS ' +
                'inventory and absent from the supplied SDE inventory. ' +
                'The inventory comparison alone does not establish why.'
            )
        }
        else {
            $status = 'ORPHANED_IN_SDE'
            $reason = (
                'Normalized replica_id is present in the supplied SDE ' +
                'inventory and absent from the supplied FS inventory. ' +
                'The inventory comparison alone does not establish why.'
            )
        }

        $duplicateNotes = @()

        if ($fsCount -gt 1) {
            $duplicateNotes += "Repeated ID in FS ($fsCount rows)."
        }

        if ($sdeCount -gt 1) {
            $duplicateNotes += "Repeated ID in SDE ($sdeCount rows)."
        }

        if ($duplicateNotes.Count -gt 0) {
            $duplicateNotes += (
                'Context fields show the first row from each inventory.'
            )
        }

        [pscustomobject][ordered]@{
            replica_id = $id
            status = $status
            present_in_FS = $inFs
            present_in_SDE = $inSde

            fs_replica_name = Get-RowValue `
                -Row $fsRow -CandidateNames $replicaNameCandidates
            sde_replica_name = Get-RowValue `
                -Row $sdeRow -CandidateNames $replicaNameCandidates

            fs_service_name = Get-ServiceName -Row $fsRow
            sde_service_name = Get-ServiceName -Row $sdeRow

            fs_service_url = Get-RowValue `
                -Row $fsRow -CandidateNames $serviceUrlCandidates
            sde_service_url = Get-RowValue `
                -Row $sdeRow -CandidateNames $serviceUrlCandidates

            fs_owner = Get-RowValue `
                -Row $fsRow -CandidateNames $ownerCandidates
            sde_owner = Get-RowValue `
                -Row $sdeRow -CandidateNames $ownerCandidates

            fs_date_created = Get-RowValue `
                -Row $fsRow -CandidateNames $dateCandidates
            sde_date_created = Get-RowValue `
                -Row $sdeRow -CandidateNames $dateCandidates

            fs_last_sync = Get-RowValue `
                -Row $fsRow -CandidateNames $syncCandidates
            sde_last_sync = Get-RowValue `
                -Row $sdeRow -CandidateNames $syncCandidates

            sde_last_receive = Get-RowValue `
                -Row $sdeRow -CandidateNames @('lastReceive')
            sde_last_send = Get-RowValue `
                -Row $sdeRow -CandidateNames @('lastSend')

            fs_duplicate_count = $fsCount
            sde_duplicate_count = $sdeCount
            duplicate_note = $duplicateNotes -join ' '
            reason = $reason
        }
    }
)

$results |
    Export-Csv -LiteralPath $OutputPath `
        -NoTypeInformation -Encoding UTF8

$matched = @($results | Where-Object { $_.status -eq 'MATCHED' })
$orphanedFs = @(
    $results | Where-Object { $_.status -eq 'ORPHANED_IN_FS' }
)
$orphanedSde = @(
    $results | Where-Object { $_.status -eq 'ORPHANED_IN_SDE' }
)

Write-Host ''
Write-Host '================ REPLICA COMPARISON SUMMARY ================' `
    -ForegroundColor Yellow
Write-Host ('  FS input rows                  : {0}' -f $fsRows.Count)
Write-Host ('  SDE input rows                 : {0}' -f $sdeRows.Count)
Write-Host ('  FS blank IDs skipped           : {0}' -f $fsData.BlankCount)
Write-Host ('  SDE blank IDs skipped          : {0}' -f $sdeData.BlankCount)
Write-Host ('  Unique FS IDs                  : {0}' -f $fsLookup.Count)
Write-Host ('  Unique SDE IDs                 : {0}' -f $sdeLookup.Count)
Write-Host ('  Unique IDs across both files   : {0}' -f $allIds.Count)
Write-Host ('  Matched                        : {0}' -f $matched.Count) `
    -ForegroundColor Green
Write-Host ('  FS only / absent from SDE      : {0}' -f $orphanedFs.Count) `
    -ForegroundColor Yellow
Write-Host ('  SDE only / absent from FS      : {0}' -f $orphanedSde.Count) `
    -ForegroundColor Yellow
Write-Host '============================================================' `
    -ForegroundColor Yellow

if ($orphanedFs.Count -gt 0) {
    Write-Host ''
    Write-Host 'FS-only IDs: first 25 shown; all are in the output CSV.' `
        -ForegroundColor Yellow

    $orphanedFs |
        Select-Object -First 25 -Property `
            replica_id, fs_service_name, fs_owner |
        Format-Table -AutoSize |
        Out-String -Width 240 |
        Write-Host
}

if ($orphanedSde.Count -gt 0) {
    Write-Host ''
    Write-Host 'SDE-only IDs: first 25 shown; all are in the output CSV.' `
        -ForegroundColor Yellow

    $orphanedSde |
        Select-Object -First 25 -Property `
            replica_id, sde_service_name, sde_owner |
        Format-Table -AutoSize |
        Out-String -Width 240 |
        Write-Host
}

Write-Host ''
Write-Host (
    'Unmatched IDs indicate differences between these inventory snapshots, ' +
    'not confirmed deletion candidates.'
) -ForegroundColor Yellow

Write-Host ''
Write-Host "Comparison written to: $OutputPath" -ForegroundColor Green
Write-Host 'Done.' -ForegroundColor Cyan
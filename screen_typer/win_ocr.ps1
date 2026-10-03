# Bridge to the built-in Windows OCR engine (Windows.Media.Ocr).
# Usage: powershell -NoProfile -File win_ocr.ps1 -ImagePath <png> -Lang zh-Hant-TW
# Output: UTF-8 JSON  { "ok": true, "lang": "...", "lines": [ { text, words } ] }
# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads .ps1 as ANSI
# unless it has a BOM, so non-ASCII text here would break parsing.
param(
    [Parameter(Mandatory = $true)][string]$ImagePath,
    [string]$Lang = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Write-Fail($msg) {
    $o = [ordered]@{ ok = $false; error = "$msg"; lines = @() }
    [Console]::Out.Write(($o | ConvertTo-Json -Depth 4 -Compress))
    exit 0
}

try {
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation.UniversalApiContract, ContentType = WindowsRuntime]
    $null = [Windows.Storage.StorageFile, Windows.Foundation.UniversalApiContract, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation.UniversalApiContract, ContentType = WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Foundation.UniversalApiContract, ContentType = WindowsRuntime]
    Add-Type -AssemblyName System.Runtime.WindowsRuntime

    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        })[0]

    function Await($op, $type) {
        $asTask = $asTaskGeneric.MakeGenericMethod($type)
        $task = $asTask.Invoke($null, @($op))
        $task.Wait(-1) | Out-Null
        $task.Result
    }
}
catch {
    Write-Fail "WINRT_INIT_FAILED: $($_.Exception.Message)"
}

# -Lang '?'  ->  just list the installed OCR languages
if ($Lang -eq "?") {
    $tags = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag })
    [Console]::Out.Write((([ordered]@{ ok = $true; languages = $tags }) | ConvertTo-Json -Depth 4 -Compress))
    exit 0
}

try {
    if ([string]::IsNullOrWhiteSpace($Lang)) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    }
    else {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Lang))
    }
    if ($null -eq $engine) { Write-Fail "NO_ENGINE_FOR_LANG: $Lang" }

    $full = [System.IO.Path]::GetFullPath($ImagePath)
    if (-not (Test-Path -LiteralPath $full)) { Write-Fail "IMAGE_NOT_FOUND: $full" }

    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

    $lines = @()
    foreach ($line in $result.Lines) {
        $words = @($line.Words | ForEach-Object {
                [ordered]@{
                    t = $_.Text
                    x = [int]$_.BoundingRect.X
                    y = [int]$_.BoundingRect.Y
                    w = [int]$_.BoundingRect.Width
                    h = [int]$_.BoundingRect.Height
                }
            })
        $lines += , [ordered]@{ text = $line.Text; words = $words }
    }
    $out = [ordered]@{ ok = $true; lang = $engine.RecognizerLanguage.LanguageTag; lines = $lines }
    [Console]::Out.Write(($out | ConvertTo-Json -Depth 6 -Compress))
}
catch {
    Write-Fail "OCR_FAILED: $($_.Exception.Message)"
}

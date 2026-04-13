$ErrorActionPreference = "Stop"

function New-Utf8Text {
  param([byte[]]$Bytes)
  return [System.Text.Encoding]::UTF8.GetString($Bytes)
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $root "release"
$build = Join-Path $root "build"

$folderLabel = New-Utf8Text @(0xE6,0x96,0x87,0xE4,0xBB,0xB6,0xE5,0xA4,0xB9,0xE7,0x89,0x88)
$singleLabel = New-Utf8Text @(0xE5,0x8D,0x95,0xE6,0x96,0x87,0xE4,0xBB,0xB6,0xE7,0x89,0x88)
$senderLabel = New-Utf8Text @(0xE5,0x8F,0x91,0xE5,0xB0,0x84,0xE7,0xAB,0xAF)
$receiverLabel = New-Utf8Text @(0xE6,0x8E,0xA5,0xE6,0x94,0xB6,0xE7,0xAB,0xAF)
$senderSingleName = "{0}-{1}.exe" -f $senderLabel, $singleLabel
$receiverSingleName = "{0}-{1}.exe" -f $receiverLabel, $singleLabel

$folderDist = Join-Path $dist $folderLabel
$singleDist = Join-Path $dist $singleLabel
$zipPath = Join-Path $root ("Ether-Stream-{0}.zip" -f $folderLabel)
$singleZipPath = Join-Path $root ("Ether-Stream-{0}.zip" -f $singleLabel)

if (Test-Path -LiteralPath $dist) { Remove-Item -LiteralPath $dist -Recurse -Force }
if (Test-Path -LiteralPath $build) { Remove-Item -LiteralPath $build -Recurse -Force }
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
if (Test-Path -LiteralPath $singleZipPath) { Remove-Item -LiteralPath $singleZipPath -Force }

python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --exclude-module matplotlib `
  --exclude-module IPython `
  --exclude-module jupyter_client `
  --exclude-module matplotlib_inline `
  --name ether-stream-sender `
  --distpath $folderDist `
  --workpath $build `
  --specpath $build `
  gui_sender.py

python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --exclude-module matplotlib `
  --exclude-module IPython `
  --exclude-module jupyter_client `
  --exclude-module matplotlib_inline `
  --name ether-stream-receiver `
  --distpath $folderDist `
  --workpath $build `
  --specpath $build `
  gui_receiver.py

python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onefile `
  --exclude-module matplotlib `
  --exclude-module IPython `
  --exclude-module jupyter_client `
  --exclude-module matplotlib_inline `
  --name ether-stream-sender-single `
  --distpath $singleDist `
  --workpath $build `
  --specpath $build `
  gui_sender.py

python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onefile `
  --exclude-module matplotlib `
  --exclude-module IPython `
  --exclude-module jupyter_client `
  --exclude-module matplotlib_inline `
  --name ether-stream-receiver-single `
  --distpath $singleDist `
  --workpath $build `
  --specpath $build `
  gui_receiver.py

$npcapRoots = @(
  $root,
  "I:\rj\QQ"
)

$npCapSource = $null
foreach ($searchRoot in $npcapRoots) {
  if (-not (Test-Path -LiteralPath $searchRoot)) { continue }
  $candidate = Get-ChildItem -LiteralPath $searchRoot -Recurse -Filter "npcap-1.79.exe" -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if ($candidate) {
    $npCapSource = $candidate.FullName
    break
  }
}

if ($npCapSource) {
  Copy-Item -LiteralPath $npCapSource -Destination (Join-Path $folderDist "npcap-1.79.exe") -Force
  Copy-Item -LiteralPath $npCapSource -Destination (Join-Path $singleDist "npcap-1.79.exe") -Force
}

$senderFolderPath = Join-Path $folderDist "ether-stream-sender"
$receiverFolderPath = Join-Path $folderDist "ether-stream-receiver"
Rename-Item -LiteralPath $senderFolderPath -NewName $senderLabel
Rename-Item -LiteralPath $receiverFolderPath -NewName $receiverLabel
Rename-Item -LiteralPath (Join-Path (Join-Path $folderDist $senderLabel) "ether-stream-sender.exe") -NewName ("{0}.exe" -f $senderLabel)
Rename-Item -LiteralPath (Join-Path (Join-Path $folderDist $receiverLabel) "ether-stream-receiver.exe") -NewName ("{0}.exe" -f $receiverLabel)
Rename-Item -LiteralPath (Join-Path $singleDist "ether-stream-sender-single.exe") -NewName $senderSingleName
Rename-Item -LiteralPath (Join-Path $singleDist "ether-stream-receiver-single.exe") -NewName $receiverSingleName

Copy-Item -LiteralPath (Join-Path $root "README.md") -Destination (Join-Path $folderDist "README.md") -Force
Copy-Item -LiteralPath (Join-Path $root "README.md") -Destination (Join-Path $singleDist "README.md") -Force

$usageDoc = Get-ChildItem -LiteralPath $root -File -Filter "*.txt" -ErrorAction SilentlyContinue |
  Sort-Object Length -Descending |
  Select-Object -First 1
if ($usageDoc) {
  Copy-Item -LiteralPath $usageDoc.FullName -Destination (Join-Path $folderDist $usageDoc.Name) -Force
  Copy-Item -LiteralPath $usageDoc.FullName -Destination (Join-Path $singleDist $usageDoc.Name) -Force
}

Get-ChildItem -LiteralPath $folderDist -Recurse -File -Filter "*_gui_config.json" -ErrorAction SilentlyContinue |
  Remove-Item -Force

Get-ChildItem -LiteralPath $singleDist -Recurse -File -Filter "*_gui_config.json" -ErrorAction SilentlyContinue |
  Remove-Item -Force

Compress-Archive -Path (Join-Path $folderDist "*") -DestinationPath $zipPath -Force
Compress-Archive -Path (Join-Path $singleDist "*") -DestinationPath $singleZipPath -Force

Write-Host "release ready:" $dist
Write-Host "folder release ready:" $folderDist
Write-Host "single-file ready:" $singleDist
Write-Host "folder zip ready:" $zipPath
Write-Host "single-file zip ready:" $singleZipPath

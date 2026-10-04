param([string[]]$Frames = @("foundations","components","home","home-not-ready","home-light","welcome-1","welcome-2","welcome-3","models","words","tools","settings","overlays","home-compact"))
# Renders each design frame alone with headless Edge (no window is shown). Output: shots\<frame>.png
$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$html = Join-Path $dir "rflow-ui.html"
$out = Join-Path $dir "shots"
New-Item -ItemType Directory -Force $out | Out-Null
$sizes = @{ "foundations" = "1568,1700"; "components" = "1568,1180"; "overlays" = "1568,1028"; "home-compact" = "908,668" }
foreach ($f in $Frames) {
    $size = if ($sizes.ContainsKey($f)) { $sizes[$f] } else { "1128,828" }
    $url = "file:///" + ($html -replace '\\', '/') + "#only=$f"
    $png = Join-Path $out "$f.png"
    & $edge --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 --virtual-time-budget=6000 "--window-size=$size" "--screenshot=$png" $url 2>$null | Out-Null
    if (Test-Path $png) { Write-Output "ok  $f" } else { Write-Output "FAIL $f" }
}

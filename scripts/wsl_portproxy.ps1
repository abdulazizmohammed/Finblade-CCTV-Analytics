<#
Expose the FinBlade API running inside WSL to the LAN, so a phone (Traccar
Client, or web/tracker.html) can post GPS positions to it.

    Run as Administrator:
      powershell -ExecutionPolicy Bypass -File scripts\wsl_portproxy.ps1
      powershell -ExecutionPolicy Bypass -File scripts\wsl_portproxy.ps1 -Remove

WSL2 sits behind NAT: Windows reaches it as localhost automatically, the rest
of the network does not. This adds a portproxy from every Windows interface on
:8000 to the WSL address, plus a firewall rule. THE WSL ADDRESS CHANGES ON
REBOOT, so re-run this after one; it replaces any previous rule for the port.

The phone then uses  http://<this machine's Wi-Fi address>:8000/...
#>
param([int]$Port = 8000, [string]$Distro = "Ubuntu-22.04", [switch]$Remove)

$wslIp = (wsl -d $Distro -- hostname -I).Trim().Split(' ')[0]
if (-not $wslIp) { Write-Error "could not read the WSL address"; exit 1 }

netsh interface portproxy delete v4tov4 listenport=$Port listenaddress=0.0.0.0 | Out-Null
netsh advfirewall firewall delete rule name="FinBlade API $Port" | Out-Null
if ($Remove) { Write-Host "removed forward and firewall rule for :$Port"; exit 0 }

netsh interface portproxy add v4tov4 listenport=$Port listenaddress=0.0.0.0 connectport=$Port connectaddress=$wslIp
netsh advfirewall firewall add rule name="FinBlade API $Port" dir=in action=allow protocol=TCP localport=$Port | Out-Null
Write-Host "forwarding 0.0.0.0:$Port -> $wslIp`:$Port"
netsh interface portproxy show v4tov4
$lan = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notlike '*WSL*' -and $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.*' } | Select-Object -First 1 -ExpandProperty IPAddress
Write-Host ""
Write-Host "Phone -> Traccar Client server URL:  http://$lan`:$Port/api/v1/trackers/ingest"
Write-Host "Phone -> browser reporter:           http://$lan`:$Port/web/tracker.html"

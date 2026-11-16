# Install and use NAVIQRA on a phone or tablet

## Before installing

Get the approved HTTPS address from the station operator. A phone needs access to the station server through the authorized Wi-Fi network or an approved private remote-access service. The loopback address 127.0.0.1 is for the device running NAVIQRA.

1. **iPhone and iPad.** Open the address in Safari. Open the Share menu and choose Add to Home Screen. Enable Open as Web App if offered, then tap Add. Open the NAVIQRA icon and sign in.
2. **Android.** Open the address in Chrome. From Chrome’s menu, choose Install app or Add to Home screen when offered. Open NAVIQRA from the new icon and sign in.
3. **Windows.** Double-click Setup NAVIQRA.cmd to start the server. Use the launcher’s Open running station button to reopen the dashboard. On an approved HTTPS address, Chrome or Edge may offer a web-app install action. A browser shortcut on another device does not start the Windows server.

The app requires a connection for login, live cameras, clips, reports, sales, inventory and changes to station records. An offline navigation displays a generic connection notice. The worker does not put authenticated pages, clips, exports or API responses into browser Cache Storage. The current dashboard hides station data when the browser reports loss of connection or the station heartbeat fails. A downloaded file remains subject to the device’s storage and access policy.

## For the station network administrator

Keep the NAVIQRA listener on loopback. Terminate TLS at a reverse proxy restricted to the authorized private station interface and approved client network. Configure a private DNS hostname that matches a certificate trusted by each client device. Set NAVIQRA_TRUSTED_HOSTS to the exact HTTPS hostname plus localhost and 127.0.0.1 for the local startup check. Set NAVIQRA_HTTPS=1 so session cookies require TLS. The container accepts both settings through Compose. The Windows launcher inherits them from the process environment. Keep the station secret stable and use the HTTPS hostname for operator sign-in.

The following Caddy example is illustrative and requires the station owner’s approval, private DNS and review of local firewall rules. Replace the example values. The example neither runs automatically nor provisions an Internet endpoint.

```caddyfile
{
    auto_https disable_redirects
}
https://naviqra.station.example {
    bind 10.10.20.15
    tls internal
    reverse_proxy 127.0.0.1:8080
}
```

A private certificate authority needs an approved trust installation on client devices. Distribute only the public root certificate through station-managed device tooling. Keep the CA private key on the proxy. Verify the browser shows a valid HTTPS origin before using the PWA. For off-site access use an approved station VPN or private access gateway with managed authentication, device authorization and access logging. Obtain permission before enabling remote access.

[Install a web app on iPhone](https://support.apple.com/guide/iphone/open-as-web-app-iphea86e5236/ios) · [Caddy TLS configuration](https://caddyserver.com/docs/caddyfile/directives/tls)

# Exposing the admin panel behind TLS

Default and recommended: don't. Bind stays `127.0.0.1`, access is an ssh tunnel:

```
ssh -L 8765:localhost:8765 user@vps
```

then open `http://localhost:8765/` locally. Nothing below is needed for that path.

## If you actually need it reachable off-box

`admin/server.py` refuses a non-loopback `--host` unless you pass
`--i-know-this-is-exposed`. Even then it is still plain HTTP: **without a TLS
reverse proxy in front, the admin password and the session cookie cross the
network in cleartext** — anyone on the path (open wifi, a compromised router,
the VPS provider's network) can read the password out of the login POST body.
Do not skip the proxy.

Once a proxy terminates TLS in front of the app:

1. Keep `admin/server.py` bound to `127.0.0.1` (the proxy is the only thing
   that should reach it directly).
2. Set `ADMIN_BEHIND_TLS=1` in `~/.hermes/.env` — this does two things:
   - the session cookie gets the `Secure` flag (see `admin/server.py::_make_cookie`)
   - `X-Forwarded-For` is trusted (last hop only) for rate-limiting by real client IP
3. Restart `health-admin.service` after changing `.env`.

## Caddy

```
admin.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

Caddy issues and renews the certificate automatically. That's the whole config.

## nginx

```nginx
server {
    listen 443 ssl;
    server_name admin.example.com;

    ssl_certificate     /etc/letsencrypt/live/admin.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/admin.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name admin.example.com;
    return 301 https://$host$request_uri;
}
```

`proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` is what makes
"last hop only" trustworthy: nginx appends the real client IP itself rather
than passing through whatever a client claimed.

## Still recommend the tunnel

This panel serves personal medical data to a single operator. A reverse proxy
adds a public DNS name, a cert to renew, and one more thing that can
misconfigure itself into an open door. The ssh tunnel has none of that attack
surface. Use the proxy only if you genuinely need access from a device you
can't `ssh -L` from.

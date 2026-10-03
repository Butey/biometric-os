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

## Login: password plus a Telegram code

Reachable from the internet, the panel is meant to run behind the TLS proxy
with `ADMIN_BEHIND_TLS=1` (see above). Login is two steps, with no bypass: the
same two steps apply to a request that arrives straight on `127.0.0.1` through
an ssh tunnel.

1. The shared admin password. A correct password does not log anyone in; it
   makes the panel send a 6-digit one-time code to every id in
   `HEALTH_ADMIN_IDS` through the Telegram Bot API (the panel does this itself,
   the bot process does not need to be running).
2. The code, valid for 5 minutes. 5 wrong codes or expiry mean starting over
   from the password. A new code is not sent while a live one is pending.

Needed in `~/.hermes/.env`:

- `TELEGRAM_BOT_TOKEN` - the same bot token the bot uses
- `HEALTH_ADMIN_IDS` - comma-separated Telegram ids that receive the codes

If no ids are configured, the token is missing, or Telegram is unreachable, the
login fails closed with a "could not send the code" message. There is
deliberately no way in through the web in that case: no backup codes, no
"remember this device". Recovery is on the server itself (fix `.env`, restart
`health-admin.service`, or use the ssh tunnel once Telegram works again - the
tunnel does not skip the code either).

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

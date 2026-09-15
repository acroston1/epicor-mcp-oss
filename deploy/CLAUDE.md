# Deployment examples

Purpose: provide portable reverse-proxy examples for an operator-hosted server.

## Entry points
- `nginx.conf.example`: HTTPS origin forwarding to loopback port 8015.
- [Root README](../README.md): token, public-origin, and connection configuration.
- [Optional SSO](../docs/microsoft-sso.md): OAuth origins and redirect requirements.

## Invariants
- Use placeholder hostnames/certificate paths; operators provide DNS, TLS, and supervision.
- Keep streaming compatible: disable buffering and retain request headers.
- Public origin must match server settings and client connections.
- Examples never install or change system configuration automatically.

## Gotchas
- The example uses a dedicated hostname and root origin to avoid prefix rewriting.
- Health reports runtime modes; it does not validate Epicor credentials.
- Guide pairs remain identical and <=60 lines.

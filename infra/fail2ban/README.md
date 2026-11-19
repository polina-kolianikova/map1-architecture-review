# Fail2ban for Review Access

The review server writes Caddy access logs to:

```text
/var/log/map1-turkey/caddy/map1-turkey.log
```

Install the jail on the host:

```bash
sudo mkdir -p /etc/fail2ban/filter.d /etc/fail2ban/jail.d /var/log/map1-turkey/caddy
sudo cp infra/fail2ban/filter.d/map1-caddy-auth.conf /etc/fail2ban/filter.d/
sudo cp infra/fail2ban/jail.d/map1-caddy-auth.local /etc/fail2ban/jail.d/
sudo systemctl restart fail2ban
sudo fail2ban-client status map1-caddy-auth
```

Check the filter against the live log after the review site has received a few
failed Basic Auth attempts:

```bash
sudo fail2ban-regex /var/log/map1-turkey/caddy/map1-turkey.log \
  /etc/fail2ban/filter.d/map1-caddy-auth.conf
```

Check that bans affect Docker-published ports on the host:

```bash
sudo fail2ban-client set map1-caddy-auth banip <HOST_ADDRESS>
sudo fail2ban-client status map1-caddy-auth
sudo fail2ban-client set map1-caddy-auth unbanip <HOST_ADDRESS>
```

If the host firewall routes Docker traffic through a separate chain, add the ban
action to the host firewall or `DOCKER-USER` chain and repeat the check from an
external network.

Only ports 80 and 443 should be reachable from outside the server. Backend,
frontend, and PostgreSQL ports are for local host checks and must remain blocked
by the server firewall.

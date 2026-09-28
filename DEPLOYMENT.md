# Production deployment

This runbook targets a Debian/Ubuntu VPS with `systemd`, Nginx and a domain.
The application process is deliberately not exposed to the Internet: Nginx is
the only public entry point and proxies every request to the authenticated
Python server.

## Filesystem contract

Use separate locations for immutable code, datasets and secrets:

```text
/opt/regional-map-framework/          Git checkout, owned by root
/srv/regional-map-deployment/         registry, region contracts and data links
/srv/regional-map-data/<region>/      externally delivered GeoJSON snapshots
/var/lib/regional-map-framework/      credentials and persistent private state
/etc/regional-map-framework.env       production environment, mode 0600
```

Do not serve `/opt/regional-map-framework` or `/srv/regional-map-data` with an
Nginx `root`, `alias` or `try_files` directive. All traffic must pass through
`serve.py`; this is what enforces authentication and the public-file allowlist.

The external deployment package rooted at `/srv/regional-map-deployment` must
contain the small regional contract files (`registry.json`,
`regions/<id>/region.json`, `SOURCES.md` and any reviewed pipeline
configuration) as well as links to the separately transported datasets. A
directory containing GeoJSON alone is insufficient because the server exposes
only files declared by registered region contracts.

## 1. Prepare the host

Install Python, Git, Nginx and an ACME client using the distribution packages.
Create a locked, unprivileged service account:

```bash
sudo useradd --system --home /var/lib/regional-map-framework \
  --shell /usr/sbin/nologin regional-map
sudo install -d -o root -g root -m 0755 /opt/regional-map-framework
sudo install -d -o root -g regional-map -m 0750 /srv/regional-map-data
sudo install -d -o root -g regional-map -m 0750 \
  /srv/regional-map-deployment
sudo install -d -o regional-map -g regional-map -m 0700 \
  /var/lib/regional-map-framework
```

The commands assume that SSH is already restricted to administrator keys and
that the VPS firewall exposes only SSH, HTTP and HTTPS.

## 2. Install and verify code

Clone the repository into `/opt/regional-map-framework`, check out an explicit
reviewed commit or release tag, and leave the checkout owned by `root:root`.
Never deploy a mutable branch without recording its commit SHA.

```bash
cd /opt/regional-map-framework
python3 manage.py verify-core
python3 -m unittest discover -s tests -v
python3 pipeline_core/selftest.py
python3 manage.py validate-all --allow-missing-data
```

The web server itself uses only the Python standard library. Install
`pipeline_core/requirements-lock.txt` in a dedicated virtual environment only
when the deployed regional pipelines actually require those dependencies.

## 3. Install the regional deployment package

Build the contract bundle on the trusted source machine only after every region
has passed `doctor` and has been promoted to `production`:

```bash
RMF_CONTENT_ROOT=/path/to/current/content \
  python3 manage.py bundle-build \
  --bundle-dir deployment/release/contracts
python3 manage.py bundle-verify \
  --bundle-dir deployment/release/contracts \
  --data-dir deployment/release/data
```

The first command refuses draft or invalid regions and never overwrites an
existing destination. The second command verifies every contract and every
separately stored dataset against `deployment-manifest.json`. Transfer the
contract bundle and the data directory independently; neither belongs in Git.

On the VPS, verify the uploaded files before installing them:

```bash
cd /opt/regional-map-framework
python3 manage.py bundle-verify \
  --bundle-dir /path/to/uploaded/contracts \
  --data-dir /path/to/uploaded/data
```

The uploaded bundle and data tree must be regular directories without symlinks.
This prevents verification from following paths outside the transferred release;
the active per-region `data` symlink is created later by `attach-data`.

Install the verified contents of `contracts/content/` under
`/srv/regional-map-deployment`, not into the Git checkout. Install each large
snapshot from `data/<region>/` into the matching region-specific directory
under `/srv/regional-map-data`; never combine files from different regions in
one directory. Preserve the uploaded `deployment-manifest.json` with the
release records. The resulting active content tree starts as follows:

```text
/srv/regional-map-deployment/
├── registry.json
└── regions/
    └── my_region/
        ├── region.json
        ├── SOURCES.md
        └── data -> /srv/regional-map-data/my_region
```

After copying, make contracts and snapshots readable by the service without
making them writable by it:

```bash
sudo chown -R root:regional-map \
  /srv/regional-map-deployment /srv/regional-map-data
sudo chmod -R u=rwX,g=rX,o= \
  /srv/regional-map-deployment /srv/regional-map-data
```

For an external-data region, attach its snapshot and validate it:

```bash
cd /opt/regional-map-framework
sudo env RMF_CONTENT_ROOT=/srv/regional-map-deployment \
  RMF_RUNTIME_ROOT=/var/lib/regional-map-framework/regions \
  python3 manage.py attach-data my_region \
  --data-dir /srv/regional-map-data/my_region --attach-mode symlink
sudo env RMF_CONTENT_ROOT=/srv/regional-map-deployment \
  RMF_RUNTIME_ROOT=/var/lib/regional-map-framework/regions \
  python3 manage.py validate my_region
sudo env RMF_CONTENT_ROOT=/srv/regional-map-deployment \
  RMF_RUNTIME_ROOT=/var/lib/regional-map-framework/regions \
  python3 manage.py doctor my_region
```

Repeat this for every ID in `registry.json`, then run:

```bash
sudo env RMF_CONTENT_ROOT=/srv/regional-map-deployment \
  RMF_RUNTIME_ROOT=/var/lib/regional-map-framework/regions \
  python3 manage.py deployment-check
```

`deployment-check` requires a non-empty valid registry, a valid default region,
`production` lifecycle and complete validated data for every region. It must
finish successfully before the service is started. Keep a
checksum manifest and a backup of every snapshot that cannot be regenerated.
Make the generated private runtime state accessible to the service account:

```bash
sudo chown -R regional-map:regional-map /var/lib/regional-map-framework
sudo chmod -R go-rwx /var/lib/regional-map-framework
```

## 4. Configure runtime and credentials

Install the environment template and keep it readable only by root and the
service manager:

```bash
sudo install -o root -g root -m 0600 \
  deploy/regional-map-framework.env.example \
  /etc/regional-map-framework.env
```

The secure defaults disable browser-triggered regeneration. Do not change
`RMF_COOKIE_SECURE=1` in production. `RMF_TRUST_PROXY=1` is safe only while the
backend listens on loopback and Nginx overwrites `X-Forwarded-For`.

Create the first administrator credential interactively. The password is not
placed in shell history:

```bash
cd /opt/regional-map-framework
sudo -u regional-map env \
  RMF_AUTH_FILE=/var/lib/regional-map-framework/users.json \
  python3 manage.py auth-set-user map_admin
```

Back up `users.json` to a private encrypted location. Replacing or losing that
file invalidates sessions and removes the configured accounts.

## 5. Install the service

```bash
sudo install -o root -g root -m 0644 \
  deploy/systemd/regional-map-framework.service \
  /etc/systemd/system/regional-map-framework.service
sudo systemctl daemon-reload
sudo systemctl enable --now regional-map-framework
sudo systemctl status regional-map-framework
curl --fail --silent http://127.0.0.1:8000/healthz
```

The default unit makes the checkout and datasets read-only. This is intentional
for the initial deployment where `RMF_ALLOW_REGENERATION=0`. Before every start
it runs `deployment-check` as the same unprivileged service account; invalid or
unreadable content prevents the HTTP server from starting.

## 6. Configure Nginx and HTTPS

Replace `maps.example.com` in the template with the real DNS name, install it,
and test Nginx before reloading:

```bash
sudo install -o root -g root -m 0644 \
  deploy/nginx/regional-map-framework.conf \
  /etc/nginx/sites-available/regional-map-framework.conf
sudo ln -s /etc/nginx/sites-available/regional-map-framework.conf \
  /etc/nginx/sites-enabled/regional-map-framework.conf
sudo nginx -t
sudo systemctl reload nginx
```

Obtain a certificate and let the ACME client add the HTTPS listener and an HTTP
to HTTPS redirect. For Certbot on Debian/Ubuntu this is normally:

```bash
sudo certbot --nginx -d maps.example.com --redirect
sudo nginx -t
sudo systemctl reload nginx
```

The initial port-80 configuration exists for certificate issuance only. Do not
log in until HTTPS and the redirect are active: cookies are intentionally marked
`Secure` and authentication over plain HTTP is not a supported production mode.

## 7. Acceptance checks

Run these checks after HTTPS is enabled:

```bash
curl --fail --silent https://maps.example.com/healthz
curl --head https://maps.example.com/
sudo journalctl -u regional-map-framework --since today
sudo systemctl is-enabled regional-map-framework
sudo systemctl is-active regional-map-framework
```

Expected behavior:

- `/healthz` returns HTTP 200 without authentication;
- `/` redirects an anonymous client to `/login`;
- source files such as `/serve.py`, `/.git/config`, `.runtime` and undeclared
  data return HTTP 404 after login;
- the login cookie contains `Secure`, `HttpOnly` and `SameSite=Strict`;
- the Python process listens only on `127.0.0.1:8000`;
- every registered region passes `manage.py validate` and opens after login.

## Updates and rollback

Before every update, record the active commit SHA and back up credentials and
non-regenerable data. Fetch the new release, verify it and its regions, then
restart the service:

```bash
cd /opt/regional-map-framework
python3 manage.py verify-core
python3 -m unittest discover -s tests -v
sudo env RMF_CONTENT_ROOT=/srv/regional-map-deployment \
  RMF_RUNTIME_ROOT=/var/lib/regional-map-framework/regions \
  python3 manage.py deployment-check
sudo systemctl restart regional-map-framework
curl --fail --silent http://127.0.0.1:8000/healthz
```

Rollback means checking out the previously recorded release, restoring the
matching regional contracts/snapshot if their schema changed, validating, and
restarting the service. Never use `git reset --hard` against a directory that
contains uncommitted regional contracts.

## Enabling regeneration later

Keep regeneration disabled until the selected region has a reviewed
`pipeline/regeneration.json`, all its regenerable sources work from the VPS, and
its data directory is a normal local directory rather than a symlink.
`pipeline_core/regeneration.py` atomically swaps that directory and writes
runtime state below `RMF_RUNTIME_ROOT`; therefore the default hardened
unit intentionally blocks it. Enabling regeneration requires an explicit
systemd override with narrowly scoped `ReadWritePaths` for exactly those region
directories, followed by a security review and a failure/rollback test.

## Backups and monitoring

At minimum, back up `/var/lib/regional-map-framework/users.json`, the deployment
package's contracts and every non-regenerable dataset. System logs are available
through `journalctl`; configure the host's normal journal retention policy and
monitor both the public `/healthz` endpoint and certificate renewal. A 200 from
`/healthz` proves the process is alive, not that every regional dataset is valid,
so scheduled monitoring should also run `python3 manage.py deployment-check`
with the production `RMF_CONTENT_ROOT` and `RMF_RUNTIME_ROOT`.

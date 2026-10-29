# Installing on TrueNAS SCALE

Three ways, easiest first.

---

## 1. Install via YAML (recommended)

TrueNAS SCALE 24.10+ can install any Docker Compose file directly. This is the
supported "custom app" path and needs no shell access.

**Step 1 — create a dataset for the app's state**

Datasets → Add Dataset, e.g. `apps/ktn-enclosure-manager`. Then, from a shell
(or Shell in the UI), make it writable by the app user:

```bash
chown -R 1000:1000 /mnt/YOURPOOL/apps/ktn-enclosure-manager
```

The container refuses to start without this and tells you so — accounts and the
audit log would otherwise silently fail to persist.

**Step 2 — find your SES device and enclosure id**

```bash
lsscsi -g | grep enclosu        # -> [1:0:15:0] enclosu EMC  ESES Enclosure ... /dev/sg16
cat /sys/class/enclosure/*/id   # -> 0x5006048004a54c3e
```

**Step 3 — install**

Apps → Discover Apps → **⋮** → **Install via YAML**. Paste
[`truenas/install-via-yaml.yaml`](../truenas/install-via-yaml.yaml) and edit the
lines marked `### EDIT ###`:

| Edit | What |
|---|---|
| `/mnt/YOURPOOL/...` | the dataset from step 1 |
| `/dev/sg16` | your SES device from step 2 |
| `KTN_TRUENAS_URL` / `KTN_TRUENAS_API_KEY` | optional, for pool/vdev/SMART data |

**Step 4 — open it and create your administrator**

`http://<truenas>:8420`. First run has **no account and no default password**;
you create the administrator in the browser.

> The app card gets a working **Web UI** button, like a catalog app. That comes
> from the `x-portals` block at the top of the YAML. TrueNAS reads that key back
> out of the rendered compose config, so it works for custom apps even though
> nothing in the Install-via-YAML dialog mentions it. If you change the port,
> change it in `x-portals` too.
>
> **Leave `host: 0.0.0.0` alone.** It looks like a placeholder and is not one.
> The stored portal really is `http://0.0.0.0:8420/` — `midclt call app.query`
> shows it that way — but the WebUI rewrites it on click: `openPortalLink()`
> replaces a `0.0.0.0` hostname with `window.location.hostname`, bracketing it
> if it is IPv6. The button therefore follows whatever address you browsed to,
> including a hostname or a VPN address, which a hardcoded value cannot do.
> Catalog apps show the same `0.0.0.0` in the API for the same reason.
>
> The portal is recorded when the app is **installed**. Adding or correcting
> `x-portals` on an app that already exists does not backfill it: for a custom
> app, `app.update` re-renders the compose file but never recomputes the
> portal, so the Apps list keeps showing the old link. You do not have to
> reinstall — run
> [`truenas/fix-custom-app-metadata.py`](../truenas/fix-custom-app-metadata.py)
> (below), which recomputes the portal and notes from the rendered compose with
> the middleware's own helper and prints the resulting URL.
>
> An earlier revision of this document said `midclt call app.metadata.generate`
> alone refreshes it (observed on 25.10.5). On 25.10.7 the middleware source
> shows that call only copies each app's stored metadata into the cache; it
> does not recompute the portal. The script does both.
>
> This is worth knowing generally: a custom app whose `x-portals` names
> `0.0.0.0` shows a **Web UI** button that cannot work, and several published
> Install-via-YAML recipes have exactly that.
>
> **The card icon, title and displayed version CAN be fixed — from the shell,
> not the YAML.** Every custom app initially shows the generic TrueNAS cube,
> the title "Custom App" and version `1.0.0_custom`, because TrueNAS builds a
> custom app's metadata from a hardcoded stub
> (`catalog_reader.custom_app.get_version_details`) and no `x-` extension can
> reach it. An earlier revision of this document said writing the stored
> metadata was ignored; that was wrong — the edit only appears to be ignored
> until `app.metadata.generate` is run, because the WebUI reads a collective
> cache rather than the per-app file. The display fields live in
> `/mnt/.ix-apps/app_configs/<app>/metadata.yaml`; for a custom app
> `app.update` does not touch that file at all (so the edits survive, and so
> does a stale portal), and regenerating the cache makes the WebUI pick them
> up. Verified on 25.10.6: title, icon and displayed
> version all render. The details page additionally reads
> `metadata.version` and `metadata.app_version`; `metadata.version` names the
> `versions/<dir>` the middleware reads config from, so fixing it means
> renaming that directory in the same operation - verified durable through a
> full `app.update` cycle afterwards. Run
> [`truenas/fix-custom-app-metadata.py`](../truenas/fix-custom-app-metadata.py)
> on the appliance to apply all of it; it takes a full tar backup first and
> prints the rollback. Re-run after a reinstall, after updating to a new
> image tag, or after changing `x-portals` (normal app updates do not revert
> the title or icon).

### Privileges this asks for

One device node — your enclosure — and nothing else. The container is not
privileged, mounts no host path besides its own data dataset (plus, only with
the optional HTTPS below, two read-only certificate files), keeps Docker's
default AppArmor profile, and drops every capability except the two used to
drop the web process to uid 1000. See [SECURITY.md](../SECURITY.md).

### Serving HTTPS with the TrueNAS certificate

Optional, off by default (since 1.6.0). If the TrueNAS UI already has a
certificate for a name - for example a Let's Encrypt one from its ACME
integration - KTN can serve that same certificate on its own port, so
`https://<that name>:8420` opens with no warning. Read the
[blast-radius note in SECURITY.md](../SECURITY.md#serving-https-with-the-truenas-certificate-opt-in)
first: the key you share is the TrueNAS UI's own.

**1. Find the certificate's paths** (they follow the certificate's *name*):

```bash
midclt call certificate.query '[]' '{"select":["name","certificate_path","privatekey_path"]}'
```

Use the entry your UI uses (System → General → GUI → certificate), typically
`/etc/certificates/<name>.crt` and `/etc/certificates/<name>.key`. Check both
exist (`ls -l`) before editing the YAML.

**2. Edit the app's YAML** (Apps → the app → Edit). Uncomment the TLS block in
[`install-via-yaml.yaml`](../truenas/install-via-yaml.yaml):

```yaml
    volumes:
      - type: bind
        source: /etc/certificates/<name>.crt
        target: /tls/fullchain.pem
        read_only: true
        bind: {create_host_path: false}
      - type: bind
        source: /etc/certificates/<name>.key
        target: /tls/privkey.pem
        read_only: true
        bind: {create_host_path: false}

    environment:
      KTN_TLS_ENABLED: "true"
      KTN_TLS_CERTFILE: /tls/fullchain.pem
      KTN_TLS_KEYFILE: /tls/privkey.pem
```

> **Use this long syntax only.** The short form
> (`- /etc/certificates/<name>.key:/tls/privkey.pem:ro`) creates a missing
> source path as a *directory*. If the key were ever absent when the container
> is created, Docker would make a directory at `/etc/certificates/<name>.key`,
> and TrueNAS's own certificate writer would then fail on it - breaking the
> appliance UI's TLS, not just this app's. `create_host_path: false` makes a
> missing file a clear container-create error instead.

Keep the `/run/ktn` tmpfs line: the key copy lives there, in RAM, and the app
refuses to start without it. Change the portal to the name on the certificate
so the **Web UI** button opens it over https (an integer port, never quoted -
a schema failure silently drops every portal):

```yaml
x-portals:
  - name: Web UI
    scheme: https
    host: <name on the certificate>
    port: 8420
    path: /
```

`host: 0.0.0.0` would follow the address you browsed the TrueNAS UI by, which
is wrong here: the certificate covers only its name. Setting
`KTN_ALLOWED_HOSTS` to that name and the NAS's IP address is a good idea too.

**3. Refresh the button.** `app.update` does not recompute a custom app's
portal, so run the fix script afterwards; it prints the resulting portal URL,
which should now be `https://<name>:8420/`:

```bash
python3 fix-custom-app-metadata.py
```

**Using it:**

- Open it with the **Web UI** button, or type `https://<name>:8420`. A browser
  that has opened the TrueNAS UI by name before rewrites `<name>:8420` to
  https by itself (HSTS); one that **never** has, typing the address without
  `https://`, gets an empty-response error ("didn't send any data"). There is
  no http-to-https redirect: the port speaks TLS only.
- **Update any `http://` bookmarks, dashboards and monitors** to `https://`.
  Plain HTTP to the port gets an empty reply once TLS is on.
- **If the certificate ever expires**, the browser refuses the name with no
  click-through (HSTS). Use `https://<NAS-IP>:8420` instead - the name
  mismatch there is an ordinary warning you can accept, because HSTS does not
  apply to IP addresses. `/healthz` reports `tls.days_left`, and the log warns
  from 14 days out.

**Renewal is automatic.** TrueNAS rewrites the files in place; KTN checks the
certificate's content hourly and, on a real change, restarts itself (a few
seconds of downtime) so the entrypoint can install the new pair. That relies
on the app's restart policy, `unless-stopped` in the YAML. Confirm the logs
show `renewed TLS certificate detected` after a renewal, or restart the app
by hand.

**If it will not start**, the log has an `ERROR: TLS ...` line naming the
file. It never falls back to plain HTTP. To get back in immediately, set
`KTN_TLS_ENABLED: "false"` (the two read-only mounts are harmless when off),
set the portal back to `http`, rerun the fix script, and use
`http://<NAS-IP>:8420` - by name, HSTS keeps upgrading it to https.

---

## 2. Docker Compose from a shell

```bash
git clone https://github.com/MechanicalCoderX/ktn-enclosure-manager.git
cd ktn-enclosure-manager
cp .env.example .env && chmod 600 .env
$EDITOR .env
mkdir -p data && chown -R 1000:1000 data
docker compose up -d
```

This builds from source rather than pulling the published image. Use it if you
want to modify the app or avoid the registry.

---

## 3. Official TrueNAS catalog

Not available yet. The app source in a format suitable for submission to the
[truenas/apps](https://github.com/truenas/apps) community train lives in
[`truenas/catalog-app/`](../truenas/catalog-app/), along with
[SUBMITTING.md](../truenas/catalog-app/SUBMITTING.md).

The one unusual thing this app asks a catalog reviewer to accept is the SES
device node itself (`/dev/sg*` at `rw`). Since 1.1.0 there is no writable
`/sys`, no AppArmor relaxation and no extra capability: the LED is driven by a
SCSI command on that node under the default profile. The permission floor was
measured exactly in 1.5.2 — every telemetry read runs on a read-only open, and
the `w` exists for one SEND DIAGNOSTIC — see [SECURITY.md](../SECURITY.md).
Method 1 works today regardless.

---

## Verifying the install

```bash
docker logs ktn-enclosure-manager | grep "startup complete"
# -> startup complete: 1 enclosure(s): EMC ESES Enclosure 0x...
```

If it reports `0 enclosure(s)`, check `/sys/class/enclosure` is non-empty
**on the host**. If it is empty, the HBA is not passed through or is not in
IT/HBA mode — nothing this app can fix. No `/sys` volume belongs in the YAML:
Docker's own read-only `/sys` already exposes `/sys/class/enclosure`, which is
all the app reads.

If the Chassis tab says unavailable, the SES device node is missing from the
`devices:` block or points at the wrong `/dev/sgN`. Telemetry runs on a
read-only open, so even a `:r` grant is enough for it; confirm the same way
the app reads:

```bash
docker exec ktn-enclosure-manager sg_ses --readonly -p cf /dev/sg16 | head -3
```

If only Identify returns a permission error, the device is granted `:r` — a
valid monitoring-only deployment where exactly that error is expected. The LED
write is the one operation that needs the `w`; grant `:rw` to enable it (see
[SECURITY.md](../SECURITY.md)).

## Upgrading

Install via YAML: change the image tag and redeploy, or use the app's Update
button when a new tag is published.

```bash
# compose install
git pull && docker compose build && docker compose up -d
```

Application state lives entirely in the mounted dataset, so upgrades and
reinstalls do not lose accounts, IDENT history, or the audit log.

## Uninstalling

Delete the app in the TrueNAS UI, or `docker compose down`. Then remove the
dataset if you no longer want the audit history. The application creates no host
files, no systemd units, and no TrueNAS configuration, so nothing else is left
behind.

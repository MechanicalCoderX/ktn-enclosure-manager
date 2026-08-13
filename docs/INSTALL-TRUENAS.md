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

### Enabling the Identify button

By default the app is fully functional except Identify, which needs to write
`/sys`. Docker's default AppArmor profile denies that regardless of
capabilities. To enable it, uncomment this line in the YAML and redeploy:

```yaml
      - apparmor=unconfined
```

Please read [SECURITY.md](../SECURITY.md) first — it explains exactly what that
does and does not expose, and why the alternative (a custom AppArmor profile)
is not the default.

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

Be aware this app needs a writable `/sys`, an SES device node, and (for
Identify) a relaxed AppArmor profile. Those are unusual for a catalog app and
may well be grounds for rejection. Method 1 works today regardless.

---

## Verifying the install

```bash
docker logs ktn-enclosure-manager | grep "startup complete"
# -> startup complete: 1 enclosure(s): EMC ESES Enclosure 0x...
```

If it reports `0 enclosure(s)`:

- Check `/sys/class/enclosure` is non-empty **on the host**. If it is empty, the
  HBA is not passed through or is not in IT/HBA mode — nothing this app can fix.
- Check the `/sys:/sys:rw` volume is present in your YAML.

If the Chassis tab says unavailable, the SES device node is missing or was
granted `:r` instead of `:rw`:

```bash
docker exec ktn-enclosure-manager sg_ses -p cf /dev/sg16 | head -3
```

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

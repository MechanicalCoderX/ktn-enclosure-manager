# Test fixtures

Captured from a real EMC KTN-STL3 on TrueNAS SCALE 25.10.5, then **sanitized**:
disk serials, WWNs/SAS addresses, enclosure logical identifiers and the pool
name were replaced with synthetic values before publication.

The sanitization deliberately preserves every property the tests depend on, so
they are exactly as strong as they were against the original capture:

- the non-alphabetical slot→device mapping (slots 11–14 are `sdn, sdp, sdo, sdm`)
- the SES port address == block node WWN **+ 2** relationship
- serial and WWN uniqueness, formats and field widths
- the full SES element structure: 5 subenclosures, 26 type descriptors,
  134 elements, and the duplicate `Temp. Sensor B` label on type descriptors
  1 and 21 that forces `(type_index, element_index)` keying

| File | Contents |
|---|---|
| `ktn-stl3/sg_cf.txt` | `sg_ses -p cf` — configuration page |
| `ktn-stl3/sg_es.txt` | `sg_ses -p es` — enclosure status |
| `ktn-stl3/sg_aes.txt` | `sg_ses -p aes` — additional element status |
| `ktn-stl3/sg_join*.txt` | `sg_ses --join`, filtered and unfiltered |
| `ktn-stl3/sg_inq.txt` | SCSI inquiry |
| `ktn-stl3/sysfs_slots.txt` | per-slot sysfs attributes |
| `ktn-stl3/sysfs_blocks.txt` | per-disk sysfs identity attributes |
| `truenas/*.json` | filtered `disk.query`, `pool.query`, `disk.temperatures` |
| `sysfs_root/` | generated fake `/sys` tree — see below |

`sysfs_root/` is **generated**, not captured. Rebuild it after editing the
captured files:

```bash
python3 tests/build_sysfs_fixture.py
```

It writes `vpd_pg80` as real bytes including the 4-byte VPD header, so the
serial parser is exercised on the byte layout the kernel actually produces.

## synthetic/

Hand-written sg_ses output variants the KTN-STL3 cannot produce, used to pin
parser behaviour the real capture never exercises: a configuration page with
textless type descriptors (`sg_cf_textless.txt`), bays reported as plain
`Device slot` type 0x01 (`sg_cf_device_slot.txt`), and an AES page whose
device slot numbers are permuted/1-based relative to element indexes
(`sg_aes_permuted.txt`) — the case where Identify must translate, and where
an unmapped slot must be refused.

`sg_cf_cooling_codes.txt` / `sg_join_cooling_codes.txt` walk a Cooling type
descriptor through all eight SES-3 speed codes plus two failure-to-map forms
(no phrase printed, and a phrase outside the table). The KTN-STL3 has only
ever been observed at `Fan at highest speed` and `Fan at third lowest speed`,
so the other six codes exist nowhere in the capture.

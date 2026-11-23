# This folder is a snapshot of the TrueNAS catalog package

The package TrueNAS users install is **upstream**:
[truenas/apps `ix-dev/community/ktn-enclosure-manager`](https://github.com/truenas/apps/tree/master/ix-dev/community/ktn-enclosure-manager).
Upstream is the authority. Its maintainers reworked our 2026 submission, and truenasbot moves the image tag to each new release, pinned by digest.

This copy was taken from upstream commit `36668d1661bef712bbf9b200efdd6e70ae49d435` on 2026-11-23. That commit serves app version 1.6.5.

- `app.yaml`, `item.yaml`, `ix_values.yaml`, `questions.yaml`, `README.md` and `templates/` are verbatim copies from that commit.
- `SUBMITTING.md` and `SUBMISSION-TEXTS.md` are ours: the historical record of the submission (truenas/apps#5679, merged 2026-11-17).
- Nothing here is bumped by a release. To refresh it, copy the same files from a newer upstream commit and update the commit above. `tests/test_version_consistency.py` checks that the copy agrees with itself and is not ahead of the app.

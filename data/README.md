# Generated research assets

This directory is generated and then committed by GitHub Actions.

- `raw_tiles/`: exact source tile bytes retained for audit.
- `mosaics/`: exact-cell derivative PNGs produced only when every required tile is available.
- `manifests/`: cumulative tile ledger, per-run logs, and the reader-facing `manifest.json`.

A missing mosaic never means “no candidate exists.” It means the underlying source acquisition for that grid has not completed.

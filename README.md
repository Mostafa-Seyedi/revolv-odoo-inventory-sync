# Revolv Odoo Inventory Sync

Small external Python tool for a one-off CSV-to-Odoo inventory synchronization exercise.

## Input

The script expects `inventory.csv` with these columns:

- `material_sku`
- `quantity_change`
- `location`

`quantity_change` is treated as a delta. Example: current quantity 15 and change +5 gives target quantity 20.

## Configuration

Set these environment variables before running:

```powershell
$env:ODOO_URL="https://your-odoo.example.com"
$env:ODOO_DB="your_database"
$env:ODOO_USERNAME="integration-user@example.com"
$env:ODOO_PASSWORD="your_password_or_api_credential"
```

For the local test environment used during development, the values were set only in the local shell and were not stored in the script.

## Run safely

Dry run (default; no Odoo writes):

```powershell
python inventory_sync.py
```

Apply changes:

```powershell
python inventory_sync.py --apply
```

## Outputs

- `sync_results.csv`: row-by-row result (`DRY_RUN_OK`, `UPDATED`, `FAILED`)
- `inventory_sync.log`: technical log for troubleshooting

## Safety behavior

- Missing SKU/location -> row is logged as `FAILED` and skipped.
- Invalid quantity -> rejected before any write.
- Temporary network/API errors -> retried up to 3 times.
- Lot/serial-tracked products -> rejected for manual review because the CSV does not identify a lot/serial.
- Negative target stock -> rejected.
- Dry-run is the default; real writes require `-apply`.
- Successful writes are read back from Odoo and verified.

## Note

This is designed as a one-off migration tool. Because `quantity_change` is a delta, the same input file should not be applied twice.

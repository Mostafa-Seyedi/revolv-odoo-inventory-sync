"""CSV -> Odoo inventory synchronization tool for the Revolv Space take-home task.

Default mode is DRY RUN. Use --apply to make real inventory changes.
Required CSV columns: material_sku, quantity_change, location

Configuration is read from environment variables:
    ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD
"""

import argparse
import csv
import logging
import os
import socket
import time
import xmlrpc.client
from decimal import Decimal, InvalidOperation


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

CSV_FILE = "inventory.csv"
RESULT_FILE = "sync_results.csv"
LOG_FILE = "inventory_sync.log"

API_TIMEOUT_SECONDS = 10
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 1

REQUIRED_COLUMNS = {"material_sku", "quantity_change", "location"}


# -----------------------------------------------------------------------------
# XML-RPC transports with timeouts
# -----------------------------------------------------------------------------

class TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout=API_TIMEOUT_SECONDS):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


class TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout=API_TIMEOUT_SECONDS):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


def create_server_proxy(endpoint):
    transport = (
        TimeoutSafeTransport()
        if endpoint.startswith("https://")
        else TimeoutTransport()
    )
    return xmlrpc.client.ServerProxy(
        endpoint,
        transport=transport,
        allow_none=True,
    )


# -----------------------------------------------------------------------------
# Logging and retry helpers
# -----------------------------------------------------------------------------

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def call_with_retry(operation, description):
    """Retry temporary network/server errors, but not normal Odoo data errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return operation()
        except xmlrpc.client.ProtocolError as error:
            # 4xx errors are normally deterministic; do not retry them.
            if error.errcode < 500:
                raise
            logging.warning(
                "%s failed on attempt %s/%s: HTTP %s %s",
                description,
                attempt,
                MAX_RETRIES,
                error.errcode,
                error.errmsg,
            )
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as error:
            logging.warning(
                "%s failed on attempt %s/%s: %s",
                description,
                attempt,
                MAX_RETRIES,
                error,
            )

        if attempt == MAX_RETRIES:
            raise RuntimeError(
                f"{description} failed after {MAX_RETRIES} attempts"
            )

        delay = RETRY_DELAY_SECONDS * attempt
        logging.info("Retrying in %s second(s)...", delay)
        time.sleep(delay)


# -----------------------------------------------------------------------------
# Input and configuration validation
# -----------------------------------------------------------------------------

def validate_configuration():
    values = {
        "ODOO_URL": ODOO_URL,
        "ODOO_DB": ODOO_DB,
        "ODOO_USERNAME": ODOO_USERNAME,
        "ODOO_PASSWORD": ODOO_PASSWORD,
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing environment variables: " + ", ".join(missing)
        )


def validate_row(row):
    errors = []

    sku = (row.get("material_sku") or "").strip()
    quantity_raw = (row.get("quantity_change") or "").strip()
    location = (row.get("location") or "").strip()

    if not sku:
        errors.append("material_sku is missing")
    if not location:
        errors.append("location is missing")

    quantity = None
    if not quantity_raw:
        errors.append("quantity_change is missing")
    else:
        try:
            quantity = Decimal(quantity_raw)
        except InvalidOperation:
            errors.append(
                f"quantity_change '{quantity_raw}' is not a valid number"
            )

    return {
        "material_sku": sku,
        "quantity_change": quantity,
        "quantity_raw": quantity_raw,
        "location": location,
    }, errors


# -----------------------------------------------------------------------------
# Odoo connection and lookups
# -----------------------------------------------------------------------------

def connect_to_odoo():
    common = create_server_proxy(f"{ODOO_URL}/xmlrpc/2/common")

    uid = call_with_retry(
        lambda: common.authenticate(
            ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {}
        ),
        "Odoo authentication",
    )

    if not uid:
        raise RuntimeError("Odoo authentication failed")

    models = create_server_proxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def find_product(models, uid, sku):
    products = call_with_retry(
        lambda: models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "product.product",
            "search_read",
            [["default_code", "=", sku]],
            {
                "fields": ["id", "name", "default_code", "tracking"],
                "limit": 1,
            },
        ),
        f"Product lookup for SKU '{sku}'",
    )
    return products[0] if products else None


def find_location(models, uid, location_name):
    locations = call_with_retry(
        lambda: models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "stock.location",
            "search_read",
            [[
                ["complete_name", "=", location_name],
                ["usage", "=", "internal"],
            ]],
            {
                "fields": ["id", "name", "complete_name"],
                "limit": 1,
            },
        ),
        f"Location lookup for '{location_name}'",
    )
    return locations[0] if locations else None


def get_current_quantity(models, uid, product_id, location_id):
    quants = call_with_retry(
        lambda: models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "stock.quant",
            "search_read",
            [[
                ["product_id", "=", product_id],
                ["location_id", "=", location_id],
            ]],
            {"fields": ["quantity"]},
        ),
        f"Stock lookup for product {product_id} at location {location_id}",
    )

    total = Decimal("0")
    for quant in quants:
        total += Decimal(str(quant["quantity"]))
    return total


def find_quant(models, uid, product_id, location_id):
    quants = call_with_retry(
        lambda: models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "stock.quant",
            "search_read",
            [[
                ["product_id", "=", product_id],
                ["location_id", "=", location_id],
                ["lot_id", "=", False],
                ["package_id", "=", False],
                ["owner_id", "=", False],
            ]],
            {"fields": ["id", "quantity"], "limit": 1},
        ),
        "Stock quant lookup",
    )
    return quants[0] if quants else None


# -----------------------------------------------------------------------------
# Inventory write
# -----------------------------------------------------------------------------

def apply_inventory_quantity(
    models,
    uid,
    product_id,
    location_id,
    target_quantity,
):
    """Apply an absolute target quantity through Odoo's inventory mechanism."""
    if target_quantity < 0:
        raise ValueError("Proposed inventory quantity cannot be negative")

    quant = find_quant(models, uid, product_id, location_id)
    target = float(target_quantity)
    context = {"inventory_mode": True}

    if quant:
        quant_id = quant["id"]
        result = call_with_retry(
            lambda: models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "stock.quant",
                "write",
                [[quant_id], {"inventory_quantity_auto_apply": target}],
                {"context": context},
            ),
            f"Inventory update for quant {quant_id}",
        )
        if result is not True:
            raise RuntimeError("Odoo did not confirm the inventory update")
    else:
        quant_id = call_with_retry(
            lambda: models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "stock.quant",
                "create",
                [{
                    "product_id": product_id,
                    "location_id": location_id,
                    "inventory_quantity_auto_apply": target,
                }],
                {"context": context},
            ),
            "Creating inventory quantity",
        )
        if not quant_id:
            raise RuntimeError("Odoo did not create the inventory quantity")


# -----------------------------------------------------------------------------
# Result CSV helper
# -----------------------------------------------------------------------------

def write_result(
    writer,
    row_number,
    sku,
    location,
    quantity_change,
    current_quantity,
    proposed_quantity,
    final_quantity,
    status,
    message,
):
    writer.writerow({
        "row_number": row_number,
        "material_sku": sku,
        "location": location,
        "quantity_change": quantity_change,
        "current_quantity": current_quantity,
        "proposed_quantity": proposed_quantity,
        "final_quantity": final_quantity,
        "status": status,
        "message": message,
    })


# -----------------------------------------------------------------------------
# Main program
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Synchronize inventory CSV data with Odoo."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually apply inventory changes. Without this flag the script "
            "runs in safe dry-run mode."
        ),
    )
    args = parser.parse_args()
    apply_mode = args.apply

    setup_logging()
    mode = "APPLY" if apply_mode else "DRY RUN"
    logging.info("Starting inventory synchronization in %s mode", mode)

    try:
        validate_configuration()
        uid, models = connect_to_odoo()
    except Exception as error:
        logging.error("Could not start synchronization: %s", error)
        return

    logging.info("Connected to Odoo successfully as user ID %s", uid)

    try:
        input_file = open(CSV_FILE, mode="r", newline="", encoding="utf-8")
    except FileNotFoundError:
        logging.error("CSV file '%s' was not found", CSV_FILE)
        return

    success_count = 0
    failed_count = 0

    with input_file:
        reader = csv.DictReader(input_file)
        columns = set(reader.fieldnames or [])
        missing_columns = REQUIRED_COLUMNS - columns

        if missing_columns:
            logging.error(
                "Missing required columns: %s",
                ", ".join(sorted(missing_columns)),
            )
            return

        with open(
            RESULT_FILE,
            mode="w",
            newline="",
            encoding="utf-8",
        ) as result_file:
            fieldnames = [
                "row_number",
                "material_sku",
                "location",
                "quantity_change",
                "current_quantity",
                "proposed_quantity",
                "final_quantity",
                "status",
                "message",
            ]
            writer = csv.DictWriter(result_file, fieldnames=fieldnames)
            writer.writeheader()

            for row_number, row in enumerate(reader, start=2):
                normalized, errors = validate_row(row)
                sku = normalized["material_sku"]
                location_name = normalized["location"]
                quantity_change = normalized["quantity_change"]

                if errors:
                    failed_count += 1
                    message = "; ".join(errors)
                    logging.warning(
                        "Row %s failed CSV validation: %s",
                        row_number,
                        message,
                    )
                    write_result(
                        writer,
                        row_number,
                        sku,
                        location_name,
                        normalized["quantity_raw"],
                        "",
                        "",
                        "",
                        "FAILED",
                        message,
                    )
                    continue

                try:
                    product = find_product(models, uid, sku)
                    if not product:
                        failed_count += 1
                        message = f"SKU '{sku}' not found in Odoo"
                        logging.warning("Row %s: %s", row_number, message)
                        write_result(
                            writer, row_number, sku, location_name,
                            quantity_change, "", "", "", "FAILED", message
                        )
                        continue

                    if product["tracking"] != "none":
                        failed_count += 1
                        message = (
                            "Product uses lot/serial tracking and requires "
                            "manual review"
                        )
                        logging.warning("Row %s: %s", row_number, message)
                        write_result(
                            writer, row_number, sku, location_name,
                            quantity_change, "", "", "", "FAILED", message
                        )
                        continue

                    location = find_location(models, uid, location_name)
                    if not location:
                        failed_count += 1
                        message = f"Location '{location_name}' not found in Odoo"
                        logging.warning("Row %s: %s", row_number, message)
                        write_result(
                            writer, row_number, sku, location_name,
                            quantity_change, "", "", "", "FAILED", message
                        )
                        continue

                    current_quantity = get_current_quantity(
                        models, uid, product["id"], location["id"]
                    )
                    proposed_quantity = current_quantity + quantity_change

                    if proposed_quantity < 0:
                        failed_count += 1
                        message = "Quantity change would produce negative stock"
                        logging.warning("Row %s: %s", row_number, message)
                        write_result(
                            writer, row_number, sku, location_name,
                            quantity_change, current_quantity,
                            proposed_quantity, "", "FAILED", message
                        )
                        continue

                    if not apply_mode:
                        success_count += 1
                        message = (
                            f"Would change quantity from {current_quantity} "
                            f"to {proposed_quantity}"
                        )
                        logging.info(
                            "DRY RUN row %s: SKU=%s, location=%s, current=%s, "
                            "change=%s, proposed=%s",
                            row_number,
                            sku,
                            location_name,
                            current_quantity,
                            quantity_change,
                            proposed_quantity,
                        )
                        write_result(
                            writer, row_number, sku, location_name,
                            quantity_change, current_quantity,
                            proposed_quantity, "", "DRY_RUN_OK", message
                        )
                        continue

                    apply_inventory_quantity(
                        models,
                        uid,
                        product["id"],
                        location["id"],
                        proposed_quantity,
                    )

                    final_quantity = get_current_quantity(
                        models, uid, product["id"], location["id"]
                    )
                    tolerance = Decimal("0.000001")
                    if abs(final_quantity - proposed_quantity) > tolerance:
                        raise RuntimeError(
                            "Post-update verification failed: expected "
                            f"{proposed_quantity}, found {final_quantity}"
                        )

                    success_count += 1
                    message = (
                        f"Updated successfully from {current_quantity} "
                        f"to {final_quantity}"
                    )
                    logging.info(
                        "UPDATED row %s: SKU=%s, location=%s, old=%s, "
                        "change=%s, new=%s",
                        row_number,
                        sku,
                        location_name,
                        current_quantity,
                        quantity_change,
                        final_quantity,
                    )
                    write_result(
                        writer, row_number, sku, location_name,
                        quantity_change, current_quantity,
                        proposed_quantity, final_quantity, "UPDATED", message
                    )

                except xmlrpc.client.Fault as error:
                    failed_count += 1
                    message = f"Odoo rejected the operation: {error.faultString}"
                    logging.error(
                        "Row %s failed due to Odoo error: %s",
                        row_number,
                        error.faultString,
                    )
                    write_result(
                        writer, row_number, sku, location_name,
                        quantity_change, "", "", "", "FAILED", message
                    )
                except Exception as error:
                    failed_count += 1
                    message = f"Odoo/API error: {error}"
                    logging.error("Row %s failed: %s", row_number, error)
                    write_result(
                        writer, row_number, sku, location_name,
                        quantity_change, "", "", "", "FAILED", message
                    )

    logging.info(
        "Synchronization finished. Successful rows: %s, failed rows: %s",
        success_count,
        failed_count,
    )
    logging.info("Results saved to %s", RESULT_FILE)
    if not apply_mode:
        logging.info("DRY RUN ONLY. No Odoo inventory was modified.")


if __name__ == "__main__":
    main()

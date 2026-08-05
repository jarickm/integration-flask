# app.py
import sqlite3
import datetime
from flask import Flask, render_template, jsonify, request, flash

app = Flask(__name__)
app.secret_key = "netsuite_operations_secret_token"
DB_FILE = "advances_terminal.db"


def init_db():
    """Builds and verifies all three internal workflow tables in SQLite."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # 1. Customer advances tracking table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS advance_tasks (
            customer_id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'Unassigned',
            assigned_to TEXT DEFAULT 'Unassigned',
            priority TEXT DEFAULT 'Medium',
            notes TEXT DEFAULT '',
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 2. Fixed Asset physical verification status tracking table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fixed_asset_tasks (
            asset_id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'Unverified',
            custodian TEXT DEFAULT 'Unassigned',
            notes TEXT DEFAULT '',
            last_verified TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 3. Warehouse Item Receipts QC Verification tracking table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS warehouse_tasks (
            receipt_id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'Pending Inspection',
            verified_qty INTEGER DEFAULT 0,
            temp_recorded REAL DEFAULT 0.0,
            inspector TEXT DEFAULT 'Unassigned',
            notes TEXT DEFAULT '',
            last_inspected TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()

    # Self-healing check: Verify created tables and output to the terminal on startup
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()

    print("\n================ DATABASE INITIALIZATION ================")
    print(f"📁 Database File: {DB_FILE}")
    print(f"📊 Active Tables Found: {', '.join(tables)}")
    print("=========================================================\n")


# Initialize database schemas on startup
init_db()


# --- DATABASE HELPERS: CUSTOMER ADVANCES ---

def get_task_metadata():
    """Retrieves operational updates stored in the SQLite database for customer advances."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM advance_tasks")
    rows = cursor.fetchall()
    conn.close()
    return {row["customer_id"]: dict(row) for row in rows}


def save_task_metadata(customer_id, status, assigned_to, priority, notes):
    """Saves workflow status changes locally for customer advances."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO advance_tasks (customer_id, status, assigned_to, priority, notes, last_updated)
        VALUES (?, ?, ?, ?, ?, ?) 
        ON CONFLICT(customer_id) DO UPDATE SET
            status=excluded.status,
            assigned_to=excluded.assigned_to,
            priority=excluded.priority,
            notes=excluded.notes,
            last_updated=excluded.last_updated
    """, (customer_id, status, assigned_to, priority, notes, now))
    conn.commit()
    conn.close()


# --- DATABASE HELPERS: FIXED ASSETS ---

def get_asset_metadata():
    """Retrieves operational updates stored in the SQLite database for Fixed Assets."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM fixed_asset_tasks")
    rows = cursor.fetchall()
    conn.close()
    return {row["asset_id"]: dict(row) for row in rows}


def save_asset_metadata(asset_id, status, custodian, notes):
    """Saves operational verification details locally for Fixed Assets."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO fixed_asset_tasks (asset_id, status, custodian, notes, last_verified)
        VALUES (?, ?, ?, ?, ?) 
        ON CONFLICT(asset_id) DO UPDATE SET
            status=excluded.status,
            custodian=excluded.custodian,
            notes=excluded.notes,
            last_verified=excluded.last_verified
    """, (asset_id, status, custodian, notes, now))
    conn.commit()
    conn.close()


# --- DATABASE HELPERS: WAREHOUSE RECEIPTS ---

def get_warehouse_metadata():
    """Retrieves operational updates stored in the SQLite database for Inbound Warehouse Receipts."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM warehouse_tasks")
    rows = cursor.fetchall()
    conn.close()
    return {row["receipt_id"]: dict(row) for row in rows}


def save_warehouse_metadata(receipt_id, status, verified_qty, temp_recorded, inspector, notes):
    """Saves QC verification and temperature metrics locally for Inbound Receipts."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO warehouse_tasks (receipt_id, status, verified_qty, temp_recorded, inspector, notes, last_inspected)
        VALUES (?, ?, ?, ?, ?, ?, ?) 
        ON CONFLICT(receipt_id) DO UPDATE SET
            status=excluded.status,
            verified_qty=excluded.verified_qty,
            temp_recorded=excluded.temp_recorded,
            inspector=excluded.inspector,
            notes=excluded.notes,
            last_inspected=excluded.last_inspected
    """, (receipt_id, status, verified_qty, temp_recorded, inspector, notes, now))
    conn.commit()
    conn.close()


# --- APP ROUTING & CONTROLLERS ---

@app.route("/")
@app.route("/dashboard")
def dashboard():
    """Renders the dashboard workspace with customer advances."""
    from netsuite import get_netsuite_access_token, fetch_customer_advances

    token = get_netsuite_access_token()
    advances = fetch_customer_advances(token)

    task_metadata = get_task_metadata()
    enriched_advances = []
    total_amount = 0.0

    for adv in advances:
        cust_id = adv["id"]
        meta = task_metadata.get(cust_id, {
            "status": "Unassigned",
            "assigned_to": "Unassigned",
            "priority": "High" if adv["amount"] >= 150000 else "Medium",
            "notes": ""
        })

        enriched_item = {**adv, **meta}
        enriched_advances.append(enriched_item)
        total_amount += adv["amount"]

    customer_count = len(enriched_advances)
    avg_advance = (total_amount / customer_count) if customer_count > 0 else 0

    sorted_advances = sorted(enriched_advances, key=lambda x: x["amount"], reverse=True)

    # Slice collection to show the top 10 records for chart
    top_10_advances = sorted_advances[:10]
    chart_labels = [c["name"] for c in top_10_advances]
    chart_data = [c["amount"] for c in top_10_advances]

    unassigned_count = sum(1 for item in enriched_advances if item["status"] == "Unassigned")
    investigating_count = sum(1 for item in enriched_advances if item["status"] == "Investigating")
    is_mock_data = any(item.get("is_mock", False) for item in advances)

    return render_template(
        "index.html",
        advances=enriched_advances,
        total_amount=total_amount,
        customer_count=customer_count,
        avg_advance=avg_advance,
        unassigned_count=unassigned_count,
        investigating_count=investigating_count,
        chart_labels=chart_labels,
        chart_data=chart_data,
        is_mock_data=is_mock_data,
        active_page="dashboard"
    )


@app.route("/fixed-assets")
def fixed_assets():
    """Renders the physical fixed asset management ledger workspace."""
    from netsuite import get_netsuite_access_token, fetch_fixed_assets

    token = get_netsuite_access_token()
    raw_assets = fetch_fixed_assets(token)

    metadata = get_asset_metadata()
    enriched_assets = []
    total_cost = 0.0
    total_nbv = 0.0

    for ast in raw_assets:
        ast_id = ast["id"]
        meta = metadata.get(ast_id, {
            "status": "Unverified",
            "custodian": "Unassigned",
            "notes": ""
        })
        enriched_item = {**ast, **meta}
        enriched_assets.append(enriched_item)
        total_cost += ast["cost"]
        total_nbv += ast["nbv"]

    asset_count = len(enriched_assets)
    unverified_count = sum(1 for item in enriched_assets if item["status"] == "Unverified")
    needs_maintenance_count = sum(1 for item in enriched_assets if item["status"] == "Needs Maintenance")
    is_mock_data = any(item.get("is_mock", False) for item in raw_assets)

    # Sort assets by Net Book Value for chart metrics
    sorted_assets = sorted(enriched_assets, key=lambda x: x["nbv"], reverse=True)
    top_assets = sorted_assets[:10]
    chart_labels = [a["name"] for a in top_assets]
    chart_data = [a["nbv"] for a in top_assets]

    return render_template(
        "fixed_assets.html",
        assets=enriched_assets,
        total_cost=total_cost,
        total_nbv=total_nbv,
        asset_count=asset_count,
        unverified_count=unverified_count,
        needs_maintenance_count=needs_maintenance_count,
        chart_labels=chart_labels,
        chart_data=chart_data,
        is_mock_data=is_mock_data,
        active_page="fixed-assets"
    )


@app.route("/warehouse")
def warehouse():
    """Renders the physical warehouse inbound goods register."""
    from netsuite import get_netsuite_access_token, fetch_warehouse_entries

    token = get_netsuite_access_token()
    raw_entries = fetch_warehouse_entries(token)

    metadata = get_warehouse_metadata()
    enriched_entries = []
    total_value = 0.0

    for ent in raw_entries:
        rcpt_id = ent["id"]
        meta = metadata.get(rcpt_id, {
            "status": "Pending Inspection",
            "verified_qty": ent["expected_qty"],
            "temp_recorded": 24.5,
            "inspector": "Unassigned",
            "notes": ""
        })
        enriched_item = {**ent, **meta}
        enriched_entries.append(enriched_item)
        total_value += ent["value"]

    entry_count = len(enriched_entries)
    pending_count = sum(1 for item in enriched_entries if item["status"] == "Pending Inspection")
    quarantined_count = sum(1 for item in enriched_entries if item["status"] == "Quarantined")
    is_mock_data = any(item.get("is_mock", False) for item in raw_entries)

    # Sort entries by total shipment value for the chart metrics
    sorted_entries = sorted(enriched_entries, key=lambda x: x["value"], reverse=True)
    top_entries = sorted_entries[:10]
    chart_labels = [e["receipt_no"] for e in top_entries]
    chart_data = [e["value"] for e in top_entries]

    return render_template(
        "warehouse.html",
        entries=enriched_entries,
        total_value=total_value,
        entry_count=entry_count,
        pending_count=pending_count,
        quarantined_count=quarantined_count,
        chart_labels=chart_labels,
        chart_data=chart_data,
        is_mock_data=is_mock_data,
        active_page="warehouse"
    )


@app.route("/schema")
def schema():
    """Renders the Developer Schema & Table Connection Explorer."""
    from netsuite import get_netsuite_access_token, fetch_custom_table_registry

    token = get_netsuite_access_token()
    custom_tables = fetch_custom_table_registry(token)

    # Pre-define NetSuite Core ERP Tables with Developer ERD Connection mapping descriptions
    core_tables = [
        {
            "table_name": "transaction",
            "display_name": "Transaction Master (Headers)",
            "category": "Core Financials & ERP",
            "description": "Stores headers for Item Receipts (ItemRcpt), Purchase Orders (PurchOrd), Invoices, and Advances.",
            "connections": [
                {"field": "id", "target": "transactionline.transaction", "type": "1-to-Many (Lines)"},
                {"field": "entity", "target": "customer.id / vendor.id", "type": "Foreign Key (Entity)"},
                {"field": "createdfrom", "target": "transaction.id", "type": "Self-Join (Parent PO / TO)"}
            ]
        },
        {
            "table_name": "transactionline",
            "display_name": "Transaction Line Items (Details)",
            "category": "Core Financials & ERP",
            "description": "Stores individual quantities, rates, bins, and inventory items for every transaction header.",
            "connections": [
                {"field": "transaction", "target": "transaction.id", "type": "Foreign Key (Parent)"},
                {"field": "item", "target": "item.id", "type": "Foreign Key (Inventory)"}
            ]
        },
        {
            "table_name": "customer",
            "display_name": "Customer Accounts Registry",
            "category": "CRM & Sales",
            "description": "Primary repository for company entities holding unapplied advances and accounts receivable balances.",
            "connections": [
                {"field": "id", "target": "CustomerSubsidiaryRelationship.entity", "type": "1-to-Many (Balances)"},
                {"field": "id", "target": "transaction.entity", "type": "1-to-Many (Transactions)"},
                {"field": "subsidiary", "target": "subsidiary.id", "type": "Foreign Key (Operating Entity)"}
            ]
        },
        {
            "table_name": "CustomerSubsidiaryRelationship",
            "display_name": "Customer Subsidiary & Deposit Balance Map",
            "category": "CRM & Sales",
            "description": "Crucial bridge table where unapplied customer deposit balances (depositbalance) are actually calculated and stored.",
            "connections": [
                {"field": "entity", "target": "customer.id", "type": "Foreign Key (Customer)"},
                {"field": "subsidiary", "target": "subsidiary.id", "type": "Foreign Key (Subsidiary)"}
            ]
        },
        {
            "table_name": "customrecord_ncfar_asset",
            "display_name": "Fixed Asset Management Registry (FAM)",
            "category": "Fixed Assets (FAM Bundle)",
            "description": "Stores asset registers, depreciation methods, acquisition costs, and net book values.",
            "connections": [
                {"field": "custrecord_ncfar_assetsubsidiary", "target": "subsidiary.id", "type": "Foreign Key"},
                {"field": "custrecord_ncfar_assetclass", "target": "customrecord_ncfar_assettype.id", "type": "Foreign Key"}
            ]
        },
        {
            "table_name": "item",
            "display_name": "Master Item & Inventory Catalog",
            "category": "Warehouse & Logistics",
            "description": "Holds definitions for HVAC materials, spare parts, refrigerants, and assembly components.",
            "connections": [
                {"field": "id", "target": "transactionline.item", "type": "1-to-Many (Receipt Lines)"}
            ]
        },
        {
            "table_name": "subsidiary",
            "display_name": "Operating Subsidiaries Registry",
            "category": "Company Setup",
            "description": "Defines consolidated business units (e.g., Coolaire Consolidation Inc, Coolaire Logistics).",
            "connections": [
                {"field": "id", "target": "customer.subsidiary / customrecord_ncfar_asset.subsidiary", "type": "Primary Key"}
            ]
        },
        {
            "table_name": "vendor",
            "display_name": "Supplier & Vendor Registry",
            "category": "Procurement & Payables",
            "description": "Stores supplier identities linked to inbound warehouse item receipts and purchase orders.",
            "connections": [
                {"field": "id", "target": "transaction.entity", "type": "1-to-Many (Inbound Receipts)"}
            ]
        }
    ]

    return render_template(
        "schema_explorer.html",
        core_tables=core_tables,
        custom_tables=custom_tables,
        active_page="schema"
    )


# --- API ENDPOINTS ---

@app.route("/api/update_task", methods=["POST"])
def update_task():
    """API endpoint to save customer advance updates locally."""
    customer_id = request.form.get("customer_id")
    status = request.form.get("status", "Unassigned")
    assigned_to = request.form.get("assigned_to", "Unassigned")
    priority = request.form.get("priority", "Medium")
    notes = request.form.get("notes", "")

    if not customer_id:
        return jsonify({"status": "error", "message": "Missing Customer ID"}), 400

    save_task_metadata(customer_id, status, assigned_to, priority, notes)
    flash(f"Updated status for Customer #{customer_id}.", "success")
    return jsonify({"status": "success", "message": "Information synced successfully"})


@app.route("/api/update_asset", methods=["POST"])
def update_asset():
    """API endpoint to save fixed asset verification updates locally."""
    asset_id = request.form.get("asset_id")
    status = request.form.get("status", "Unverified")
    custodian = request.form.get("custodian", "Unassigned")
    notes = request.form.get("notes", "")

    if not asset_id:
        return jsonify({"status": "error", "message": "Missing Asset ID"}), 400

    save_asset_metadata(asset_id, status, custodian, notes)
    flash(f"Verification updated for Asset #{asset_id}.", "success")
    return jsonify({"status": "success", "message": "Information synced successfully"})


@app.route("/api/update_warehouse", methods=["POST"])
def update_warehouse():
    """API endpoint to save warehouse entry inspection updates locally."""
    receipt_id = request.form.get("receipt_id")
    status = request.form.get("status", "Pending Inspection")
    verified_qty = int(request.form.get("verified_qty", 0))
    temp_recorded = float(request.form.get("temp_recorded", 0.0))
    inspector = request.form.get("inspector", "Unassigned")
    notes = request.form.get("notes", "")

    if not receipt_id:
        return jsonify({"status": "error", "message": "Missing Receipt ID"}), 400

    save_warehouse_metadata(receipt_id, status, verified_qty, temp_recorded, inspector, notes)
    flash(f"Quality check completed for Shipment #{receipt_id}.", "success")
    return jsonify({"status": "success", "message": "Information synced successfully"})


@app.route("/api/inspect_table", methods=["GET"])
def api_inspect_table():
    """API endpoint triggered when a developer clicks a table to inspect live column schemas."""
    table = request.args.get("table", "").strip()
    if not table:
        return jsonify({"status": "error", "message": "No table specified"}), 400

    from netsuite import get_netsuite_access_token, inspect_table_schema
    token = get_netsuite_access_token()
    if not token:
        return jsonify({"status": "error", "message": "NetSuite authentication failed"}), 401

    result = inspect_table_schema(token, table)
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
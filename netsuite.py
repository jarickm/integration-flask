# netsuite.py
import os
import time
import jwt
import requests

ACCOUNT_ID = "8481926-sb1"
CLIENT_ID = "a60f098dc8b53351cc02e170b9b11fc6aff6b7d4d273eed1dd4eae97fa266d86"
CERT_ID = "5__5PWNDB5IhMZqaKSZI1JS4Q7frox8VdRWQ37_mzPo"
PRIVATE_KEY_PATH = "private.pem"

_token_store = {
    "token": None,
    "expires_at": 0
}

# Cached resolved schemas to prevent schema-checks on every page request
_resolved_fam_schema = {
    "cost_field": None,
    "nbv_field": None
}

_resolved_wh_schema = {
    "variant_index": None
}


def get_netsuite_access_token():
    """Generates a JWT Assertion and exchanges it for a NetSuite token with local caching."""
    global _token_store
    now = int(time.time())

    if _token_store["token"] and _token_store["expires_at"] > (now + 300):
        return _token_store["token"]

    if not os.path.exists(PRIVATE_KEY_PATH):
        print(f"⚠️ NetSuite credentials ('{PRIVATE_KEY_PATH}') not found. Unable to authenticate.")
        return None

    try:
        with open(PRIVATE_KEY_PATH, "r") as f:
            private_key = f.read()

        token_url = f"https://{ACCOUNT_ID}.suitetalk.api.netsuite.com/services/rest/auth/oauth2/v1/token"

        payload = {
            "iss": CLIENT_ID,
            "scope": ["restlets", "rest_webservices"],
            "aud": token_url,
            "exp": now + 3600,
            "iat": now
        }

        headers = {
            "kid": CERT_ID,
            "alg": "PS256",
            "typ": "JWT"
        }

        client_assertion = jwt.encode(payload, private_key, algorithm="PS256", headers=headers)

        token_data = {
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion
        }

        response = requests.post(token_url, data=token_data, timeout=15)
        if response.status_code == 200:
            token = response.json().get("access_token")
            if token:
                _token_store["token"] = token
                _token_store["expires_at"] = now + 3600
                return token
        print(f"❌ Token Exchange Fail: {response.status_code} - {response.text}")
        return None
    except Exception as e:
        print(f"⚠️ JWT Token construction threw exception: {str(e)}")
        return None


def fetch_customer_advances(token):
    """Retrieves unapplied customer deposits directly from NetSuite using SuiteQL."""
    if not token:
        print("⚠️ No valid NetSuite token. Skipping Advances retrieval.")
        return []

    suiteql_url = f"https://{ACCOUNT_ID}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "transient"
    }

    query_payload = {
        "q": """
             SELECT c.id,
                    c.entityid,
                    c.companyname,
                    c.email,
                    csr.depositbalance,
                    csr.balance,
                    BUILTIN.DF(c.subsidiary)                          as subsidiary,
                    BUILTIN.DF(c.currency)                            as currency,
                    BUILTIN.DF(c.salesrep)                            as salesrep,
                    (TRUNC(CURRENT_DATE) - TRUNC(c.lastmodifieddate)) as days_inactive
             FROM customer c
                      JOIN
                  CustomerSubsidiaryRelationship csr ON c.id = csr.entity
             WHERE c.isinactive = 'F'
               AND csr.depositbalance > 0
             ORDER BY csr.depositbalance DESC
             """
    }

    try:
        response = requests.post(suiteql_url, headers=headers, json=query_payload, timeout=20)
        if response.status_code == 200:
            raw_items = response.json().get("items", [])
            advances = []
            for item in raw_items:
                cust_name = item.get("companyname") or item.get("entityid") or "Unknown Customer"
                advances.append({
                    "id": str(item.get("id")),
                    "name": cust_name,
                    "email": item.get("email") or "billing-department@company.ph",
                    "amount": float(item.get("depositbalance", 0)),
                    "ar_balance": float(item.get("balance", 0)),
                    "subsidiary": item.get("subsidiary") or "Coolaire Consolidated",
                    "currency": item.get("currency") or "PHP",
                    "salesrep": item.get("salesrep") or "House Account",
                    "days_open": int(item.get("days_inactive") or 15),
                    "is_mock": False
                })
            return advances
        else:
            print(f"⚠️ NetSuite API responded with code {response.status_code}: {response.text}")
            return []
    except Exception as e:
        print(f"⚠️ Network error while running API call: {str(e)}")
        return []


def fetch_fixed_assets(token):
    """Retrieves Fixed Asset details using self-adjusting schema discovery."""
    global _resolved_fam_schema
    if not token:
        print("⚠️ No valid NetSuite token. Skipping Fixed Assets retrieval.")
        return []

    suiteql_url = f"https://{ACCOUNT_ID}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "transient"
    }

    # Possible spelling variants for Cost and Current/Book Value fields in NetSuite FAM bundles
    schema_variants = [
        ("custrecord_ncfar_assetcost", "custrecord_ncfar_assetcurrentvalue"),  # Standard Bundle 4.x
        ("custrecord_ncfar_assetcost", "custrecord_ncfar_assetbookvalue"),  # Standard Bundle 3.x
        ("custrecord_ncfar_assetoriginalcost", "custrecord_ncfar_assetcurrentvalue"),
        ("custrecord_ncfar_assetcost", "custrecord_ncfar_assetbookval")
    ]

    cost_field = _resolved_fam_schema["cost_field"]
    nbv_field = _resolved_fam_schema["nbv_field"]

    # Step 1: If we have already successfully resolved the schema, use it directly
    if cost_field and nbv_field:
        q = f"""
            SELECT 
                a.id, a.name, a.{cost_field} as original_cost, a.{nbv_field} as net_book_value
            FROM customrecord_ncfar_asset a WHERE a.isinactive = 'F'
            ORDER BY a.{nbv_field} DESC
        """
        try:
            response = requests.post(suiteql_url, headers=headers, json={"q": q}, timeout=15)
            if response.status_code == 200:
                raw_items = response.json().get("items", [])
                assets = []
                for item in raw_items:
                    assets.append({
                        "id": str(item.get("id")),
                        "name": item.get("name") or "Unnamed Asset",
                        "class": "Fixed Machinery",
                        "subsidiary": "Coolaire Consolidation Inc.",
                        "nbv": float(item.get("net_book_value") or 0.0),
                        "cost": float(item.get("original_cost") or 0.0),
                        "depr_method": "Straight Line",
                        "cap_date": "N/A",
                        "is_mock": False
                    })
                return assets
        except Exception:
            pass

    # Step 2: Schema Discovery Loop (runs only once to find working columns)
    print("🔍 Testing NetSuite FAM schema field variants to resolve valuation columns...")
    for cost_opt, nbv_opt in schema_variants:
        test_query = f"""
            SELECT a.id, a.name, a.{cost_opt} as original_cost, a.{nbv_opt} as net_book_value
            FROM customrecord_ncfar_asset a WHERE a.isinactive = 'F'
            ORDER BY a.{nbv_opt} DESC
        """
        try:
            resp = requests.post(suiteql_url, headers=headers, json={"q": test_query}, timeout=10)
            if resp.status_code == 200:
                # Save the validated schema structure
                _resolved_fam_schema["cost_field"] = cost_opt
                _resolved_fam_schema["nbv_field"] = nbv_opt
                print(f"✅ Auto-resolved FAM Schema: Cost = {cost_opt}, NBV = {nbv_opt}")

                raw_items = resp.json().get("items", [])
                assets = []
                for item in raw_items:
                    assets.append({
                        "id": str(item.get("id")),
                        "name": item.get("name") or "Unnamed Asset",
                        "class": "Fixed Machinery",
                        "subsidiary": "Coolaire Consolidation Inc.",
                        "nbv": float(item.get("net_book_value") or 0.0),
                        "cost": float(item.get("original_cost") or 0.0),
                        "depr_method": "Straight Line",
                        "cap_date": "N/A",
                        "is_mock": False
                    })
                return assets
        except Exception:
            continue

    # Step 3: Minimal fallback (if all database valuation fields fail)
    print("ℹ️ Unable to match standard custom field parameters. Using minimal ID/Name fallback.")
    fallback_query = "SELECT a.id, a.name FROM customrecord_ncfar_asset a WHERE a.isinactive = 'F'"
    try:
        resp_fallback = requests.post(suiteql_url, headers=headers, json={"q": fallback_query}, timeout=15)
        if resp_fallback.status_code == 200:
            raw_items = resp_fallback.json().get("items", [])
            assets = []
            for item in raw_items:
                assets.append({
                    "id": str(item.get("id")),
                    "name": item.get("name") or "Unnamed Asset",
                    "class": "Fixed Machinery",
                    "subsidiary": "Coolaire Consolidation Inc.",
                    "nbv": 0.0,
                    "cost": 0.0,
                    "depr_method": "Straight Line",
                    "cap_date": "N/A",
                    "is_mock": False
                })
            return assets
    except Exception as e:
        print(f"⚠️ NetSuite Fixed Assets fallback execution crashed: {str(e)}")

    return []


def fetch_warehouse_entries(token):
    """Retrieves Inbound Item Receipts using a self-resolving permissions matrix."""
    global _resolved_wh_schema
    if not token:
        print("⚠️ No valid NetSuite token. Skipping Warehouse retrieval.")
        return []

    suiteql_url = f"https://{ACCOUNT_ID}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "transient"
    }

    # Progressive query configurations based on transaction field access layers
    queries = [
        # Variant 0: Full Detail (Requires Vendor and Purchase Order view permissions)
        {
            "sql": """
                   SELECT t.id,
                          t.tranid             as receipt_no,
                          BUILTIN.DF(t.entity) as supplier,
                          t.trandate           as date_received,
                          t.createdfrom        as source_po_id
                   FROM transaction t
                   WHERE t.type = 'ItemRcpt'
                   ORDER BY t.trandate DESC
                   """,
            "desc": "Full Detail (Supplier & Parent PO Links)",
            "has_supplier": True, "has_po": True
        },
        # Variant 1: No PO Parent Reference (Bypasses t.createdfrom permission blocks)
        {
            "sql": """
                   SELECT t.id, t.tranid as receipt_no, BUILTIN.DF(t.entity) as supplier, t.trandate as date_received
                   FROM transaction t
                   WHERE t.type = 'ItemRcpt'
                   ORDER BY t.trandate DESC
                   """,
            "desc": "Supplier Only (PO References Restricted)",
            "has_supplier": True, "has_po": False
        },
        # Variant 2: No Supplier Reference (Bypasses t.entity/Vendor permission blocks)
        {
            "sql": """
                   SELECT t.id, t.tranid as receipt_no, t.trandate as date_received, t.createdfrom as source_po_id
                   FROM transaction t
                   WHERE t.type = 'ItemRcpt'
                   ORDER BY t.trandate DESC
                   """,
            "desc": "PO Only (Supplier Names Restricted)",
            "has_supplier": False, "has_po": True
        },
        # Variant 3: Minimal Core (Absolute fallback - requires only standard transaction view access)
        {
            "sql": """
                   SELECT t.id, t.tranid as receipt_no, t.trandate as date_received
                   FROM transaction t
                   WHERE t.type = 'ItemRcpt'
                   ORDER BY t.trandate DESC
                   """,
            "desc": "Core Transaction Headers Only (Suppliers & PO References Restricted)",
            "has_supplier": False, "has_po": False
        }
    ]

    v_index = _resolved_wh_schema["variant_index"]

    # Step 1: If we have already successfully resolved a safe working query, run it directly
    if v_index is not None:
        selected = queries[v_index]
        try:
            response = requests.post(suiteql_url, headers=headers, json={"q": selected["sql"]}, timeout=15)
            if response.status_code == 200:
                raw_items = response.json().get("items", [])
                entries = []
                for item in raw_items:
                    po_id = item.get("source_po_id") if selected["has_po"] else None
                    po_ref = f"PO #{po_id}" if po_id else ("N/A" if selected["has_po"] else "PO Reference (Restricted)")
                    supplier_val = item.get("supplier") if selected["has_supplier"] else "Default Supplier (Restricted)"

                    entries.append({
                        "id": str(item.get("id")),
                        "receipt_no": item.get("receipt_no") or f"IR-{item.get('id')}",
                        "supplier": supplier_val,
                        "date_received": item.get("date_received") or "N/A",
                        "source_po": po_ref,
                        "value": 0.0,
                        "expected_qty": 1,
                        "item_category": "Inbound Materials",
                        "is_mock": False
                    })
                return entries
        except Exception:
            pass

    # Step 2: Dynamic Schema Resolution Loop (Runs on initial startup or change)
    print("🔍 Testing NetSuite Warehouse access variants to resolve permitted fields...")
    for idx, q_config in enumerate(queries):
        try:
            response = requests.post(suiteql_url, headers=headers, json={"q": q_config["sql"]}, timeout=10)
            if response.status_code == 200:
                # Save the first validated operational query configuration index
                _resolved_wh_schema["variant_index"] = idx
                print(f"✅ Auto-resolved Warehouse Query Schema: {q_config['desc']}")

                # Print explicit role tuning recommendations based on the resolved level
                if idx > 0:
                    print("💡 Role Optimization Tips:")
                    if not q_config["has_supplier"]:
                        print("   -> To resolve Supplier Names, add: Permissions ➔ Lists ➔ Vendors (Level: View)")
                    if not q_config["has_po"]:
                        print(
                            "   -> To resolve Parent PO codes, add: Permissions ➔ Transactions ➔ Purchase Orders (Level: View)")

                raw_items = response.json().get("items", [])
                entries = []
                for item in raw_items:
                    po_id = item.get("source_po_id") if q_config["has_po"] else None
                    po_ref = f"PO #{po_id}" if po_id else ("N/A" if q_config["has_po"] else "PO Reference (Restricted)")
                    supplier_val = item.get("supplier") if q_config["has_supplier"] else "Default Supplier (Restricted)"

                    entries.append({
                        "id": str(item.get("id")),
                        "receipt_no": item.get("receipt_no") or f"IR-{item.get('id')}",
                        "supplier": supplier_val,
                        "date_received": item.get("date_received") or "N/A",
                        "source_po": po_ref,
                        "value": 0.0,
                        "expected_qty": 1,
                        "item_category": "Inbound Materials",
                        "is_mock": False
                    })
                return entries
        except Exception:
            continue

    print("❌ All transaction item receipt queries returned errors. Skipping Warehouse retrieval.")
    return []


# Add these two functions to the bottom of netsuite.py

# netsuite.py (Updated fetch_custom_table_registry function)

def fetch_custom_table_registry(token):
    """Auto-detects all Custom Records and Bundles installed in your NetSuite instance."""
    if not token:
        return []

    suiteql_url = f"https://{ACCOUNT_ID}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql?limit=100"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "transient"
    }

    # REPLACED: Removed invalid 'id' and 'isinactive' columns to prevent 400 Bad Request error
    query_payload = {
        "q": """
             SELECT name,
                    scriptid
             FROM customrecordtype
             ORDER BY name ASC
             """
    }

    try:
        response = requests.post(suiteql_url, headers=headers, json=query_payload, timeout=15)
        if response.status_code == 200:
            raw_items = response.json().get("items", [])
            custom_tables = []

            for index, item in enumerate(raw_items):
                script_id = item.get("scriptid")
                if not script_id:
                    continue

                # SuiteQL custom table references are evaluated as lowercase
                script_id_lower = script_id.strip().lower()

                # NetSuite custom records usually have "customrecord_" prefix, prepend if missing
                if not script_id_lower.startswith("customrecord"):
                    script_id_lower = f"customrecord_{script_id_lower}"

                custom_tables.append({
                    "table_name": script_id_lower,
                    "display_name": item.get("name") or "Unnamed Custom Table",
                    "id": str(index + 1),  # Safely generate sequential index IDs
                    "category": "Custom Record / Bundle",
                    "description": f"Custom table deployed under script ID: {script_id_lower}"
                })
            return custom_tables
        else:
            print(f"⚠️ Could not query customrecordtype (Status {response.status_code}): {response.text}")
            return []
    except Exception as e:
        print(f"⚠️ Custom table discovery exception: {str(e)}")
        return []


# netsuite.py (Updated inspect_table_schema function)

# netsuite.py (Updated inspect_table_schema function)

def inspect_table_schema(token, table_name):
    """Inspects a table's schema instantly using NetSuite's native system columns schema."""
    if not token or not table_name:
        return {"status": "error", "message": "Missing authentication or table target."}

    suiteql_url = f"https://{ACCOUNT_ID}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "transient"
    }

    # NetSuite stores table identifiers in uppercase within its system catalogs
    system_table_key = table_name.strip().upper()

    # Query 1: Native REST SuiteQL System Catalog (Works even on 0 rows)
    query_attempt_1 = {
        "q": f"""
            SELECT 
                column_name 
            FROM 
                system.columns 
            WHERE 
                table_name = '{system_table_key}'
            ORDER BY 
                column_name ASC
        """
    }

    # Query 2: Legacy Connect/ODBC System Catalog
    query_attempt_2 = {
        "q": f"""
            SELECT 
                column_name 
            FROM 
                sys_columns 
            WHERE 
                table_name = '{system_table_key}'
            ORDER BY 
                column_name ASC
        """
    }

    # Query 3: Standalone data scan fallback (only runs if metadata views are blocked)
    query_attempt_3 = {
        "q": f"SELECT * FROM {table_name}"
    }

    try:
        # --- PHASE 1: Attempt Native System.Columns View ---
        response = requests.post(suiteql_url, headers=headers, json=query_attempt_1, timeout=15)

        if response.status_code == 200:
            raw_items = response.json().get("items", [])
            columns = []
            for item in raw_items:
                col_name = item.get("column_name")
                if col_name:
                    columns.append(col_name)

            if len(columns) > 0:
                return {
                    "status": "success",
                    "table": table_name,
                    "column_count": len(columns),
                    "columns": sorted(columns),
                    "is_empty": False
                }

        # --- PHASE 2: Attempt Legacy Sys_Columns View ---
        response_legacy = requests.post(suiteql_url, headers=headers, json=query_attempt_2, timeout=15)

        if response_legacy.status_code == 200:
            raw_items = response_legacy.json().get("items", [])
            columns = []
            for item in raw_items:
                col_name = item.get("column_name")
                if col_name:
                    columns.append(col_name)

            if len(columns) > 0:
                return {
                    "status": "success",
                    "table": table_name,
                    "column_count": len(columns),
                    "columns": sorted(columns),
                    "is_empty": False
                }

        # --- PHASE 3: Fallback Data Scan (Requires at least 1 row) ---
        print(f"ℹ️ Metadata views unavailable. Running fallback data scan for '{table_name}'...")
        fallback_url = f"{suiteql_url}?limit=1"
        response_fallback = requests.post(fallback_url, headers=headers, json=query_attempt_3, timeout=35)

        if response_fallback.status_code == 200:
            raw_items = response_fallback.json().get("items", [])
            columns = []
            if raw_items and len(raw_items) > 0:
                columns = sorted(list(raw_items[0].keys()))

            if len(columns) > 0:
                return {
                    "status": "success",
                    "table": table_name,
                    "column_count": len(columns),
                    "columns": columns,
                    "is_empty": False
                }
            else:
                return {
                    "status": "error",
                    "message": f"Table '{table_name}' currently holds zero rows in NetSuite. (Add 1 sample entry to reveal column schemas via fallback scan)."
                }
        else:
            return {
                "status": "error",
                "message": f"NetSuite returned error {response_fallback.status_code} for table '{table_name}'."
            }

    except requests.exceptions.Timeout:
        return {
            "status": "error",
            "message": f"Connection timed out while inspecting table '{table_name}'."
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Network exception while inspecting table: {str(e)}"
        }
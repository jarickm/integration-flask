import time
import jwt
import requests
from flask import Flask, render_template_string

ACCOUNT_ID = "8481926-sb1"
CLIENT_ID = "a60f098dc8b53351cc02e170b9b11fc6aff6b7d4d273eed1dd4eae97fa266d86"
CERT_ID = "5__5PWNDB5IhMZqaKSZI1JS4Q7frox8VdRWQ37_mzPo"
PRIVATE_KEY_PATH = "private.pem"

app = Flask(__name__)


def get_netsuite_access_token():
    """Generates a JWT Assertion and exchanges it for a NetSuite Bearer Access Token."""
    with open(PRIVATE_KEY_PATH, "r") as f:
        private_key = f.read()

    now = int(time.time())
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

    token_response = requests.post(token_url, data=token_data)
    return token_response.json().get("access_token")


def fetch_customer_advances(token):
    """Fetches customer deposit balances directly without querying transactions."""
    suiteql_url = f"https://{ACCOUNT_ID}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "transient"
    }

    # Connects Customer to CustomerSubsidiaryRelationship where deposit balance lives
    query_payload = {
        "q": """
            SELECT 
                c.id,
                c.entityid,
                c.companyname,
                c.email,
                csr.depositbalance,
                csr.balance
            FROM 
                customer c
            JOIN 
                CustomerSubsidiaryRelationship csr ON c.id = csr.entity
            WHERE 
                c.isinactive = 'F'
                AND csr.depositbalance > 0
            ORDER BY 
                csr.depositbalance DESC
        """
    }

    response = requests.post(suiteql_url, headers=headers, json=query_payload)

    if response.status_code == 200:
        raw_items = response.json().get("items", [])
        advances = []
        for item in raw_items:
            cust_name = item.get("companyname") or item.get("entityid") or "Unknown Customer"
            advances.append({
                "id": item.get("id"),
                "name": cust_name,
                "email": item.get("email") or "N/A",
                "amount": float(item.get("depositbalance", 0)),
                "ar_balance": float(item.get("balance", 0)),
                "count": 1
            })
        print(f"✅ Found {len(advances)} customers with deposit balances.")
        return advances
    else:
        print("❌ Error fetching customer balances:", response.text)
        return []

@app.route("/")
def index():
    token = get_netsuite_access_token()
    if not token:
        return "Failed to authenticate with NetSuite", 500

    advances = fetch_customer_advances(token)

    # Calculate summary metrics
    total_advance_amount = sum(item["amount"] for item in advances)
    total_customers_with_advances = len(advances)

    with open("dashboard.html", "r") as f:
        html_template = f.read()

    return render_template_string(
        html_template,
        advances=advances,
        total_amount=total_advance_amount,
        customer_count=total_customers_with_advances
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
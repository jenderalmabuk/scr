import urllib.request, json

url = "http://127.0.0.1:8787/gateway/position-action"
headers = {
    "Authorization": "Bearer nexus_gateway_test_2024",
    "Content-Type": "application/json"
}
payload = {
    "symbol": "NBISUSDT",
    "action": "CLOSE",
    "reason": "PROVIDER_CLOSE"
}
req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
try:
    with urllib.request.urlopen(req) as resp:
        print("Close response:", resp.read().decode())
except Exception as e:
    print("Error:", e)


import concurrent.futures
import requests
from datetime import datetime, timezone

BASE = "http://127.0.0.1:8000"

payload = {
    "emp_code": "EMP9877",
    "punched_at": int(datetime.now(timezone.utc).timestamp() * 1000),
    "status": "PRESENT"
}

def punch_in(_):
    try:
        response = requests.post(
            f"{BASE}/attendance/punch-in",
            json=payload,
            timeout=10
        )
        return response.status_code, response.text[:300]
    except Exception as exc:
        return "ERROR", str(exc)

with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(punch_in, range(2)))

for result in results:
    print(result)

codes = [result[0] for result in results]
print("\nStatus codes:", codes)

if codes.count(201) == 1 and codes.count(409) == 1:
    print("PASS: exactly one punch-in succeeded.")
else:
    print("CHECK: expected one 201 and one 409.")


import concurrent.futures
import requests
from datetime import datetime, timezone

BASE = "http://127.0.0.1:8000"
EMP_CODE = "EMP9877"

# Use one identical timestamp for both simultaneous requests.
punched_at = int(datetime.now(timezone.utc).timestamp() * 1000)

payload = {
    "emp_code": EMP_CODE,
    "punched_at": punched_at
}

def punch_out(_):
    try:
        response = requests.post(
            f"{BASE}/attendance/punch-out",
            json=payload,
            timeout=15
        )
        return response.status_code, response.text[:300]
    except Exception as exc:
        return "ERROR", str(exc)

with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(punch_out, range(2)))

for result in results:
    print(result)

codes = [result[0] for result in results]
print("\nStatus codes:", codes)

if codes.count(200) == 1 and codes.count(409) == 1:
    print("PASS: exactly one punch-out succeeded.")
else:
    print("CHECK: expected one 200 and one 409.")

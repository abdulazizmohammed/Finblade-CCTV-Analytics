import time, urllib.request
url = "http://127.0.0.1:8000/api/v1/zones/state"
for i in range(15):
    try:
        r = urllib.request.urlopen(url, timeout=1)
        print("API_UP", r.status, r.read().decode()[:200])
        break
    except Exception as e:
        time.sleep(1)
else:
    print("API_DID_NOT_COME_UP")

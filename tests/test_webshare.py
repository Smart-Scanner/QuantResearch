import requests, time, concurrent.futures

proxies_raw = [
    "31.59.20.176:6754:spkthxzu:xv2ywumcem1q",
    "31.56.127.193:7684:spkthxzu:xv2ywumcem1q",
    "45.38.107.97:6014:spkthxzu:xv2ywumcem1q",
    "38.154.203.95:5863:spkthxzu:xv2ywumcem1q",
    "198.105.121.200:6462:spkthxzu:xv2ywumcem1q",
    "64.137.96.74:6641:spkthxzu:xv2ywumcem1q",
    "198.23.243.226:6361:spkthxzu:xv2ywumcem1q",
    "38.154.185.97:6370:spkthxzu:xv2ywumcem1q",
    "142.111.67.146:5611:spkthxzu:xv2ywumcem1q",
    "191.96.254.138:6185:spkthxzu:xv2ywumcem1q",
]

def to_url(raw):
    parts = raw.split(":")
    return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"

URL = "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1d"

def test(raw):
    url = to_url(raw)
    ip_port = raw.split(":")[0] + ":" + raw.split(":")[1]
    try:
        t = time.time()
        r = requests.get(URL, proxies={"http": url, "https": url}, timeout=6,
                         headers={"User-Agent": "Mozilla/5.0"})
        ms = int((time.time() - t) * 1000)
        return ip_port, url, r.status_code, ms
    except Exception as e:
        return ip_port, url, str(e)[:50], 0

alive = []
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
    for ip_port, url, status, ms in ex.map(test, proxies_raw):
        tag = "ALIVE" if status == 200 else "DEAD"
        print(f"  [{tag}] {ip_port} | {status} | {ms}ms")
        if status == 200:
            alive.append((url, ms))

alive.sort(key=lambda x: x[1])
print(f"\nAlive: {len(alive)}/10")
if alive:
    val = ",".join(u for u, _ in alive)
    print(f"\nYFINANCE_PROXIES={val}")

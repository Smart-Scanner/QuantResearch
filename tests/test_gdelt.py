import requests

query = '("NSE" OR "Nifty" OR "India stock") (earnings OR profit OR revenue OR "order win" OR contract OR acquisition OR quarterly OR IPO OR merger OR buyback OR dividend)'
params = {
    'query': query,
    'mode': 'ArtList',
    'maxrecords': 250,
    'format': 'json',
    'timespan': '48H',
    'sort': 'DateDesc'
}
headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}

try:
    print("Testing GDELT...")
    resp = requests.get('https://api.gdeltproject.org/api/v2/doc/doc', params=params, headers=headers, timeout=10)
    print("Status:", resp.status_code)
    print("Response text:", resp.text[:1000])
except Exception as e:
    print("Exception:", e)

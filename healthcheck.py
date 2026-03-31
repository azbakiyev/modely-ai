import requests
r = requests.get('https://modely-ai-production.up.railway.app/health', timeout=10)
print(r.status_code, r.text)

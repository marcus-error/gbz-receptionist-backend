import requests
import datetime
from datetime import timezone, timedelta

url = 'http://localhost:8000/api/v1/leads/upload'
headers = {'Authorization': 'Bearer test'}

now = datetime.datetime.now(timezone.utc) + timedelta(minutes=1)

payload = {
    'leads': [
        {
            'name': 'Test Lead',
            'phone': '09410697548',
            'call_time': now.isoformat(),
            'script': 'Hello, I am calling you about your recent inquiry.'
        }
    ]
}

print('Uploading lead...')
r = requests.post(url, json=payload, headers=headers)
print(r.status_code)
print(r.text)

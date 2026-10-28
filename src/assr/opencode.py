"""OpenCode HTTP adapter. Never retries potentially accepted writes."""
import base64
import json
from urllib.parse import urlparse, quote
from urllib.request import Request, urlopen

class OpenCode:
    def __init__(self, url='http://127.0.0.1:4096', password=None, username='opencode', timeout=120):
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            raise ValueError('invalid server URL')
        if parsed.scheme == 'http' and parsed.hostname not in ('localhost','127.0.0.1','::1'):
            raise ValueError('remote server requires HTTPS')
        self.url, self.timeout = url.rstrip('/'), timeout
        self.headers = {'Content-Type':'application/json'}
        if password:
            token = base64.b64encode(f'{username}:{password}'.encode()).decode()
            self.headers['Authorization'] = 'Basic ' + token

    def request(self, method, path, body=None):
        request = Request(self.url + path, data=None if body is None else json.dumps(body).encode(), headers=self.headers, method=method)
        with urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def health(self):
        return self.request('GET', '/global/health')

    def create(self, title):
        return self.request('POST','/session',{'title':title})['id']

    def prompt(self, session, profile, text):
        return self.request('POST',f'/session/{quote(session, safe="")}/message', {'agent':profile,'parts':[{'type':'text','text':text}]})

    def abort(self, session):
        return self.request('POST',f'/session/{quote(session, safe="")}/abort', {})

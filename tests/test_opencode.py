import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from assr.opencode import OpenCode

class AdapterTest(unittest.TestCase):
    def test_real_http_contract(self):
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path,body))
                payload={'id':'sess'} if self.path=='/session' else {'parts':[]}
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps(payload).encode())
            def log_message(self,*args):pass
        server=HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            client=OpenCode(f'http://127.0.0.1:{server.server_port}')
            sid=client.create('Task');client.prompt(sid,'developer-plan','Read code')
            self.assertEqual(calls[-1][0],'/session/sess/message')
            self.assertEqual(calls[-1][1]['agent'],'developer-plan')
        finally:server.shutdown();thread.join();server.server_close()
    def test_remote_plain_http_rejected(self):
        with self.assertRaises(ValueError):OpenCode('http://example.com')

#!/usr/bin/env python3
"""
Local CORS proxy for the HSP API.
Run this before using the commute tracker app:  python3 proxy.py
"""
import http.server
import urllib.request
import json

PORT = 8787
ALLOWED_PATHS = {'serviceMetrics', 'serviceDetails'}
HSP_BASE = 'https://hsp-prod.rockshore.net/api/v1'

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization',
}

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def send_cors_headers(self):
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_POST(self):
        path = self.path.strip('/')
        if path not in ALLOWED_PATHS:
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        auth = self.headers.get('Authorization', '')

        req = urllib.request.Request(
            f'{HSP_BASE}/{path}',
            data=body,
            headers={'Content-Type': 'application/json', 'Authorization': auth},
            method='POST'
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                self.send_response(resp.status)
                self.send_header('Content-Type', 'application/json')
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def log_message(self, fmt, *args):
        print(f'  {args[0]} {args[1]}')

print(f'HSP proxy running on http://localhost:{PORT}')
print('Keep this open while using the app. Press Ctrl+C to stop.\n')
http.server.HTTPServer(('', PORT), ProxyHandler).serve_forever()

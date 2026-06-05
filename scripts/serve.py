#!/usr/bin/env python3
"""
Tiny static server for the R2 stand-in (serve/), with permissive CORS so the app
can fetch it cross-origin exactly as it will fetch data.woodpecker.naklitechie.com.

This is the LOCAL substitute for R2. Tomorrow the app's single DATA constant flips
from http://localhost:8787 to the R2 custom domain and this server goes away.

Run: python3 scripts/serve.py            (serves ./serve on :8787)
     python3 scripts/serve.py --port 9000 --dir serve
"""
import argparse
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class CORSHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt, *args):  # quieter
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--dir", default="serve")
    args = ap.parse_args()
    root = os.path.abspath(args.dir)
    os.makedirs(root, exist_ok=True)
    os.chdir(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), CORSHandler)
    print(f"Serving {root} at http://127.0.0.1:{args.port}  (CORS *)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()

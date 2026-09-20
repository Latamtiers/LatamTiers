#!/usr/bin/env python3
"""
server.py — Sirve PVPHQ y hace de proxy hacia Lunaris (con DoH fallback).
"""

import argparse
import http.server
import json
import socket
import socketserver
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================
LUNARIS_HOST = "mx.lunarishost.com"
LUNARIS_PORT = 20037
LUNARIS_PATH = "/web"
PROXY_PATH = "/api/ranking"
TIMEOUT = 20


# ============================================================
# DNS RESOLUTION — sistema + fallback a DoH
# ============================================================
def resolve_system(hostname: str) -> str | None:
    """Resolver con el DNS del sistema forzando IPv4."""
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
        if infos:
            return infos[0][4][0]
    except socket.gaierror:
        return None
    return None


def resolve_doh(hostname: str, timeout: int = 6) -> str | None:
    """Fallback: resolver usando Cloudflare / Google DNS-over-HTTPS."""
    endpoints = [
        f"https://cloudflare-dns.com/dns-query?name={hostname}&type=A",
        f"https://dns.google/resolve?name={hostname}&type=A",
        f"https://1.1.1.1/dns-query?name={hostname}&type=A",
    ]
    for url in endpoints:
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/dns-json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for ans in data.get("Answer", []) or []:
                if ans.get("type") == 1 and ans.get("data"):
                    return ans["data"]
        except Exception:
            continue
    return None


def resolve_host(hostname: str) -> str | None:
    ip = resolve_system(hostname)
    if ip:
        return ip
    ip = resolve_doh(hostname)
    return ip


# ============================================================
# FETCH A LUNARIS
# ============================================================
def fetch_lunaris() -> tuple[bytes, str]:
    """
    Descarga el JSON de Lunaris.
    Devuelve (body, content_type) o lanza una excepción con mensaje claro.
    """
    ip = resolve_host(LUNARIS_HOST)
    if ip is None:
        raise RuntimeError(
            f"No se pudo resolver '{LUNARIS_HOST}' ni por DNS del sistema ni por DoH. "
            "Verifica tu conexión a internet o si el host está caído."
        )

    # Intento 1: hostname normal
    url = f"http://{LUNARIS_HOST}:{LUNARIS_PORT}{LUNARIS_PATH}"
    headers = {"Accept": "application/json", "User-Agent": "PVPHQ-proxy/1.0"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "application/json")
        json.loads(body)  # validación
        return body, ctype
    except (socket.gaierror, urllib.error.URLError) as e:
        # Intento 2: IP directa con Host header
        pass
    except json.JSONDecodeError:
        raise RuntimeError("Lunaris no devolvió JSON válido (intento 1)")

    url_ip = f"http://{ip}:{LUNARIS_PORT}{LUNARIS_PATH}"
    headers_ip = dict(headers)
    headers_ip["Host"] = f"{LUNARIS_HOST}:{LUNARIS_PORT}"

    try:
        req = urllib.request.Request(url_ip, headers=headers_ip)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "application/json")
        json.loads(body)
        return body, ctype
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Lunaris (IP {ip}) HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"No se pudo conectar a {ip}:{LUNARIS_PORT} — {e.reason}")
    except json.JSONDecodeError:
        raise RuntimeError("Lunaris no devolvió JSON válido (intento 2)")


# ============================================================
# HTTP HANDLER
# ============================================================
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

    def do_GET(self):
        if self.path == PROXY_PATH or self.path.startswith(PROXY_PATH + "?"):
            self._proxy_ranking()
            return
        super().do_GET()

    def _proxy_ranking(self):
        try:
            body, ctype = fetch_lunaris()
        except RuntimeError as e:
            # Fallback: servir ranking.json local si existe
            local = Path(__file__).parent / "ranking.json"
            if local.exists():
                print(f"[proxy] ⚠️  {e}", file=sys.stderr)
                print(f"[proxy]    Sirviendo {local.name} local como fallback", file=sys.stderr)
                self._send_json(local.read_bytes(), 200)
                return
            self._send_error(502, str(e))
            return
        except Exception as e:
            self._send_error(500, f"Error inesperado: {e}")
            return

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, body: bytes, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code, message):
        payload = json.dumps({"error": message}).encode("utf-8")
        self._send_json(payload, code)

    def log_message(self, fmt, *args):
        if self.path.startswith(PROXY_PATH):
            sys.stderr.write(f"[proxy] {fmt % args}\n")


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="0.0.0.0")
    return p.parse_args()


def main():
    args = parse_args()

    # Diagnóstico inicial
    print(f"🔍 Resolviendo {LUNARIS_HOST}…")
    ip = resolve_system(LUNARIS_HOST)
    if ip:
        print(f"   ✅ DNS del sistema: {ip}")
    else:
        print(f"   ⚠️  DNS del sistema falló. Probando DoH…")
        ip = resolve_doh(LUNARIS_HOST)
        if ip:
            print(f"   ✅ DNS over HTTPS: {ip}")
        else:
            print(f"   ❌ No se pudo resolver de ninguna forma.")
            print(f"      El server igual arranca, pero el proxy dará 502.")
            print(f"      Asegúrate de tener 'ranking.json' local como fallback.")

    with ThreadingTCPServer((args.host, args.port), Handler) as httpd:
        print()
        print(f"🚀 Sirviendo PVPHQ en http://localhost:{args.port}")
        print(f"🔀 Proxy Lunaris en   http://localhost:{args.port}{PROXY_PATH}")
        print(f"   → http://{LUNARIS_HOST}:{LUNARIS_PORT}{LUNARIS_PATH}")
        print("   Ctrl+C para parar.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n⏹️  Servidor detenido.")


if __name__ == "__main__":
    main()
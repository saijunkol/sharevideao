"""ShareVideo DLNA Media Server — main entry point.

Starts an HTTP server for DLNA protocol on port 8000, a Flask web UI on port 8080,
and an SSDP broadcaster for TV discovery.

Usage:
    python server.py [--port 8000] [--web-port 8080]
"""

import argparse
import json
import os
import signal
import socket
import sys
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from dlna.device_xml import device_description, cds_scpd, cms_scpd
from dlna.media_store import MediaStore

# Will be imported in Phase 2 & 3 — stubbed for now
SSDP_ADVERTISER = None  # type: ignore
SOAP_HANDLER = None     # type: ignore
FLASK_APP_RUN = None    # type: ignore


# ====================================================================
# Config Manager
# ====================================================================

DEFAULT_CONFIG = {
    "uuid": "",           # generated on first run
    "server_name": "ShareVideo",
    "port": 8000,
    "web_port": 8080,
    "shared_folders": [],
    "allowed_extensions": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v"],
}


class ConfigManager:
    """Load, persist, and provide access to config.json."""

    def __init__(self, path: str):
        self.path = path
        self.data: dict = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
            # Ensure all default keys exist
            for key, value in DEFAULT_CONFIG.items():
                if key not in self.data:
                    self.data[key] = value
        else:
            self.data = dict(DEFAULT_CONFIG)
            self.data["uuid"] = str(uuid.uuid4())
            self.save()

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()


# ====================================================================
# IP Detection
# ====================================================================

def get_local_ip() -> str:
    """Detect the LAN IP by connecting a UDP socket to a dummy address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        # Fallback: try hostname resolution
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


# ====================================================================
# DLNA HTTP Request Handler
# ====================================================================

# Global references set at startup
_media_store: MediaStore = None  # type: ignore
_config: ConfigManager = None    # type: ignore
_local_ip: str = ""
_web_port: int = 8080


class DLNARequestHandler(BaseHTTPRequestHandler):
    """Handles DLNA protocol requests: device XML, SOAP, media files."""

    # Suppress default request logging to stderr (keep it clean)
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/device.xml" or path == "/description.xml":
            self._serve_xml(device_description(
                _config.get("uuid"), _config.get("server_name"),
                _local_ip, _config.get("port"), _web_port
            ))

        elif path == "/cds.xml":
            self._serve_xml(cds_scpd())

        elif path == "/cms.xml":
            self._serve_xml(cms_scpd())

        elif path.startswith("/media/"):
            media_id = path.split("/media/", 1)[1]
            if not media_id:
                self.send_error(404)
                return
            self._serve_media(media_id)

        elif path == "/":
            # Redirect to web UI
            self.send_response(302)
            self.send_header("Location", f"http://{_local_ip}:{_web_port}/")
            self.end_headers()

        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        soap_action = self.headers.get("SOAPACTION", "")

        if path in ("/cds/control", "/cms/control"):
            handler = SOAP_HANDLER
            if handler is None:
                self.send_error(500, "SOAP handler not initialized")
                return
            response_xml = handler.handle(path, body, soap_action)
            self._serve_xml(response_xml)

        else:
            self.send_error(404)

    def do_SUBSCRIBE(self):
        """Minimal GENA SUBSCRIBE — some TVs try to subscribe to events."""
        self.send_response(200)
        self.send_header("SID", f"uuid:{uuid.uuid4()}")
        self.send_header("TIMEOUT", "Second-1800")
        self.end_headers()

    def do_UNSUBSCRIBE(self):
        self.send_response(200)
        self.end_headers()

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _serve_xml(self, xml_str: str):
        data = xml_str.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("EXT", "")
        self.send_header("Server", "Windows/10 UPnP/1.0 ShareVideo/1.0")
        self.end_headers()
        self.wfile.write(data)

    def _serve_media(self, media_id: str):
        try:
            code, headers, body = _media_store.serve_file(
                media_id, self.headers.get("Range")
            )
        except Exception as e:
            print(f"  [ERROR] Serving media {media_id}: {e}")
            self.send_error(500)
            return

        self.send_response(code)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()

        if isinstance(body, bytes):
            self.wfile.write(body)
        elif hasattr(body, "read"):
            try:
                while chunk := body.read(65536):
                    self.wfile.write(chunk)
            finally:
                body.close()


# ====================================================================
# Server startup
# ====================================================================

def main():
    global _media_store, _config, _local_ip, _web_port
    global SSDP_ADVERTISER, SOAP_HANDLER, FLASK_APP_RUN

    parser = argparse.ArgumentParser(description="ShareVideo DLNA Media Server")
    parser.add_argument("--port", type=int, help="DLNA HTTP server port")
    parser.add_argument("--web-port", type=int, help="Web management UI port")
    parser.add_argument("--config", type=str, default="config.json",
                        help="Config file path")
    args = parser.parse_args()

    # Load config
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               args.config)
    _config = ConfigManager(config_path)

    # Apply CLI overrides
    port = args.port or _config.get("port", 8000)
    _web_port = args.web_port or _config.get("web_port", 8080)
    _config.set("port", port)
    _config.set("web_port", _web_port)

    # Detect LAN IP
    _local_ip = get_local_ip()

    print("=" * 60)
    print("  ShareVideo DLNA Media Server")
    print("=" * 60)
    print(f"  Server name : {_config.get('server_name')}")
    print(f"  Local IP    : {_local_ip}")
    print(f"  DLNA port   : {port}")
    print(f"  Web UI      : http://{_local_ip}:{_web_port}")
    print(f"  Config      : {config_path}")
    print("-" * 60)

    # Create media store and scan
    _media_store = MediaStore(_local_ip, port)
    rescan()

    # ---- Phase 2: SOAP Handler ----
    try:
        from dlna.soap_handler import SoapHandler
        SOAP_HANDLER = SoapHandler(_media_store)
        print("  SOAP handler: ready")
    except ImportError:
        print("  SOAP handler: not available (Phase 2)")

    # ---- Phase 3: SSDP ----
    try:
        from dlna.ssdp import SSDPAdvertiser
        ssdp = SSDPAdvertiser(
            uuid=_config.get("uuid"),
            local_ip=_local_ip,
            port=port,
            server_name=_config.get("server_name"),
        )
        ssdp.start()
        SSDP_ADVERTISER = ssdp
        print("  SSDP        : broadcasting")
    except ImportError:
        print("  SSDP        : not available (Phase 3)")

    # ---- Phase 4: Flask Web UI ----
    try:
        from web.app import create_app, run_flask
        flask_app = create_app(_media_store, _config)
        flask_thread = threading.Thread(
            target=run_flask,
            args=(flask_app, "0.0.0.0", _web_port),
            daemon=True,
            name="flask-web-ui",
        )
        flask_thread.start()
        FLASK_APP_RUN = True
        print(f"  Web UI      : http://localhost:{_web_port}")
    except ImportError:
        print("  Web UI      : not available (Phase 4)")

    print("-" * 60)
    print("  Press Ctrl+C to stop the server")
    print("=" * 60)

    # Start HTTP server (blocks main thread)
    server = HTTPServer(("0.0.0.0", port), DLNARequestHandler)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    shutdown_flag = threading.Event()

    def do_shutdown(*args):
        print("\nShutting down...")
        shutdown_flag.set()

        # Send SSDP byebye
        if SSDP_ADVERTISER:
            SSDP_ADVERTISER.stop()

        # Stop HTTP server
        server.shutdown()

        # Save config
        _config.save()
        print("Server stopped. Goodbye!")
        sys.exit(0)

    signal.signal(signal.SIGINT, do_shutdown)
    signal.signal(signal.SIGTERM, do_shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        do_shutdown()


def rescan():
    """Re-scan shared folders. Called on startup and from the web UI."""
    folders = _config.get("shared_folders", [])
    extensions = _config.get("allowed_extensions", [])
    if folders:
        print(f"  Scanning {len(folders)} shared folder(s)...")
        _media_store.scan(folders, extensions)
    else:
        print("  No shared folders configured. Use the Web UI to add some.")


if __name__ == "__main__":
    main()

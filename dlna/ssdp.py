"""SSDP (Simple Service Discovery Protocol) advertiser.

Broadcasts NOTIFY messages on the LAN so the TV can discover the server,
and responds to M-SEARCH queries from DLNA clients.

Runs as a daemon thread. One UDP socket handles both sending NOTIFYs
and receiving/responding to M-SEARCH requests.
"""

import email.utils
import socket
import struct
import threading
import time
from typing import Optional

MULTICAST_GROUP = "239.255.255.250"
MULTICAST_PORT = 1900
SSDP_MAX_AGE = 1800       # seconds
NOTIFY_INTERVAL = 900     # send NOTIFY at half the max-age
BYEBYE_COUNT = 3          # send byebye multiple times for reliability


class SSDPAdvertiser:
    """Manages SSDP multicast socket, periodic NOTIFYs, and M-SEARCH replies."""

    def __init__(self, uuid: str, local_ip: str, port: int, server_name: str):
        self.uuid = uuid
        self.local_ip = local_ip
        self.port = port
        self.server_name = server_name
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None

        # USN (Unique Service Name) values to announce
        self._usns = [
            f"uuid:{uuid}::urn:schemas-upnp-org:device:MediaServer:1",
            f"uuid:{uuid}::urn:schemas-upnp-org:service:ContentDirectory:1",
            f"uuid:{uuid}::urn:schemas-upnp-org:service:ConnectionManager:1",
            f"uuid:{uuid}",  # root device USN
        ]

        # Search targets we respond to
        self._search_targets = [
            "ssdp:all",
            "urn:schemas-upnp-org:device:MediaServer:1",
            "urn:schemas-upnp-org:service:ContentDirectory:1",
            "urn:schemas-upnp-org:service:ConnectionManager:1",
            f"uuid:{uuid}",
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Start the SSDP thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="ssdp-advertiser",
        )
        self._thread.start()
        # Give the socket a moment to bind
        time.sleep(0.3)
        # Send initial burst so already-online TVs discover us quickly
        self._send_notify_burst(3)

    def stop(self):
        """Send byebye NOTIFYs and stop the thread."""
        self._running = False
        self._send_byebye()

    # ------------------------------------------------------------------
    # Thread loop
    # ------------------------------------------------------------------

    def _run(self):
        """Main SSDP thread: listen for M-SEARCH, send periodic NOTIFYs."""
        self._sock = self._create_socket()
        if self._sock is None:
            return

        self._sock.settimeout(1.0)
        last_notify = 0

        while self._running:
            # Check if it's time to send a NOTIFY
            now = time.monotonic()
            if now - last_notify >= NOTIFY_INTERVAL:
                self._send_notify()
                last_notify = now

            # Listen for M-SEARCH
            try:
                data, addr = self._sock.recvfrom(4096)
                self._handle_msearch(data, addr)
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    time.sleep(0.1)
                continue

    # ------------------------------------------------------------------
    # Socket setup (Windows-compatible)
    # ------------------------------------------------------------------

    def _create_socket(self) -> Optional[socket.socket]:
        """Create and bind the UDP multicast socket."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # On Windows, we MUST bind to the specific interface IP, not ''
            sock.bind((self.local_ip, MULTICAST_PORT))

            # Join multicast group on this specific interface
            mreq = struct.pack(
                "4s4s",
                socket.inet_aton(MULTICAST_GROUP),
                socket.inet_aton(self.local_ip),
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

            # Allow multicast loopback (polite to other local listeners)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)

            return sock
        except OSError as e:
            print(f"  [ERROR] SSDP socket setup failed: {e}")
            print(f"  SSDP will not work. Check firewall settings.")
            return None

    # ------------------------------------------------------------------
    # NOTIFY messages
    # ------------------------------------------------------------------

    def _send_notify(self):
        """Send a full set of NOTIFY messages."""
        if self._sock is None:
            return
        for usn in self._usns:
            nt = _nt_from_usn(usn, self.uuid)
            msg = _build_notify(self.local_ip, self.port, nt, usn, "alive")
            try:
                self._sock.sendto(msg, (MULTICAST_GROUP, MULTICAST_PORT))
            except OSError:
                pass

    def _send_notify_burst(self, count: int):
        """Send multiple NOTIFY rounds in quick succession."""
        if self._sock is None:
            return
        for _ in range(count):
            self._send_notify()
            time.sleep(0.1)

    def _send_byebye(self):
        """Send byebye NOTIFYs multiple times for reliability."""
        if self._sock is None:
            return
        for _ in range(BYEBYE_COUNT):
            for usn in self._usns:
                nt = _nt_from_usn(usn, self.uuid)
                msg = _build_notify(self.local_ip, self.port, nt, usn, "byebye")
                try:
                    self._sock.sendto(msg, (MULTICAST_GROUP, MULTICAST_PORT))
                except OSError:
                    pass
            time.sleep(0.1)

    # ------------------------------------------------------------------
    # M-SEARCH response
    # ------------------------------------------------------------------

    def _handle_msearch(self, data: bytes, addr: tuple):
        """Parse incoming M-SEARCH and reply if relevant."""
        if not data.startswith(b"M-SEARCH"):
            return

        text = data.decode("utf-8", errors="replace")
        st_value = _extract_header(text, "ST")
        if not st_value:
            return

        # Only respond to search targets we care about
        if st_value not in self._search_targets:
            return

        # The response ST must match the request ST exactly
        # The response USN is the full device USN
        usn = f"uuid:{self.uuid}::urn:schemas-upnp-org:device:MediaServer:1"

        response = (
            "HTTP/1.1 200 OK\r\n"
            f"CACHE-CONTROL: max-age={SSDP_MAX_AGE}\r\n"
            f"DATE: {email.utils.formatdate(usegmt=True)}\r\n"
            "EXT:\r\n"
            f"LOCATION: http://{self.local_ip}:{self.port}/device.xml\r\n"
            f"SERVER: Windows/10 UPnP/1.0 ShareVideo/1.0\r\n"
            f"ST: {st_value}\r\n"
            f"USN: {usn}\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        )
        try:
            self._sock.sendto(response.encode("utf-8"), addr)
        except OSError:
            pass


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _build_notify(local_ip: str, port: int, nt: str, usn: str, nts: str) -> bytes:
    """Build a SSDP NOTIFY message."""
    location = f"http://{local_ip}:{port}/device.xml"
    msg = (
        "NOTIFY * HTTP/1.1\r\n"
        f"HOST: {MULTICAST_GROUP}:{MULTICAST_PORT}\r\n"
        f"CACHE-CONTROL: max-age={SSDP_MAX_AGE}\r\n"
        f"LOCATION: {location}\r\n"
        f"NT: {nt}\r\n"
        f"NTS: ssdp:{nts}\r\n"
        f"SERVER: Windows/10 UPnP/1.0 ShareVideo/1.0\r\n"
        f"USN: {usn}\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )
    return msg.encode("utf-8")


def _nt_from_usn(usn: str, uuid: str) -> str:
    """Derive the NT (Notification Type) from the USN."""
    if usn == f"uuid:{uuid}":
        return f"uuid:{uuid}"
    return usn.split("::", 1)[1] if "::" in usn else usn


def _extract_header(http_text: str, header_name: str) -> Optional[str]:
    """Extract a case-insensitive header value from HTTP-like text."""
    target = header_name.lower() + ":"
    for line in http_text.split("\r\n"):
        if line.lower().startswith(target):
            return line[len(target):].strip()
    return None

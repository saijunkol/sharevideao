"""SOAP handler for ContentDirectory and ConnectionManager services.

Handles incoming SOAP requests from DLNA clients (the TV) and returns
properly formatted SOAP responses.
"""

import xml.etree.ElementTree as ET
from typing import Optional
from xml.sax.saxutils import escape

from .media_store import MediaStore

# SOAP XML namespaces
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP_ENC = "http://schemas.xmlsoap.org/soap/encoding/"
CDS_NS = "urn:schemas-upnp-org:service:ContentDirectory:1"
CMS_NS = "urn:schemas-upnp-org:service:ConnectionManager:1"

# Supported MIME types for GetProtocolInfo
PROTOCOL_INFO_SOURCE = ",".join([
    "http-get:*:video/mp4:*",
    "http-get:*:video/x-matroska:*",
    "http-get:*:video/x-msvideo:*",
    "http-get:*:video/quicktime:*",
    "http-get:*:video/x-ms-wmv:*",
    "http-get:*:video/mpeg:*",
    "http-get:*:video/mp2t:*",
    "http-get:*:video/webm:*",
    "http-get:*:video/x-flv:*",
    "http-get:*:video/3gpp:*",
])


class SoapHandler:
    """Processes SOAP actions for ContentDirectory and ConnectionManager."""

    def __init__(self, media_store: MediaStore):
        self.media_store = media_store

    def handle(self, path: str, body: bytes, soap_action: str) -> str:
        """Route to the correct handler based on path and SOAPACTION header."""
        if path == "/cds/control":
            return self._handle_cds(body, soap_action)
        elif path == "/cms/control":
            return self._handle_cms(body, soap_action)
        else:
            return self._fault("401", "Invalid Action")

    # ------------------------------------------------------------------
    # ContentDirectory
    # ------------------------------------------------------------------

    def _handle_cds(self, body: bytes, soap_action: str) -> str:
        action = self._extract_action(soap_action)
        if action is None:
            return self._fault_cds("401", "Invalid Action")

        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return self._fault_cds("401", "Invalid Action")

        if action == "Browse":
            return self._handle_browse(root)
        elif action == "GetSearchCapabilities":
            return self._make_response(CDS_NS, "GetSearchCapabilities",
                                       {"SearchCaps": ""})
        elif action == "GetSortCapabilities":
            return self._make_response(CDS_NS, "GetSortCapabilities",
                                       {"SortCaps": "dc:title"})
        elif action == "GetSystemUpdateID":
            return self._make_response(CDS_NS, "GetSystemUpdateID",
                                       {"Id": str(self.media_store.update_id)})
        else:
            return self._fault_cds("401", "Invalid Action")

    def _handle_browse(self, root: ET.Element) -> str:
        # Extract Browse arguments from SOAP body
        object_id = "0"
        browse_flag = "BrowseDirectChildren"
        starting_index = 0
        requested_count = 0

        # Find the Browse element
        for elem in root.iter():
            tag = _local_name(elem.tag)
            if tag == "ObjectID":
                object_id = (elem.text or "0").strip()
            elif tag == "BrowseFlag":
                browse_flag = (elem.text or "BrowseDirectChildren").strip()
            elif tag == "StartingIndex":
                try:
                    starting_index = int(elem.text or "0")
                except (ValueError, TypeError):
                    starting_index = 0
            elif tag == "RequestedCount":
                try:
                    requested_count = int(elem.text or "0")
                except (ValueError, TypeError):
                    requested_count = 0

        # Perform browse
        didl, num_returned, total_matches = self.media_store.browse(
            object_id, browse_flag, starting_index, requested_count
        )

        return self._make_response(CDS_NS, "Browse", {
            "Result": escape(didl),   # DIDL-Lite XML must be XML-escaped inside Result
            "NumberReturned": str(num_returned),
            "TotalMatches": str(total_matches),
            "UpdateID": str(self.media_store.update_id),
        })

    # ------------------------------------------------------------------
    # ConnectionManager
    # ------------------------------------------------------------------

    def _handle_cms(self, body: bytes, soap_action: str) -> str:
        action = self._extract_action(soap_action)
        if action is None:
            return self._fault_cms("401", "Invalid Action")

        if action == "GetProtocolInfo":
            return self._make_response(CMS_NS, "GetProtocolInfo", {
                "Source": PROTOCOL_INFO_SOURCE,
                "Sink": "",
            })
        elif action == "GetCurrentConnectionIDs":
            return self._make_response(CMS_NS, "GetCurrentConnectionIDs", {
                "ConnectionIDs": "0",
            })
        elif action == "GetCurrentConnectionInfo":
            # Sony TVs usually call this with ConnectionID=0
            return self._make_response(CMS_NS, "GetCurrentConnectionInfo", {
                "RcsID": "-1",
                "AVTransportID": "-1",
                "ProtocolInfo": "",
                "PeerConnectionManager": "",
                "PeerConnectionID": "-1",
                "Direction": "Output",
                "Status": "OK",
            })
        else:
            return self._fault_cms("401", "Invalid Action")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_action(self, soap_action: str) -> Optional[str]:
        """Extract the action name from the SOAPACTION header.
        Format: "urn:schemas-upnp-org:service:ServiceName:1#ActionName"
        """
        if "#" in soap_action:
            return soap_action.split("#", 1)[1].strip('"').strip()
        # Try to extract from the last segment
        parts = soap_action.strip('"').split(":")
        if parts:
            return parts[-1]
        return None

    def _make_response(self, service_ns: str, action_name: str,
                       fields: dict[str, str]) -> str:
        """Build a SOAP response envelope."""
        args_xml = ""
        for name, value in fields.items():
            args_xml += f"<{name}>{_escape_val(value)}</{name}>"

        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="' + SOAP_NS + '" '
            's:encodingStyle="' + SOAP_ENC + '">'
            '<s:Body>'
            f'<u:{action_name}Response xmlns:u="{service_ns}">'
            f'{args_xml}'
            f'</u:{action_name}Response>'
            '</s:Body>'
            '</s:Envelope>'
        )

    def _fault_cds(self, code: str, description: str) -> str:
        return self._fault(CDS_NS, code, description)

    def _fault_cms(self, code: str, description: str) -> str:
        return self._fault(CMS_NS, code, description)

    def _fault(self, service_ns: str = CDS_NS, code: str = "401",
               description: str = "Invalid Action") -> str:
        """Build a SOAP fault response."""
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="' + SOAP_NS + '" '
            's:encodingStyle="' + SOAP_ENC + '">'
            '<s:Body>'
            '<s:Fault>'
            f'<faultcode>s:Client</faultcode>'
            f'<faultstring>UPnP Error {code}: {escape(description)}</faultstring>'
            '<detail>'
            f'<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
            f'<errorCode>{code}</errorCode>'
            f'<errorDescription>{escape(description)}</errorDescription>'
            f'</UPnPError>'
            '</detail>'
            '</s:Fault>'
            '</s:Body>'
            '</s:Envelope>'
        )


def _local_name(tag: str) -> str:
    """Extract the local name from a namespaced XML tag."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _escape_val(val: str) -> str:
    """Escape a value for XML text content."""
    return escape(val)

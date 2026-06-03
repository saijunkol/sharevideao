"""File scanning, DIDL-Lite XML generation, and media file serving with Range support."""

import base64
import os
from typing import Optional, Tuple
from xml.sax.saxutils import escape

# MIME type lookup by file extension
MIME_MAP = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".wmv": "video/x-ms-wmv",
    ".m4v": "video/mp4",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".ts": "video/mp2t",
    ".webm": "video/webm",
    ".flv": "video/x-flv",
    ".3gp": "video/3gpp",
    ".vob": "video/mpeg",
}

# DIDL-Lite XML namespaces
DIDL_HEADER = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
               'xmlns:dc="http://purl.org/dc/elements/1.1/" '
               'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
               'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">')
DIDL_FOOTER = '</DIDL-Lite>'


class FileEntry:
    """Represents a single file or folder in the media store."""

    def __init__(self, object_id: str, parent_id: str, title: str,
                 full_path: str, is_folder: bool, file_size: int = 0,
                 mime_type: str = "", mtime: float = 0):
        self.object_id = object_id
        self.parent_id = parent_id
        self.title = title
        self.full_path = full_path
        self.is_folder = is_folder
        self.file_size = file_size
        self.mime_type = mime_type
        self.mtime = mtime
        self.children: list[str] = []  # object_ids of children


class MediaStore:
    """Manages the virtual file tree, generates DIDL-Lite, and serves media bytes."""

    def __init__(self, local_ip: str, port: int):
        self.local_ip = local_ip
        self.port = port
        self.items: dict[str, FileEntry] = {}
        self.update_id = 0
        self.file_count = 0
        self.folder_count = 0
        # Root container
        root = FileEntry(
            object_id="0", parent_id="-1", title="Root",
            full_path="", is_folder=True
        )
        self.items["0"] = root

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def scan(self, shared_folders: list[str], allowed_extensions: list[str]):
        """Full rescan: rebuild the tree from configured shared folders."""
        # Reset
        self.items = {"0": self.items["0"]}
        self.items["0"].children = []
        self.file_count = 0
        self.folder_count = 0

        ext_set = set(e.lower() for e in allowed_extensions)

        for folder_path in shared_folders:
            folder_path = os.path.normpath(folder_path)
            if not os.path.isdir(folder_path):
                print(f"  [WARN] Folder not found, skipping: {folder_path}")
                continue

            folder_oid = _path_to_oid(folder_path)
            folder_entry = FileEntry(
                object_id=folder_oid, parent_id="0",
                title=os.path.basename(folder_path) or folder_path,
                full_path=folder_path, is_folder=True
            )
            self.items[folder_oid] = folder_entry
            self.items["0"].children.append(folder_oid)
            self.folder_count += 1

            self._scan_folder(folder_path, folder_oid, ext_set)

        self.update_id += 1
        print(f"  Scanned: {self.file_count} files, {self.folder_count} folders")

    def _scan_folder(self, folder_path: str, parent_oid: str,
                     ext_set: set[str]):
        """Recursively scan a folder, adding entries to self.items."""
        try:
            entries = sorted(os.scandir(folder_path),
                             key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            print(f"  [WARN] Permission denied: {folder_path}")
            return
        except OSError as e:
            print(f"  [WARN] Cannot read {folder_path}: {e}")
            return

        parent = self.items[parent_oid]

        for entry in entries:
            if entry.name.startswith('.'):
                continue  # skip hidden files/folders

            full = os.path.normpath(entry.path)

            if entry.is_dir():
                try:
                    # Skip junctions/symlinks to avoid cycles
                    if _is_junction_or_symlink(full):
                        continue
                except OSError:
                    continue

                oid = _path_to_oid(full)
                fe = FileEntry(
                    object_id=oid, parent_id=parent_oid,
                    title=entry.name, full_path=full,
                    is_folder=True
                )
                self.items[oid] = fe
                parent.children.append(oid)
                self.folder_count += 1
                self._scan_folder(full, oid, ext_set)

            elif entry.is_file():
                ext = os.path.splitext(entry.name)[1].lower()
                if ext not in ext_set:
                    continue

                try:
                    stat = entry.stat()
                    size = stat.st_size
                    mtime = stat.st_mtime
                except OSError:
                    size = 0
                    mtime = 0

                oid = _path_to_oid(full)
                fe = FileEntry(
                    object_id=oid, parent_id=parent_oid,
                    title=entry.name, full_path=full,
                    is_folder=False, file_size=size,
                    mime_type=MIME_MAP.get(ext, "application/octet-stream"),
                    mtime=mtime
                )
                self.items[oid] = fe
                parent.children.append(oid)
                self.file_count += 1

    # ------------------------------------------------------------------
    # Browse (DIDL-Lite)
    # ------------------------------------------------------------------

    def browse(self, object_id: str, browse_flag: str,
               starting_index: int, requested_count: int
               ) -> Tuple[str, int, int]:
        """Return (didl_lite_xml, number_returned, total_matches)."""
        entry = self.items.get(object_id)
        if entry is None:
            return (f"{DIDL_HEADER}{DIDL_FOOTER}", 0, 0)

        if browse_flag == "BrowseMetadata":
            xml = self._didl_item(entry)
            return (f"{DIDL_HEADER}{xml}{DIDL_FOOTER}", 1, 1)

        if browse_flag == "BrowseDirectChildren":
            if not entry.is_folder:
                return (f"{DIDL_HEADER}{DIDL_FOOTER}", 0, 0)

            children = entry.children
            total = len(children)

            if starting_index >= total:
                return (f"{DIDL_HEADER}{DIDL_FOOTER}", 0, total)

            if requested_count == 0:
                requested_count = total

            end = min(starting_index + requested_count, total)
            slice_ids = children[starting_index:end]

            items_xml = []
            for cid in slice_ids:
                child = self.items.get(cid)
                if child:
                    items_xml.append(self._didl_item(child))

            xml = f"{DIDL_HEADER}{''.join(items_xml)}{DIDL_FOOTER}"
            return (xml, len(items_xml), total)

        return (f"{DIDL_HEADER}{DIDL_FOOTER}", 0, 0)

    def _didl_item(self, entry: FileEntry) -> str:
        if entry.is_folder:
            return (
                f'<container id="{entry.object_id}" '
                f'parentID="{entry.parent_id}" restricted="1" '
                f'childCount="{len(entry.children)}">'
                f'<dc:title>{escape(entry.title)}</dc:title>'
                f'<upnp:class>object.container.storageFolder</upnp:class>'
                f'</container>'
            )
        else:
            url = f"http://{self.local_ip}:{self.port}/media/{entry.object_id}"
            # Sony TVs work well with the simplified protocolInfo format
            proto = f"http-get:*:{entry.mime_type}:*"
            return (
                f'<item id="{entry.object_id}" '
                f'parentID="{entry.parent_id}" restricted="1">'
                f'<dc:title>{escape(entry.title)}</dc:title>'
                f'<upnp:class>object.item.videoItem</upnp:class>'
                f'<res protocolInfo="{proto}" size="{entry.file_size}">'
                f'{escape(url)}</res>'
                f'</item>'
            )

    # ------------------------------------------------------------------
    # Media serving with Range support
    # ------------------------------------------------------------------

    def serve_file(self, media_id: str, range_header: Optional[str]
                   ) -> Tuple[int, dict, object]:
        """Return (status_code, headers_dict, body).
        body can be bytes or a file-like object.
        """
        entry = self.items.get(media_id)
        if entry is None or entry.is_folder:
            return (404, {"Content-Type": "text/plain"}, b"Not Found")

        try:
            f = open(entry.full_path, "rb")
        except (OSError, PermissionError):
            return (404, {"Content-Type": "text/plain"}, b"Cannot open file")

        file_size = entry.file_size
        if file_size == 0:
            try:
                f.seek(0, 2)
                file_size = f.tell()
            except OSError:
                pass

        headers = {
            "Content-Type": entry.mime_type,
            "Accept-Ranges": "bytes",
        }

        if range_header:
            try:
                start, end = _parse_range(range_header, file_size)
            except ValueError:
                f.close()
                return (400, {"Content-Type": "text/plain"}, b"Bad Range header")

            try:
                f.seek(start)
                data = f.read(end - start + 1)
            except OSError:
                f.close()
                return (500, {"Content-Type": "text/plain"}, b"Read error")
            finally:
                f.close()

            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            headers["Content-Length"] = str(len(data))
            return (206, headers, data)
        else:
            # No Range header — stream whole file in chunks
            headers["Content-Length"] = str(file_size)
            return (200, headers, f)

    def get_folder_info(self) -> list[dict]:
        """Return info about root-level folders for the web UI."""
        result = []
        root = self.items["0"]
        for oid in root.children:
            entry = self.items.get(oid)
            if entry:
                result.append({
                    "path": entry.full_path,
                    "title": entry.title,
                    "files": self._count_files_recursive(entry),
                })
        return result

    def _count_files_recursive(self, entry: FileEntry) -> int:
        if not entry.is_folder:
            return 1
        count = 0
        for cid in entry.children:
            child = self.items.get(cid)
            if child:
                count += self._count_files_recursive(child)
        return count


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _path_to_oid(path: str) -> str:
    """Encode a filesystem path as a stable, URL-safe object ID."""
    # Normalize separators
    normalized = path.replace("\\", "/")
    return base64.urlsafe_b64encode(
        normalized.encode("utf-8")
    ).decode("ascii").rstrip("=")


def _parse_range(range_header: str, file_size: int) -> Tuple[int, int]:
    """Parse 'bytes=N-M' header. Returns (start, end) inclusive."""
    prefix, _, value = range_header.partition("=")
    if prefix.strip() != "bytes":
        raise ValueError("Not a bytes range")

    start_str, _, end_str = value.partition("-")
    start_str = start_str.strip()
    end_str = end_str.strip()

    if start_str:
        start = int(start_str)
    else:
        start = 0

    if end_str:
        end = int(end_str)
    else:
        end = file_size - 1

    if start > end or start >= file_size:
        raise ValueError("Unsatisfiable range")

    return (start, min(end, file_size - 1))


def _is_junction_or_symlink(path: str) -> bool:
    """Check if a path is a Windows junction or symlink."""
    try:
        return os.path.islink(path)
    except (OSError, NotImplementedError):
        # On older Windows, os.path.islink may not exist or work
        return False

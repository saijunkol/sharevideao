"""Flask web management UI for ShareVideo DLNA server.

Provides a simple single-page interface to manage shared folders,
view server status, and trigger rescans.
"""

import os

from flask import Flask, jsonify, render_template, request

from dlna.media_store import MediaStore


def create_app(media_store: MediaStore, config_manager) -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/status")
    def status():
        return jsonify({
            "name": config_manager.get("server_name", "ShareVideo"),
            "port": config_manager.get("port", 8000),
            "web_port": config_manager.get("web_port", 8080),
            "uuid": config_manager.get("uuid", ""),
            "file_count": media_store.file_count,
            "folder_count": media_store.folder_count,
            "shared_folder_count": len(config_manager.get("shared_folders", [])),
            "allowed_extensions": config_manager.get("allowed_extensions", []),
        })

    @app.route("/api/folders")
    def get_folders():
        folders = config_manager.get("shared_folders", [])
        folder_infos = media_store.get_folder_info()
        # Merge: folder_infos is indexed by full_path
        info_by_path = {fi["path"]: fi for fi in folder_infos}
        result = []
        for path in folders:
            info = info_by_path.get(path, {"path": path, "title": os.path.basename(path), "files": 0})
            result.append({"path": path, "name": os.path.basename(path), "files": info["files"]})
        return jsonify(result)

    @app.route("/api/folders", methods=["POST"])
    def add_folder():
        data = request.get_json()
        if not data or "path" not in data:
            return jsonify({"error": "Missing 'path' field"}), 400

        folder_path = os.path.normpath(data["path"].strip())
        if not os.path.isdir(folder_path):
            return jsonify({"error": f"Folder not found: {folder_path}"}), 400

        folders = config_manager.get("shared_folders", [])
        if folder_path in folders:
            return jsonify({"error": "Folder already added"}), 400

        folders.append(folder_path)
        config_manager.set("shared_folders", folders)

        # Trigger rescan
        _do_rescan(media_store, config_manager)

        return jsonify({"success": True, "path": folder_path})

    @app.route("/api/folders", methods=["DELETE"])
    def remove_folder():
        path = request.args.get("path", "")
        if not path:
            # Try reading from JSON body
            data = request.get_json(silent=True) or {}
            path = data.get("path", "")

        path = os.path.normpath(path)
        folders = config_manager.get("shared_folders", [])
        if path not in folders:
            return jsonify({"error": "Folder not in list"}), 404

        folders.remove(path)
        config_manager.set("shared_folders", folders)

        # Trigger rescan
        _do_rescan(media_store, config_manager)

        return jsonify({"success": True})

    @app.route("/api/rescan", methods=["POST"])
    def rescan():
        _do_rescan(media_store, config_manager)
        return jsonify({"success": True, "file_count": media_store.file_count})

    return app


def run_flask(app: Flask, host: str, port: int):
    """Run the Flask dev server in a thread."""
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


def _do_rescan(media_store: MediaStore, config_manager):
    """Re-scan all shared folders."""
    folders = config_manager.get("shared_folders", [])
    extensions = config_manager.get("allowed_extensions", [])
    media_store.scan(folders, extensions)

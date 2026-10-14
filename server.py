import http.server
import socketserver
import json
import os
import sys
import webbrowser
import urllib.parse
import stat
import shutil
import hashlib
import re
import subprocess

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PORT = 8085
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = "GreenFile – Code Green for Your Storage.html"
TMP_REGEX = re.compile(r'(\.(tmp|temp|log|bak|crdownload|cache)$|^~|^\.DS_Store)', re.IGNORECASE)

def get_quick_folders():
    home = os.path.expanduser("~")
    folders = []
    
    # User Home Directory
    if os.path.isdir(home):
        folders.append({"name": "User Profile (Entire Home)", "path": os.path.normpath(home), "icon": "🏠"})

    # Downloads folder
    d = os.path.join(home, "Downloads")
    if os.path.isdir(d):
        folders.append({"name": "Downloads", "path": os.path.normpath(d), "icon": "📥"})
    
    # Documents folder
    doc = os.path.join(home, "Documents")
    if os.path.isdir(doc):
        folders.append({"name": "Documents", "path": os.path.normpath(doc), "icon": "📄"})
        
    # Desktop folder
    dt = os.path.join(home, "Desktop")
    if os.path.isdir(dt):
        folders.append({"name": "Desktop", "path": os.path.normpath(dt), "icon": "🖥️"})
        
    # Current project directory
    folders.append({"name": "GreenFile Project", "path": os.path.normpath(PROJECT_DIR), "icon": "🌱"})
    return folders

def pick_folder_native():
    """Opens a native Windows folder picker dialog in foreground and returns the selected path."""
    # Method 1: PowerShell FolderBrowserDialog with TopMost Form owner
    try:
        ps_cmd = (
            "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
            "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$f.Description = 'GreenFile - Select Folder to Scan and Clean'; "
            "$f.ShowNewFolderButton = $false; "
            "$top = New-Object System.Windows.Forms.Form; "
            "$top.TopMost = $true; "
            "if ($f.ShowDialog($top) -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $f.SelectedPath }"
        )
        res = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps_cmd], capture_output=True, text=True, timeout=60)
        out = res.stdout.strip()
        if out and os.path.isdir(out):
            return os.path.normpath(out)
    except Exception as err:
        print(f"[pick_folder] PowerShell dialog error: {err}")

    # Method 2: Tkinter fallback with topmost window
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes('-topmost', 1)
        root.focus_force()
        folder = filedialog.askdirectory(title="GreenFile - Select Folder to Scan and Clean")
        root.destroy()
        if folder and os.path.isdir(folder):
            return os.path.normpath(folder)
    except Exception as err:
        print(f"[pick_folder] Tkinter dialog error: {err}")

    return None

def score_folder_originality(dir_name, mtime):
    """Scores a folder name to determine whether it is the original or a copy."""
    score = 0
    name_lower = dir_name.lower()
    if re.search(r'[-_\s]copy\b', name_lower):
        score -= 100
    if re.search(r'\(\d+\)', name_lower):
        score -= 80
    if re.search(r'^copy\s+(?:of\s+)?', name_lower):
        score -= 100
    if 'backup' in name_lower:
        score -= 40
    if 'temp' in name_lower or 'tmp' in name_lower:
        score -= 50
    # Shorter names preferred
    score -= len(dir_name)
    return score

def detect_folders_and_duplicates(base_dir, file_list):
    """Detects duplicate folders, folder statistics, and empty directories."""
    dir_files_map = {}
    all_dirs_set = set()
    
    ignore_dirs = {".git", ".gemini", "node_modules", "__pycache__", ".vscode", "$RECYCLE.BIN", "System Volume Information"}
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith('.')]
        for d in dirs:
            all_dirs_set.add(os.path.normpath(os.path.join(root, d)))
            
    for f in file_list:
        dir_path = os.path.normpath(os.path.dirname(f["path"]))
        cur = dir_path
        while cur and cur != base_dir and cur.startswith(base_dir):
            dir_files_map.setdefault(cur, []).append(f)
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent

    empty_folders = []
    for d in sorted(all_dirs_set):
        files_in_dir = dir_files_map.get(d, [])
        if len(files_in_dir) == 0:
            rel = os.path.relpath(d, base_dir).replace('\\', '/')
            empty_folders.append({
                "path": d,
                "relPath": rel,
                "name": os.path.basename(d)
            })

    # Group directories by content signature: sorted list of (rel_subpath, size, hash)
    dir_sigs = {}
    for d, files in dir_files_map.items():
        if not files:
            continue
        sub_items = []
        total_size = 0
        for f in files:
            rel_sub = os.path.relpath(f["path"], d).replace('\\', '/')
            h = f.get("hash") or f"{f['size']}_{f['name']}"
            sub_items.append((rel_sub, f["size"], h))
            total_size += f["size"]
            
        sub_items.sort(key=lambda x: x[0])
        sig_str = ";".join(f"{item[0]}:{item[1]}:{item[2]}" for item in sub_items)
        sig_hash = hashlib.sha256(sig_str.encode('utf-8')).hexdigest()
        
        mtime = 0
        try:
            mtime = int(os.stat(d).st_mtime * 1000)
        except Exception:
            pass

        dir_sigs.setdefault(sig_hash, []).append({
            "path": d,
            "relPath": os.path.relpath(d, base_dir).replace('\\', '/'),
            "name": os.path.basename(d),
            "fileCount": len(files),
            "totalSize": total_size,
            "mtime": mtime
        })

    duplicate_folders = []
    dup_group_id = 0
    for sig_hash, d_list in dir_sigs.items():
        if len(d_list) < 2:
            continue
        # Filter out parent-child relationships
        filtered = []
        for i, d1 in enumerate(d_list):
            is_parent = False
            for j, d2 in enumerate(d_list):
                if i != j and d2["path"].startswith(d1["path"] + os.sep):
                    is_parent = True
                    break
            if not is_parent:
                filtered.append(d1)
                
        if len(filtered) < 2:
            continue
            
        filtered.sort(key=lambda x: (score_folder_originality(x["name"], x["mtime"]), -x["mtime"]), reverse=True)
        original = filtered[0]
        duplicates = filtered[1:]
        
        dup_group_id += 1
        duplicate_folders.append({
            "id": f"df_{dup_group_id}",
            "original": original,
            "duplicates": duplicates,
            "fileCount": original["fileCount"],
            "size": original["totalSize"],
            "recoverableSize": original["totalSize"] * len(duplicates)
        })

    # Filter out duplicate folders that are subfolders of already identified top-level duplicate folders
    duplicate_folders.sort(key=lambda x: len(x["original"]["path"]))
    top_duplicate_folders = []
    for df in duplicate_folders:
        orig_p = df["original"]["path"]
        if not any(orig_p.startswith(top["original"]["path"] + os.sep) for top in top_duplicate_folders):
            top_duplicate_folders.append(df)

    return top_duplicate_folders, empty_folders

def detect_name_matches(file_list):
    """Detects files that share identical or similar base names across different locations."""
    name_groups = {}
    for f in file_list:
        base_name, ext = os.path.splitext(f["name"])
        clean_base = re.sub(r'[-_\s]+(?:copy|\(\d+\))(?:\s*\(\d+\))?$', '', base_name, flags=re.IGNORECASE).strip().lower()
        key = clean_base + ext.lower()
        name_groups.setdefault(key, []).append(f)
        
    results = []
    gid = 0
    for key, group in name_groups.items():
        if len(group) > 1:
            # Only include if not all sharing the exact same hash (those are already hash duplicates)
            hashes = {f.get("hash") for f in group if f.get("hash")}
            if len(hashes) > 1 or len(hashes) == 0:
                gid += 1
                results.append({
                    "id": f"nm_{gid}",
                    "name": key,
                    "files": group
                })
    return results

def scan_directory(base_dir):
    """Scans a directory on disk, calculates file metadata, and identifies duplicate files & folders."""
    base_dir = os.path.normpath(os.path.abspath(base_dir))
    if not os.path.isdir(base_dir):
        return {"success": False, "error": f"Folder not found: {base_dir}"}
        
    file_list = []
    size_map = {}
    
    ignore_dirs = {".git", ".gemini", "node_modules", "__pycache__", ".vscode", "$RECYCLE.BIN", "System Volume Information"}
    
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith('.')]
        
        for name in files:
            full_path = os.path.normpath(os.path.join(root, name))
            try:
                st = os.stat(full_path)
                sz = st.st_size
                mtime = int(st.st_mtime * 1000)
                is_tmp = bool(TMP_REGEX.search(name))
                rel_path = os.path.relpath(full_path, base_dir).replace('\\', '/')
                safe_id = hashlib.md5(full_path.encode('utf-8', errors='replace')).hexdigest()[:16]
                
                f_item = {
                    "name": name,
                    "path": full_path,
                    "relPath": rel_path,
                    "size": sz,
                    "mod": mtime,
                    "tmp": is_tmp,
                    "id": f"f_{safe_id}"
                }
                file_list.append(f_item)
                size_map.setdefault(sz, []).append(f_item)
            except Exception:
                continue

    # Hash duplicate candidates (files sharing exact same byte size)
    for sz, candidates in size_map.items():
        if len(candidates) > 1:
            for item in candidates:
                if sz == 0:
                    item["hash"] = "empty_file_0"
                    continue
                try:
                    p = item["path"]
                    h = hashlib.sha256()
                    if sz <= 256 * 1024 * 1024:
                        with open(p, "rb") as f:
                            while chunk := f.read(1024 * 1024):
                                h.update(chunk)
                    else:
                        with open(p, "rb") as f:
                            h.update(f.read(4 * 1024 * 1024))
                            if sz > 8 * 1024 * 1024:
                                f.seek(sz - 4 * 1024 * 1024)
                                h.update(f.read(4 * 1024 * 1024))
                    item["hash"] = h.hexdigest() + str(sz)
                except Exception as e:
                    print(f"Hash error for {item['path']}: {e}")

    dup_folders, empty_folders = detect_folders_and_duplicates(base_dir, file_list)
    name_matches = detect_name_matches(file_list)

    return {
        "success": True,
        "folder": base_dir,
        "files": file_list,
        "duplicateFolders": dup_folders,
        "emptyFolders": empty_folders,
        "nameMatches": name_matches
    }

def force_delete_file(target_path, base_dir=None):
    """Permanently deletes a file or directory from disk and verifies deletion."""
    full_path = target_path
    if base_dir and not os.path.isabs(full_path):
        full_path = os.path.join(base_dir, full_path)
    full_path = os.path.normpath(os.path.abspath(full_path))

    if not os.path.exists(full_path):
        home = os.path.expanduser("~")
        candidates = []
        if base_dir:
            candidates.append(os.path.normpath(os.path.join(home, "Downloads", base_dir, target_path)))
            candidates.append(os.path.normpath(os.path.join(home, "Desktop", base_dir, target_path)))
            candidates.append(os.path.normpath(os.path.join(home, "Documents", base_dir, target_path)))
            candidates.append(os.path.normpath(os.path.join(home, base_dir, target_path)))
            candidates.append(os.path.normpath(os.path.join(PROJECT_DIR, base_dir, target_path)))

        candidates.append(os.path.normpath(os.path.join(home, "Downloads", target_path)))
        candidates.append(os.path.normpath(os.path.join(home, "Desktop", target_path)))
        candidates.append(os.path.normpath(os.path.join(home, "Documents", target_path)))
        candidates.append(os.path.normpath(os.path.join(home, target_path)))
        candidates.append(os.path.normpath(os.path.join(PROJECT_DIR, target_path)))

        for c in candidates:
            if os.path.exists(c):
                full_path = c
                break

    if not os.path.exists(full_path):
        return False, f"Path does not exist on disk: {target_path}"

    # Step 1: Remove read-only attributes recursively
    try:
        if os.path.isdir(full_path):
            for root, dirs, files in os.walk(full_path):
                for fname in files:
                    p = os.path.join(root, fname)
                    try:
                        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
                    except Exception:
                        pass
        else:
            os.chmod(full_path, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass

    # Step 2: Attempt standard Python removal
    try:
        if os.path.isdir(full_path):
            shutil.rmtree(full_path, ignore_errors=False)
        else:
            os.remove(full_path)
    except Exception as err:
        print(f"[delete] Standard remove failed for {full_path}: {err}. Trying PowerShell force remove...")

    # Step 3: If still present, attempt PowerShell force removal
    if os.path.exists(full_path):
        try:
            ps_cmd = f'Remove-Item -LiteralPath "{full_path}" -Force -Recurse -ErrorAction SilentlyContinue'
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, timeout=15)
        except Exception as ps_err:
            print(f"[delete] PowerShell fallback error: {ps_err}")

    # Step 4: Strict verification that path is gone
    if os.path.exists(full_path):
        return False, "Item could not be removed (locked by system, process, or insufficient permissions)"

    print(f"[delete] Successfully and permanently deleted: {full_path}")
    return True, ""

class GreenFileHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PROJECT_DIR, **kwargs)

    def log_message(self, format, *args):
        # Keep logs clean and concise
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def safe_send_json(self, data, status=200):
        try:
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    def safe_send_bytes(self, body, content_type="text/html; charset=utf-8", status=200):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    def do_OPTIONS(self):
        try:
            self.send_response(200)
            self.send_cors_headers()
            self.end_headers()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            try:
                html_path = os.path.join(PROJECT_DIR, HTML_FILE)
                with open(html_path, "rb") as f:
                    content = f.read()
                self.safe_send_bytes(content, "text/html; charset=utf-8", 200)
            except Exception as err:
                self.safe_send_bytes(f"Error loading HTML: {err}".encode("utf-8"), "text/plain", 500)
            return
            
        elif path == "/favicon.ico":
            try:
                self.send_response(204)
                self.end_headers()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                pass
            return

        elif path == "/api/status":
            data = {
                "ok": True,
                "server": "GreenFile Native Deletion Engine",
                "version": "2.0",
                "projectDir": PROJECT_DIR,
                "quickFolders": get_quick_folders()
            }
            self.safe_send_json(data, 200)
            return

        elif path == "/api/quick-folders":
            self.safe_send_json({"ok": True, "folders": get_quick_folders()}, 200)
            return

        elif path == "/api/browse":
            folder = pick_folder_native()
            if folder:
                self.safe_send_json({"ok": True, "folder": folder}, 200)
            else:
                self.safe_send_json({"ok": False, "cancelled": True}, 200)
            return

        elif path == "/api/scan":
            params = urllib.parse.parse_qs(parsed.query)
            folder = params.get("folder", [""])[0]
            if not folder:
                folder = PROJECT_DIR
            result = scan_directory(folder)
            self.safe_send_json(result, 200 if result.get("success") else 400)
            return

        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/scan":
            content_length = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_length)
            try:
                data = json.loads(post_body.decode("utf-8")) if post_body else {}
                folder = data.get("folder") or data.get("path") or PROJECT_DIR
                result = scan_directory(folder)
                self.safe_send_json(result, 200 if result.get("success") else 400)
            except Exception as err:
                self.safe_send_json({"success": False, "error": str(err)}, 400)
            return

        elif path == "/api/delete":
            content_length = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_length)
            try:
                data = json.loads(post_body.decode("utf-8"))
                files = data.get("files", [])
                base_dir = data.get("baseDir", "")
                
                deleted = []
                failed = []
                
                for item in files:
                    target_path = item if isinstance(item, str) else item.get("path", "")
                    if not target_path:
                        continue
                    
                    ok, err = force_delete_file(target_path, base_dir)
                    if ok:
                        deleted.append(target_path)
                    else:
                        failed.append({"path": target_path, "error": err})

                response = {
                    "success": True,
                    "deletedCount": len(deleted),
                    "deleted": deleted,
                    "failed": failed
                }
                self.safe_send_json(response, 200)
            except Exception as err:
                self.safe_send_json({"success": False, "error": str(err)}, 400)
            return

        self.send_response(404)
        self.end_headers()

class ThreadedGreenFileServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def run():
    global PORT
    handler = GreenFileHandler
    server = None
    
    for p in range(PORT, PORT + 20):
        try:
            server = ThreadedGreenFileServer(("127.0.0.1", p), handler)
            PORT = p
            break
        except OSError:
            continue
            
    if not server:
        print("Could not bind to any port from 8085-8105")
        sys.exit(1)

    url = f"http://127.0.0.1:{PORT}/"
    print("=" * 55)
    print("  🌱 GreenFile Storage Cleaner & Sustainability Engine")
    print(f"  Local Server: {url}")
    print("  Direct Disk Deletion API: ENABLED & ACTIVE")
    print("  Multi-threaded Engine: READY")
    print("=" * 55)
    
    try:
        webbrowser.open(url)
    except Exception:
        pass
        
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping GreenFile server...")
        server.server_close()

if __name__ == "__main__":
    run()

"""Automated history and session sharing across rotated Claude Code accounts."""
import os
import shutil
import sys
import subprocess
from . import paths, registry

def create_junction_or_symlink(link_path, target_path):
    # Ensure targets are absolute
    link_path = os.path.abspath(link_path)
    target_path = os.path.abspath(target_path)
    
    # Remove existing link or directory if it exists
    if os.path.exists(link_path) or os.path.islink(link_path):
        if os.path.isdir(link_path) and not os.path.islink(link_path):
            try:
                os.rmdir(link_path)
            except OSError:
                shutil.rmtree(link_path)
        else:
            try:
                os.unlink(link_path)
            except OSError:
                shutil.rmtree(link_path)
                
    # Create the junction/symlink
    if sys.platform == "win32":
        # On Windows, use powershell New-Item Junction (does not require admin privileges)
        subprocess.run(
            ["powershell", "-Command", f"New-Item -ItemType Junction -Path '{link_path}' -Value '{target_path}'"],
            capture_output=True, text=True, check=True
        )
    else:
        os.symlink(target_path, link_path)

def merge_directories(src, dst):
    if not os.path.exists(src):
        return
    os.makedirs(dst, exist_ok=True)
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if os.path.isdir(s):
            if os.path.islink(s):
                continue
            merge_directories(s, d)
        else:
            if not os.path.exists(d):
                shutil.copy2(s, d)

def cmd_share_history():
    accounts = registry.accounts()
    claude_accounts = [acc for acc in accounts if acc.get("provider") == "claude"]
    if not claude_accounts:
        print("No connected Claude accounts found in the registry.", file=sys.stderr)
        return 1
        
    shared_base = os.path.join(paths.base_dir(), "shared_claude_state")
    os.makedirs(shared_base, exist_ok=True)
    
    subdirs = ["sessions", "projects", "backups"]
    for subdir in subdirs:
        os.makedirs(os.path.join(shared_base, subdir), exist_ok=True)
        
    print(f"Initializing shared Claude Code state at: {shared_base}")
    
    for account in claude_accounts:
        home = account["home"]
        name = account["name"]
        print(f"\nProcessing account: {name} (home: {home})")
        
        for subdir in subdirs:
            local_path = os.path.join(home, subdir)
            shared_path = os.path.join(shared_base, subdir)
            
            # Check if it is already linked
            is_linked = os.path.islink(local_path)
            if not is_linked and sys.platform == "win32" and os.path.exists(local_path):
                # Check link type on Windows
                proc = subprocess.run(
                    ["powershell", "-Command", f"(Get-Item '{local_path}').LinkType"],
                    capture_output=True, text=True
                )
                if "Junction" in proc.stdout or "SymbolicLink" in proc.stdout:
                    is_linked = True
                    
            if is_linked:
                print(f"  - {subdir}: already shared/linked")
                continue
                
            # If directory exists, merge it into shared first so we don't lose data
            if os.path.exists(local_path):
                print(f"  - {subdir}: merging existing local data to shared...")
                merge_directories(local_path, shared_path)
                
            # Create link
            print(f"  - {subdir}: linking to shared...")
            try:
                create_junction_or_symlink(local_path, shared_path)
                print(f"    [OK] Linked successfully")
            except Exception as e:
                print(f"    [ERROR] Failed to link: {e}", file=sys.stderr)
                
    print("\n[OK] Shared state setup completed. All Claude accounts now share the same history, sessions, and memory!")
    return 0

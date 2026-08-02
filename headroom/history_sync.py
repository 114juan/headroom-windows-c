"""Automated history and session sharing across rotated Claude Code accounts."""
import os
import re
import shutil
import sys
import subprocess
from . import paths, registry

# directories that hold conversation/session state worth sharing between
# accounts; "skills" is included so /rotator (and any personal skill) is
# available no matter which account a session lands on
SHARED_SUBDIRS = ["sessions", "projects", "backups", "skills"]

def is_junction_or_symlink(path):
    if os.path.islink(path):
        return True
    if sys.platform == "win32" and os.path.exists(path):
        try:
            import stat
            s = os.lstat(path)
            tag = getattr(s, "st_reparse_tag", 0)
            if tag in (stat.IO_REPARSE_TAG_MOUNT_POINT, stat.IO_REPARSE_TAG_SYMLINK):
                return True
        except OSError:
            pass
    return False

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
        # On Windows, use cmd built-in mklink /j (does not require admin privileges)
        # To avoid cmd.exe quote-stripping bugs, we use cmd.exe /s /c and wrap in outer quotes.
        if '"' in link_path or '"' in target_path:
            raise ValueError("Paths cannot contain double quotes.")
        cmd_str = f'cmd.exe /s /c "mklink /j "{link_path}" "{target_path}""'
        subprocess.run(
            cmd_str,
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

def project_slug(cwd):
    """Claude Code stores each project's transcripts under
    ``projects/<slug>`` where the slug is the project's absolute path with
    every non-alphanumeric character replaced by ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd))


def sync_project(src_home, dst_home, cwd=None):
    """Carry the current project's conversation transcripts from ``src_home``
    to ``dst_home`` so ``claude --continue`` on the destination resumes the
    exact same conversation after a rotation.

    Returns ``"shared"`` when both homes already point at the same physical
    projects dir (share-history junction), the number of files copied when a
    sync happened, or ``None`` when the source has no transcripts for this
    project (nothing to carry)."""
    cwd = cwd or os.getcwd()
    slug = project_slug(cwd)
    src = os.path.join(src_home, "projects", slug)
    dst = os.path.join(dst_home, "projects", slug)
    if not os.path.isdir(src):
        return None
    if os.path.normcase(os.path.realpath(src)) == \
            os.path.normcase(os.path.realpath(dst)):
        return "shared"
    copied = 0
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_root = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_root, exist_ok=True)
        for name in files:
            source_file = os.path.join(root, name)
            target_file = os.path.join(target_root, name)
            try:
                # copy-if-newer: never clobber a transcript that advanced on
                # the destination since the last rotation
                if not os.path.exists(target_file) or \
                        os.path.getmtime(source_file) > os.path.getmtime(target_file):
                    shutil.copy2(source_file, target_file)
                    copied += 1
            except OSError:
                pass
    return copied


def cmd_share_history():
    accounts = registry.accounts()
    claude_accounts = [acc for acc in accounts if acc.get("provider") == "claude"]
    if not claude_accounts:
        print("No connected Claude accounts found in the registry.", file=sys.stderr)
        return 1

    shared_base = os.path.join(paths.base_dir(), "shared_claude_state")
    os.makedirs(shared_base, exist_ok=True)

    subdirs = SHARED_SUBDIRS
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
            is_linked = is_junction_or_symlink(local_path)
                    
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

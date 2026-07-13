# Windows Support, Context Transfer, and Enhancements for headroom

This fork adds full cross-platform compatibility for Windows, automated conversation context transfer between rotated accounts, and deep integration with the **graphify** codebase mapping utility.

---

## 🚀 Key Enhancements

### 1. Native Windows Support
Originally, `headroom` was limited to Unix-based systems (macOS and Linux) because it relied on the Unix-exclusive python module `fcntl` for config and database locking.
* We created a cross-platform compatibility wrapper in [headroom/fcntl_compat.py](file:///D:/Users/Documents/Projects/2026/gestion%20acounts/headroom/fcntl_compat.py) that maps file locks to the Windows native `msvcrt.locking` API.
* All configuration locks, collector execution locks, and cooldown registers now work natively on Windows.

### 2. Windows Installer and Command Launchers
We created a PowerShell installer script [install.ps1](file:///D:/Users/Documents/Projects/2026/gestion%20acounts/install.ps1) for Windows users that:
* Verifies Python 3.9+ environment.
* Installs/upgrades the `graphifyy` python package.
* Generates native Windows launchers (`headroom.cmd` and `headroom.ps1`) in the local bin directory (`~/.local/bin/`).
* Provides instructions to add the bin directory to the User PATH.

### 3. Windows-Friendly Shell Commands on Rotation
When rotating accounts, `headroom rotate` now detects the host operating system and outputs appropriate commands:
* **Windows (PowerShell):** `$env:CLAUDE_CONFIG_DIR='...'`
* **Windows (CMD):** `set CLAUDE_CONFIG_DIR=...`
* **Unix (Bash/Zsh):** `export CLAUDE_CONFIG_DIR=...`

### 4. Automated Context Transfer (Mass Context Management)
To solve the issue of losing conversation history when rotating accounts (which forces new sessions to start cold), we implemented automated context summary extraction:
* During `headroom rotate`, the tool looks inside the current account's configuration directory to find the most recently modified Claude Code or Codex session.
* It automatically extracts Claude Code's auto-generated background summary (`summary.md`) or parses the last few messages of the session's `.jsonl` file.
* It prints this context summary in a structured block.
* We updated the Claude Code rotator skill ([integrations/claude-code/skills/rotator/SKILL.md](file:///D:/Users/Documents/Projects/2026/gestion%20acounts/integrations/claude-code/skills/rotator/SKILL.md)) so the developer agent reads this summary and automatically resumes the task in the new session.

### 5. Multi-User Accounts in a Single Organization
Claude's original identity verification fingerprint was calculated solely from the Anthropic Organization ID (`orgId`). This blocked users with multiple distinct email logins (seats) in the same organization from registering them in different slots, flagging them as duplicates.
* We updated the fingerprint logic to hash a combination of `orgId:email` in [headroom/collect.py](file:///D:/Users/Documents/Projects/2026/gestion%20acounts/headroom/collect.py).
* This permits different user logins of the same organization to be connected and rotated independently.

### 6. Subcommand `headroom graphify`
* We added a new `graphify` subcommand to the `headroom` CLI in [headroom/__main__.py](file:///D:/Users/Documents/Projects/2026/gestion%20acounts/headroom/__main__.py), allowing you to run the codebase mapper directly through headroom.
* The `headroom setup` wizard in [headroom/wizard.py](file:///D:/Users/Documents/Projects/2026/gestion%20acounts/headroom/wizard.py) now prompts to initialize and configure Graphify rules and skills for your developer tools (Claude Code, Google Antigravity, and Codex).

---

## ⚡ Windows Quickstart

1. Clone the repository and navigate into it:
   ```powershell
   git clone https://github.com/114juan/headroom-windows-c
   cd headroom-windows-c
   ```

2. Run the installer (bypassing execution policies for this file):
   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```

3. Ensure the local bin is in your User PATH (PowerShell command):
   ```powershell
   [Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [IO.Path]::Combine($Home, '.local', 'bin'), 'User')
   ```
   *Remember to restart your terminal after updating the PATH.*

4. Configure your accounts and dashboard:
   ```powershell
   headroom setup
   ```

5. Launch the live dashboard server:
   ```powershell
   headroom serve --open
   ```

6. Route or rotate your sessions:
   * Launch Claude: `headroom claude`
   * Rotate account manually (with context transfer): `headroom rotate`
   * Run graphify codebase mapper: `headroom graphify .`

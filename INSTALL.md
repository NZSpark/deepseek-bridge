# DeepSeek Web-to-API Bridge

A local bridge that exposes the DeepSeek web app as an OpenAI-compatible API.

## Setup

### Windows 11 (PowerShell)

The project requires Python 3.14 or newer. From the repository root, run:

```powershell
.\scripts\setup_win11.ps1
```

If PowerShell blocks the script, allow scripts for the current terminal only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\setup_win11.ps1
```

The setup script creates `.venv`, installs `requirements.txt`, and downloads Chromium. Then activate the environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
python deepseek_api_server.py
```

Then complete login in the opened Chromium window. See README.md for details.

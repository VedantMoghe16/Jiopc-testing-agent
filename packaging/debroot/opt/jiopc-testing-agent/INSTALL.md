# Installation Guide

Step-by-step instructions for installing the JioPC Automated Testing Agent on a fresh Ubuntu 24.04 + LxQt VM.

## Option 1: Debian Package (Recommended for VM)
The repository provides a build script to generate a `.deb` package that installs the agent into `/opt/jiopc-testing-agent/`.

1. Generate the package:
   ```bash
   bash packaging/build-deb.sh
   ```
2. Install the package:
   ```bash
   sudo dpkg -i dist/jiopc-testing-agent_1.0.0_all.deb
   sudo apt-get install -f # To resolve any missing system dependencies
   ```
   *Note: During installation, a postinst script will automatically create a Python virtual environment and install Playwright + Chromium.*

3. Run the agent using the globally installed launchers:
   ```bash
   jiopc-agent --config /opt/jiopc-testing-agent/jiopc-agent.yaml
   jiopc-agent-analyse
   ```

## Option 2: Manual / Developer Setup (macOS or Linux)

1. Clone the repository and navigate to the root directory.
2. Create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
3. Install the package and dependencies:
   ```bash
   pip install -e .[web,dev]
   ```
4. Install Playwright browsers:
   ```bash
   playwright install chromium --with-deps
   ```
5. Run the agent:
   ```bash
   python jiopc_agent.py --config jiopc-agent.yaml
   ```

// Windows-specific behaviour for the Commonroom desktop shell.
const { execFile } = require('node:child_process');
const path = require('node:path');
const { isCommonroomService, notCommonroom } = require('./service-process.cjs');

// Prints {pid, executable, commandLine} as JSON for the process listening on the port, or nothing.
const LISTENER = `
$ErrorActionPreference = 'Stop'
$connection = Get-NetTCPConnection -LocalPort ([int]$env:COMMONROOM_STOP_PORT) -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::', '::1') } | Select-Object -First 1
if (-not $connection) { exit 0 }
$process = Get-CimInstance Win32_Process -Filter ("ProcessId=" + [int]$connection.OwningProcess)
[pscustomobject]@{ pid = [int]$connection.OwningProcess; executable = [string]$process.ExecutablePath;
  name = [string]$process.Name; commandLine = [string]$process.CommandLine } | ConvertTo-Json -Compress
`;

function systemTool(...parts) {
  return path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', ...parts);
}

function run(command, args, options = {}) {
  return new Promise(resolve => {
    execFile(command, args, { windowsHide: true, timeout: 20000, encoding: 'utf8', ...options },
      (error, stdout) => resolve({ error, stdout: stdout || '' }));
  });
}

async function listener(port) {
  const { error, stdout } = await run(systemTool('WindowsPowerShell', 'v1.0', 'powershell.exe'),
    ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', LISTENER],
    { env: { ...process.env, COMMONROOM_STOP_PORT: String(port) } });
  if (error) throw new Error(`Windows could not check which program uses port ${port}.`);
  return stdout.trim() ? JSON.parse(stdout) : null;
}

const MINIMUM = [3, 10];
// Tried in order. The py launcher is preferred because python.exe on PATH is often the
// Microsoft Store stub, which exits with an error (or opens the Store) instead of running.
const CANDIDATES = [
  { command: 'py', args: ['-3'] },
  { command: 'python', args: [] },
  { command: 'python3', args: [] },
];
const PROBE = 'import sys;print(sys.version_info[:2]);print(sys.executable)';

function probe(candidate) {
  return new Promise(resolve => {
    execFile(candidate.command, [...candidate.args, '-c', PROBE],
      { windowsHide: true, timeout: 8000, encoding: 'utf8' },
      (error, stdout) => {
        if (error) return resolve(null);
        const [versionLine, executable] = stdout.trim().split(/\r?\n/).map(line => line.trim());
        const match = /^\((\d+),\s*(\d+)\)$/.exec(versionLine || '');
        if (!match || !executable) return resolve(null);
        const [major, minor] = [Number(match[1]), Number(match[2])];
        if (major < MINIMUM[0] || (major === MINIMUM[0] && minor < MINIMUM[1])) return resolve(null);
        resolve({ command: executable, args: [] });
      });
  });
}

module.exports = {
  name: 'windows',

  // Returns the real python.exe behind the first working candidate. Running python.exe
  // directly (not through py.exe) matters: the service is started without a console, and
  // py.exe would otherwise give its python.exe child a visible console window.
  async findPython() {
    for (const candidate of CANDIDATES) {
      const found = await probe(candidate);
      if (found) return found;
    }
    return null;
  },

  pythonInstallHint() {
    return 'winget install -e --id Python.Python.3.12';
  },

  serviceEnvironment() {
    return { ...process.env };
  },

  // Ends an older Commonroom service listening on 127.0.0.1:<port> (and the agent tools it started).
  // Only a Python process whose command line runs server.py or commonroom is ever stopped.
  async stopServiceOnPort(port) {
    if (!Number.isInteger(port)) throw new TypeError('port must be an integer');
    const found = await listener(port);
    if (!found) return { stopped: true };
    const executable = found.executable || found.name;
    if (!isCommonroomService({ executable, commandLine: found.commandLine })) {
      return { stopped: false, reason: notCommonroom(port, executable) };
    }
    const { error } = await run(systemTool('taskkill.exe'), ['/PID', String(found.pid), '/T', '/F']);
    if (error && await listener(port)) {
      return { stopped: false, reason: `Windows could not stop the older Commonroom service (process ${found.pid}).` };
    }
    return { stopped: true };
  },

  spawnOptions() {
    return { detached: true, windowsHide: true };
  },

  // Windows has no application menu; zoom keys are added per window instead.
  installMenu() {
    const { Menu } = require('electron');
    Menu.setApplicationMenu(null);
  },

  prepareWindow(contents) {
    contents.on('before-input-event', (event, input) => {
      if (input.type !== 'keyDown' || !input.control || input.alt || input.meta) return;
      const key = input.key;
      if (key !== '0' && key !== '=' && key !== '+' && key !== '-') return;
      const next = key === '0' ? 0 : contents.getZoomLevel() + (key === '-' ? -0.5 : 0.5);
      event.preventDefault();
      contents.setZoomLevel(next);
    });
  },

  quitWhenAllWindowsClosed: true,
};

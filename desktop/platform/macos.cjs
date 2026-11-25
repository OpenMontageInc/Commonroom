// macOS-specific behaviour for the Commonroom desktop shell.
const { execFile } = require('node:child_process');
const os = require('node:os');
const path = require('node:path');
const { setTimeout: delay } = require('node:timers/promises');
const { isCommonroomService, notCommonroom } = require('./service-process.cjs');

const MINIMUM = [3, 10];
// Newest first: a bare python3 is often Apple's older Command Line Tools Python.
const CANDIDATES = ['python3.15', 'python3.14', 'python3.13', 'python3.12', 'python3.11', 'python3.10', 'python3'];
const PROBE = 'import sys;print(sys.version_info[:2]);print(sys.executable)';

// Apps opened from Finder get a minimal PATH, so add the usual install locations. They go after the
// inherited PATH, so a shell's own choices (pyenv, nvm, a venv) still win for the service and agents.
function environment() {
  const home = os.homedir();
  const paths = [
    ...(process.env.PATH || '/usr/bin:/bin:/usr/sbin:/sbin').split(path.delimiter),
    path.join(home, '.local', 'bin'), path.join(home, '.npm-global', 'bin'),
    path.join(home, '.volta', 'bin'), '/opt/homebrew/bin', '/usr/local/bin',
  ];
  return { ...process.env, PATH: [...new Set(paths.filter(Boolean))].join(path.delimiter) };
}

function run(command, args) {
  return new Promise(resolve => {
    execFile(command, args, { env: environment(), timeout: 10000, encoding: 'utf8' },
      (error, stdout) => resolve({ error, stdout: stdout || '' }));
  });
}

// {command, args} for the real interpreter behind `command` when it is new enough, else null.
async function probe(command) {
  const { error, stdout } = await run(command, ['-c', PROBE]);
  if (error) return null;
  const [versionLine, executable] = stdout.trim().split(/\r?\n/).map(line => line.trim());
  const match = /^\((\d+),\s*(\d+)\)$/.exec(versionLine || '');
  if (!match || !executable) return null;
  const [major, minor] = [Number(match[1]), Number(match[2])];
  if (major < MINIMUM[0] || (major === MINIMUM[0] && minor < MINIMUM[1])) return null;
  return { command: executable, args: [] };
}

module.exports = {
  name: 'macos',

  // The shell's own Electron profile folder. On macOS appData is ~/Library/Application Support, which
  // also holds the service's data folder "Commonroom", so the shell keeps its files apart.
  profileFolder: 'Commonroom Desktop',

  // The first candidate that runs and is Python 3.10 or newer.
  async findPython() {
    for (const command of CANDIDATES) {
      const found = await probe(command);
      if (found) return found;
    }
    return null;
  },

  // Ends an older Commonroom service listening on 127.0.0.1:<port>. Only a Python process whose
  // command line runs server.py or commonroom is ever stopped.
  async stopServiceOnPort(port) {
    if (!Number.isInteger(port)) throw new TypeError('port must be an integer');
    const pids = (await run('lsof', ['-nP', `-iTCP@127.0.0.1:${port}`, '-sTCP:LISTEN', '-t'])).stdout
      .split(/\s+/).filter(Boolean).map(Number).filter(Number.isInteger);
    if (!pids.length) return { stopped: true };
    const pid = pids[0];
    const executable = (await run('ps', ['-o', 'comm=', '-p', String(pid)])).stdout.trim();
    const commandLine = (await run('ps', ['-o', 'command=', '-p', String(pid)])).stdout.trim();
    if (!isCommonroomService({ executable, commandLine })) return { stopped: false, reason: notCommonroom(port, executable) };
    for (const signal of ['SIGTERM', 'SIGKILL']) {
      try {
        process.kill(pid, signal);
      } catch {
        return { stopped: true };  // Already gone.
      }
      for (let waited = 0; waited < 5000; waited += 200) {
        await delay(200);
        try {
          process.kill(pid, 0);
        } catch {
          return { stopped: true };
        }
      }
    }
    return { stopped: false, reason: `macOS could not stop the older Commonroom service (process ${pid}).` };
  },

  pythonInstallHint() {
    return 'brew install python@3.12';
  },

  serviceEnvironment() {
    return environment();
  },

  spawnOptions() {
    return { detached: true };
  },

  installMenu({ openProject }) {
    const { Menu } = require('electron');
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      { role: 'appMenu' },
      { label: 'File', submenu: [
        { label: 'Open Project…', accelerator: 'Command+O', click: openProject },
        { type: 'separator' }, { role: 'close' },
      ] },
      { role: 'editMenu' },
      { label: 'View', submenu: [
        { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
        { type: 'separator' }, { role: 'togglefullscreen' },
      ] },
      { role: 'windowMenu' },
    ]));
  },

  // The View menu supplies zoom keys on macOS.
  prepareWindow() {},

  // macOS apps stay open with no windows; the Dock icon reopens one.
  quitWhenAllWindowsClosed: false,
};

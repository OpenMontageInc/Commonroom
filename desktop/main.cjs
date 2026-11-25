const { app, BrowserWindow, clipboard, dialog, ipcMain, nativeTheme, screen, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { setTimeout: delay } = require('node:timers/promises');

// Everything that differs between operating systems lives in ./platform/<os>.cjs.
const platformModules = { win32: './platform/windows.cjs', darwin: './platform/macos.cjs' };
const platform = platformModules[process.platform] ? require(platformModules[process.platform]) : null;
const DEFAULT_PORT = 8731;

// COMMONROOM_PORT and COMMONROOM_DATA run the shell against a throwaway service (port and data
// folder) without touching the everyday one; they also give the shell its own temporary profile.
function overridePort() {
  const value = process.env.COMMONROOM_PORT;
  if (!value) return null;
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new Error('COMMONROOM_PORT must be a whole number from 1024 to 65535.');
  }
  return port;
}

const portOverride = overridePort();
const dataOverride = process.env.COMMONROOM_DATA ? path.resolve(process.env.COMMONROOM_DATA) : null;
const servicePort = portOverride || DEFAULT_PORT;
const localEndpoint = `http://127.0.0.1:${servicePort}`;
const pythonDownloads = 'https://www.python.org/downloads/';

function qaEndpoint() {
  if (app.isPackaged || !process.env.COMMONROOM_TEST_URL) return null;
  const url = new URL(process.env.COMMONROOM_TEST_URL);
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) ||
      url.pathname !== '/' || url.username || url.password || url.search || url.hash) {
    throw new Error('COMMONROOM_TEST_URL must identify a loopback HTTP server.');
  }
  return url;
}

const testUrl = qaEndpoint();
const origin = testUrl?.origin || localEndpoint;
const profile = testUrl
  ? path.join(app.getPath('temp'), `commonroom-desktop-qa-${testUrl.hostname.replaceAll(':', '_')}-${testUrl.port || 80}`)
  : portOverride || dataOverride
    ? path.join(app.getPath('temp'), `commonroom-desktop-port-${servicePort}`)
    : path.join(app.getPath('appData'), 'Commonroom');
fs.mkdirSync(profile, { recursive: true });
app.setPath('userData', profile);
app.setName('Commonroom');

const stateFile = path.join(profile, 'window-state.json');
let saved = {};
let window = null;
let opening = null;
let choosing = false;

function readState() {
  try {
    const contents = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
    return contents && typeof contents === 'object' && !Array.isArray(contents) ? contents : {};
  } catch {
    return {};
  }
}

function saveState() {
  const temporary = `${stateFile}.${process.pid}.tmp`;
  try {
    fs.writeFileSync(temporary, JSON.stringify(saved));
    fs.renameSync(temporary, stateFile);
  } catch (error) {
    console.warn('Could not save the Commonroom window state.', error.message);
  }
}

function appPage(value) {
  try {
    const url = new URL(value);
    return url.origin === origin && url.pathname === '/' && !url.username && !url.password;
  } catch {
    return false;
  }
}

function trustedRenderer(event) {
  const frame = event.senderFrame;
  return window && !window.isDestroyed() && event.sender === window.webContents &&
    frame && frame === event.sender.mainFrame && frame.origin === origin && appPage(frame.url);
}

function rememberProject(value) {
  if (!appPage(value)) return;
  const projectId = new URL(value).searchParams.get('project');
  if (projectId && projectId !== saved.projectId) {
    saved.projectId = projectId;
    saveState();
  }
}

function placement() {
  const bounds = saved.bounds;
  if (!bounds || !['x', 'y', 'width', 'height'].every(key => Number.isInteger(bounds[key])) ||
      bounds.width <= 0 || bounds.height <= 0) {
    const area = screen.getPrimaryDisplay().workAreaSize;
    return { width: Math.min(1180, area.width), height: Math.min(820, area.height) };
  }
  const area = screen.getDisplayMatching(bounds).workArea;
  const width = Math.min(bounds.width, area.width);
  const height = Math.min(bounds.height, area.height);
  const x = Math.max(area.x, Math.min(bounds.x, area.x + area.width - width));
  const y = Math.max(area.y, Math.min(bounds.y, area.y + area.height - height));
  return { x, y, width, height };
}

function sameBounds(first, second) {
  return first && second && ['x', 'y', 'width', 'height'].every(key => first[key] === second[key]);
}

// The folder that contains the `commonroom` Python package: the repository root while
// developing, or the `service` folder the installer places next to the app's resources.
function serviceDirectory() {
  const directory = app.isPackaged ? path.join(process.resourcesPath, 'service') : path.join(__dirname, '..');
  const files = ['__init__.py', '__main__.py'];
  if (!files.every(file => fs.existsSync(path.join(directory, 'commonroom', file)))) {
    throw new Error(`The Commonroom service files could not be found in ${directory}.`);
  }
  return directory;
}

async function getLocalJson(resource, timeout = 2500) {
  const result = await fetch(new URL(resource, origin), {
    redirect: 'error', signal: AbortSignal.timeout(timeout),
  });
  if (!result.ok) throw new Error(`Commonroom returned HTTP ${result.status}.`);
  return result.json();
}

// The running service's /api/health, or null when nothing (or not Commonroom) answers.
async function serviceHealth() {
  try {
    const health = await getLocalJson('/api/health', 1000);
    return health && health.service === 'commonroom' ? health : null;
  } catch {
    return null;
  }
}

// A service is only reused when it is the same version as this shell (its UI files and API match).
async function healthy() {
  const health = await serviceHealth();
  return Boolean(health) && health.version === app.getVersion();
}

// Resolves true once nothing accepts connections on the service port.
async function portFree(timeout) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const open = await new Promise(resolve => {
      const socket = net.connect({ host: '127.0.0.1', port: servicePort });
      socket.setTimeout(1000);
      socket.once('connect', () => { socket.destroy(); resolve(true); });
      socket.once('timeout', () => { socket.destroy(); resolve(true); });
      socket.once('error', () => resolve(false));
    });
    if (!open) return true;
    if (Date.now() >= deadline) return false;
    await delay(200);
  }
}

// The service outlives the window, so after an upgrade an older version can still be running.
// Ask first (agents mid-reply will stop), then stop it: politely through /api/shutdown, or, for
// versions without that route, by ending the process listening on the port (if it is Commonroom).
async function replaceOutdatedService() {
  const { response } = await dialog.showMessageBox({
    type: 'question',
    title: 'Commonroom',
    message: 'An older version of Commonroom is still running in the background. Restart it now?',
    detail: 'Agents that are mid-reply will stop; you can retry them.',
    buttons: ['Restart', 'Quit'],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
  });
  if (response !== 0) {
    app.quit();
    throw Object.assign(new Error('An older Commonroom service is still running.'), { quiet: true });
  }
  let accepted = false;
  try {
    const result = await fetch(new URL('/api/shutdown', origin), {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(3000),
    });
    accepted = result.ok;
  } catch {
    accepted = false;
  }
  if (!accepted || !(await portFree(8000))) {
    const result = await platform.stopServiceOnPort(servicePort);
    if (!result.stopped) throw new Error(result.reason);
    if (!(await portFree(10000))) {
      throw new Error(`The older Commonroom service on port ${servicePort} did not stop. Restart your computer, then open Commonroom again.`);
    }
  }
  // Let the old process finish exiting so it releases the data folder.
  await delay(500);
}

// The last few lines the service wrote to its log after byte `offset`, for an error dialog.
function logTail(logPath, offset, count = 4) {
  try {
    const contents = fs.readFileSync(logPath).subarray(offset).toString('utf8');
    return contents.split(/\r?\n/).map(line => line.trimEnd()).filter(Boolean).slice(-count).join('\n');
  } catch {
    return '';
  }
}

// Finds Python, or asks the person to install it. Retry detects again without restarting.
async function requirePython() {
  for (;;) {
    const python = await platform.findPython();
    if (python) return python;
    const command = platform.pythonInstallHint();
    const { response } = await dialog.showMessageBox({
      type: 'warning',
      title: 'Commonroom',
      message: 'Commonroom needs Python 3.10 or newer.',
      detail: `Install it by running this command in a terminal, or download it from python.org:\n\n${command}\n\nThen choose Retry.`,
      buttons: ['Copy command', 'Open python.org', 'Retry', 'Quit'],
      defaultId: 2,
      cancelId: 3,
      noLink: true,
    });
    if (response === 0) clipboard.writeText(command);
    else if (response === 1) await shell.openExternal(pythonDownloads);
    else if (response === 3) {
      app.quit();
      throw Object.assign(new Error('Python is not installed.'), { quiet: true });
    }
  }
}

// Starts `<python> -m commonroom --port 8731` in the background. The service keeps running
// after the window closes, so agents can finish their work.
async function startService(directory) {
  const python = await requirePython();
  if (await healthy()) return;
  app.setAppLogsPath();
  fs.mkdirSync(app.getPath('logs'), { recursive: true });
  const logPath = path.join(app.getPath('logs'), 'backend.log');
  const log = fs.openSync(logPath, 'a');
  const logStart = fs.fstatSync(log).size;
  const serviceArgs = ['-m', 'commonroom', '--port', String(servicePort)];
  if (dataOverride) serviceArgs.push('--data', dataOverride);
  const env = { ...platform.serviceEnvironment(), PYTHONUNBUFFERED: '1' };
  // Keep compiled-bytecode folders out of the installed app (they break a signed macOS bundle).
  if (app.isPackaged) env.PYTHONDONTWRITEBYTECODE = '1';
  let backend;
  try {
    backend = spawn(python.command, [...python.args, ...serviceArgs], {
      ...platform.spawnOptions(),
      cwd: directory,
      env,
      stdio: ['ignore', log, log],
    });
    await new Promise((resolve, reject) => {
      backend.once('spawn', resolve);
      backend.once('error', reject);
    });
    backend.unref();
  } finally {
    fs.closeSync(log);
  }
  const deadline = Date.now() + 12000;
  do {
    if (await healthy()) return;
    if (backend.exitCode !== null || backend.signalCode !== null) break;
    await delay(200);
  } while (Date.now() < deadline);
  // Show what the service said (for example that the port is used by another program).
  await delay(200);
  const said = logTail(logPath, logStart);
  throw new Error(`${said || 'The local service did not start.'}\n\nService log: ${logPath}`);
}

async function ensureService() {
  const health = await serviceHealth();
  if (health && health.version === app.getVersion()) return;
  if (testUrl) {
    if (health) return;  // A QA server is whatever the tester started.
    throw new Error(`The Commonroom test service is unavailable at ${origin}.`);
  }
  const directory = serviceDirectory();
  if (health) await replaceOutdatedService();
  return startService(directory);
}

async function startupAddress() {
  const url = new URL('/', origin);
  if (typeof saved.projectId === 'string' && saved.projectId) {
    try {
      const directory = await getLocalJson('/api/projects');
      if (directory.projects.some(project => project.id === saved.projectId)) {
        url.searchParams.set('project', saved.projectId);
      }
    } catch {
      // The renderer owns project-directory errors and recovery.
    }
  }
  return url.href;
}

function installMenu() {
  platform.installMenu({
    openProject: () => openWindow().then(() => window?.webContents.send('commonroom:open-project')).catch(showStartupError),
  });
}

function focusWindow() {
  if (!window || window.isDestroyed()) return;
  if (window.isMinimized()) window.restore();
  if (window.isVisible()) window.focus();
}

async function createWindow() {
  await ensureService();
  const address = await startupAddress();
  const requestedBounds = placement();
  const previousBounds = sameBounds(requestedBounds, saved.bounds) ? saved.bounds : null;
  const next = new BrowserWindow({
    ...requestedBounds,
    title: 'Commonroom',
    show: false,
    backgroundColor: saved.appearance === 'dark' ? '#212121' : '#ffffff',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  window = next;
  const contents = next.webContents;
  const guardNavigation = event => {
    if (!event.isMainFrame || !appPage(event.url)) event.preventDefault();
  };
  contents.on('will-frame-navigate', guardNavigation);
  contents.on('will-redirect', guardNavigation);
  contents.on('will-attach-webview', event => event.preventDefault());
  contents.setWindowOpenHandler(() => ({ action: 'deny' }));
  contents.on('did-navigate', (_event, url) => rememberProject(url));
  contents.on('did-navigate-in-page', (_event, url, mainFrame) => {
    if (mainFrame) rememberProject(url);
  });
  platform.prepareWindow(contents);
  let shownBounds;
  function rememberWindow() {
    if (!shownBounds) return;
    const bounds = next.getNormalBounds();
    // A restored Windows window can be rounded to a slightly different size at the current DPI.
    saved.bounds = previousBounds && sameBounds(bounds, shownBounds) ? previousBounds : bounds;
    saved.maximized = next.isMaximized();
    saveState();
  }
  let saveAfterResize;
  function scheduleWindowSave() {
    clearTimeout(saveAfterResize);
    saveAfterResize = setTimeout(rememberWindow, 150);
  }
  next.once('ready-to-show', () => {
    if (saved.maximized === true) next.maximize();
    next.show();
    shownBounds = next.getNormalBounds();
    rememberWindow();
  });
  for (const event of ['move', 'resize', 'maximize', 'unmaximize']) next.on(event, scheduleWindowSave);
  next.on('close', rememberWindow);
  next.on('closed', () => {
    clearTimeout(saveAfterResize);
    window = null;
  });
  await next.loadURL(address);
}

function openWindow() {
  if (opening) return opening;
  if (window) {
    focusWindow();
    return Promise.resolve();
  }
  opening = createWindow().finally(() => { opening = null; });
  return opening;
}

function showStartupError(error) {
  if (error.quiet) return;
  dialog.showErrorBox('Commonroom could not open', error.message);
  app.quit();
}

if (!platform) {
  app.whenReady().then(() => {
    dialog.showErrorBox('Commonroom could not open',
      `Commonroom's desktop app supports Windows and macOS. On ${process.platform}, run the service with ` +
      `"python3 -m commonroom" and open ${localEndpoint} in a browser.`);
    app.quit();
  });
} else if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  saved = readState();
  nativeTheme.themeSource = saved.appearance === 'dark' ? 'dark' : 'light';
  app.on('second-instance', () => {
    if (app.isReady()) openWindow().catch(showStartupError);
  });
  app.on('activate', () => {
    if (app.isReady()) openWindow().catch(showStartupError);
  });
  app.on('window-all-closed', () => { if (platform.quitWhenAllWindowsClosed) app.quit(); });
  ipcMain.handle('commonroom:choose-folder', async event => {
    if (!trustedRenderer(event)) {
      throw new Error('Only the Commonroom window can choose a project folder.');
    }
    if (choosing) return null;
    choosing = true;
    try {
      const result = await dialog.showOpenDialog(window, {
        title: 'Choose a project folder',
        defaultPath: app.getPath('documents'),
        properties: ['openDirectory'],
      });
      return result.canceled ? null : result.filePaths[0] || null;
    } finally {
      choosing = false;
    }
  });
  ipcMain.handle('commonroom:set-appearance', (event, appearance) => {
    if (!trustedRenderer(event)) throw new Error('Only the Commonroom window can set its appearance.');
    if (appearance !== 'light' && appearance !== 'dark') throw new TypeError('Choose light or dark appearance.');
    nativeTheme.themeSource = appearance;
    if (saved.appearance !== appearance) {
      saved.appearance = appearance;
      saveState();
    }
  });
  ipcMain.handle('commonroom:open-external', (event, address) => {
    if (!trustedRenderer(event) || typeof address !== 'string') return;
    const url = new URL(address);
    if (['http:', 'https:'].includes(url.protocol) && !url.username && !url.password && url.origin !== origin) {
      return shell.openExternal(url.href);
    }
  });
  app.whenReady().then(() => {
    installMenu();
    return openWindow();
  }).catch(showStartupError);
}

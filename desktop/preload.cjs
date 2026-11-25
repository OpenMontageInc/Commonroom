const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('commonroomDesktop', {
  chooseFolder: () => ipcRenderer.invoke('commonroom:choose-folder'),
  setAppearance: appearance => ipcRenderer.invoke('commonroom:set-appearance', appearance),
});

ipcRenderer.on('commonroom:open-project', () => {
  window.dispatchEvent(new Event('commonroom:open-project'));
});

function followLink(event) {
  if (!event.isTrusted || event.defaultPrevented || ![0, 1].includes(event.button)) return;
  const anchor = event.composedPath().find(item => item instanceof HTMLAnchorElement && item.href);
  if (!anchor) return;
  let url;
  try {
    url = new URL(anchor.href);
  } catch {
    event.preventDefault();
    return;
  }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    event.preventDefault();
    return;
  }
  if (url.origin !== window.location.origin) {
    event.preventDefault();
    ipcRenderer.invoke('commonroom:open-external', url.href).catch(() => {});
  }
}

document.addEventListener('click', followLink);
document.addEventListener('auxclick', followLink);

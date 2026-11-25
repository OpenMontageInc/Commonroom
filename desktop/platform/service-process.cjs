// Shared by the platform modules: decides whether a process listening on the service port is an
// (older) Commonroom service, which is the only kind of process the shell will ever stop.

// python, python3, python3.12, pythonw, py, or macOS's Python.app binary "Python" (.exe optional).
const PYTHON = /^(python[\d.]*w?|py)(\.exe)?$/i;

function baseName(file) {
  return String(file).split(/[\\/]/).pop();
}

function isCommonroomService({ executable, commandLine }) {
  if (typeof executable !== 'string' || typeof commandLine !== 'string') return false;
  const command = commandLine.toLowerCase();
  return PYTHON.test(baseName(executable)) && (command.includes('server.py') || command.includes('commonroom'));
}

function notCommonroom(port, executable) {
  const name = executable ? baseName(executable) : 'another program';
  return `Port ${port} is used by ${name}, which is not Commonroom. Close it, then open Commonroom again.`;
}

module.exports = { isCommonroomService, notCommonroom };

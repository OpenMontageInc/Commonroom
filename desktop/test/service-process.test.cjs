// Run with `npm test` in desktop/.
const assert = require('node:assert/strict');
const test = require('node:test');
const { isCommonroomService, notCommonroom } = require('../platform/service-process.cjs');

test('recognises Python running a Commonroom service', () => {
  for (const [executable, commandLine] of [
    ['C:\\Python313\\python.exe', '"C:\\Python313\\python.exe" server.py'],
    ['C:\\Python313\\pythonw.exe', 'pythonw -m commonroom --port 8731'],
    ['C:\\Windows\\py.exe', 'py -3 C:\\Users\\me\\Commonroom\\server.py'],
    ['/usr/bin/python3.12', 'python3.12 -m commonroom'],
    ['/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python', 'Python -m commonroom'],
  ]) assert.equal(isCommonroomService({ executable, commandLine }), true, executable);
});

test('never matches other programs or other Python scripts', () => {
  for (const [executable, commandLine] of [
    ['C:\\Program Files\\nodejs\\node.exe', 'node commonroom.js'],
    ['C:\\Python313\\python.exe', 'python -m http.server 8731'],
    ['C:\\Tools\\pythonista.exe', 'pythonista server.py'],
    ['/usr/bin/python3', null],
    [undefined, 'python server.py'],
  ]) assert.equal(isCommonroomService({ executable, commandLine }), false, `${executable} ${commandLine}`);
});

test('explains which program holds the port', () => {
  assert.match(notCommonroom(8731, 'C:\\Tools\\other.exe'), /Port 8731 is used by other\.exe, which is not Commonroom/);
  assert.match(notCommonroom(8731, ''), /used by another program/);
});

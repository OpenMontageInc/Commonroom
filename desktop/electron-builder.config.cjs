// Installer build settings. Signing is off unless COMMONROOM_SIGNING is set,
// so anyone can build an unsigned installer from source. Release builds sign
// the app, the installer and the uninstaller with the maintainer's certificate.
//
//   COMMONROOM_SIGNING            none (default) | azure | certificate
//   COMMONROOM_SIGNING_PUBLISHER  certificate common name exactly as issued
//   azure:       COMMONROOM_AZURE_SIGNING_ENDPOINT, _ACCOUNT, _PROFILE
//                (Azure Artifact Signing; sign in first with `az login`)
//   certificate: CSC_LINK (+ CSC_KEY_PASSWORD), a .pfx code-signing certificate

function windowsSigning(env = process.env) {
  const method = env.COMMONROOM_SIGNING || 'none';
  if (method === 'none') return { signExecutable: false };
  const publisherName = env.COMMONROOM_SIGNING_PUBLISHER;
  if (!publisherName) throw new Error('Set COMMONROOM_SIGNING_PUBLISHER to the certificate common name exactly as issued.');
  if (method === 'azure') {
    const endpoint = env.COMMONROOM_AZURE_SIGNING_ENDPOINT;
    const codeSigningAccountName = env.COMMONROOM_AZURE_SIGNING_ACCOUNT;
    const certificateProfileName = env.COMMONROOM_AZURE_SIGNING_PROFILE;
    if (!endpoint || !codeSigningAccountName || !certificateProfileName) {
      throw new Error('Azure signing needs COMMONROOM_AZURE_SIGNING_ENDPOINT, _ACCOUNT and _PROFILE.');
    }
    return { azureSignOptions: { publisherName, endpoint, codeSigningAccountName, certificateProfileName } };
  }
  if (method === 'certificate') {
    if (!env.CSC_LINK && !env.WIN_CSC_LINK) throw new Error('Certificate signing needs CSC_LINK (and CSC_KEY_PASSWORD).');
    return { signtoolOptions: { publisherName, signingHashAlgorithms: ['sha256'] } };
  }
  throw new Error('COMMONROOM_SIGNING must be none, azure or certificate.');
}

const signing = (process.env.COMMONROOM_SIGNING || 'none') !== 'none';

module.exports = {
  appId: 'dev.commonroom.app',
  productName: 'Commonroom',
  copyright: 'Copyright (c) Commonroom contributors',
  directories: { output: 'build', buildResources: 'build-resources' },
  files: ['main.cjs', 'preload.cjs', 'platform/**/*', 'package.json'],
  extraResources: [
    {
      from: '../commonroom',
      to: 'service/commonroom',
      filter: ['**/*', '!**/__pycache__{,/**}', '!**/*.pyc', '!**/tests{,/**}', '!**/test_*.py'],
    },
    { from: '../LICENSE', to: 'LICENSE' },
    { from: '../NOTICE', to: 'NOTICE' },
  ],
  asar: true,
  // A signing build must fail rather than quietly produce unsigned files.
  forceCodeSigning: signing,
  win: { target: ['nsis'], icon: 'commonroom.ico', ...windowsSigning() },
  nsis: {
    oneClick: true,
    perMachine: false,
    createDesktopShortcut: true,
    createStartMenuShortcut: true,
    shortcutName: 'Commonroom',
    runAfterFinish: true,
    deleteAppDataOnUninstall: false,
    installerIcon: 'commonroom.ico',
    uninstallerIcon: 'commonroom.ico',
    artifactName: 'Commonroom-Setup-${version}.${ext}',
  },
  mac: { target: ['dmg'], icon: 'commonroom.icns', category: 'public.app-category.productivity' },
  dmg: { artifactName: 'Commonroom-${version}.${ext}' },
};

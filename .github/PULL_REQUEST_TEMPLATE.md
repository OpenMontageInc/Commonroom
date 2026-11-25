<!--
Thanks for contributing to Commonroom! Please fill in the sections below.
Keep PRs focused: one logical change per PR is easier to review and merge.
See CONTRIBUTING.md for the full process.
-->

## Summary

<!-- What does this PR do, and why? -->

## Related issue

<!-- Link the issue this closes, e.g. "Closes #123". Use "Refs #123" if it only relates. -->
Closes #

## Changes

<!-- Bullet the notable changes. -->
-

## Testing

<!-- Tick what you ran and fill in what you checked by hand. -->
- [ ] `py -3 -m pytest tests -q`
- [ ] `npm test` (in `desktop/`)
- Providers checked manually (tool + version, or "none"):
- Operating system(s) checked manually:

## Checklist

- [ ] The change is focused on a single logical concern.
- [ ] I ran the tests above locally, and any manual checks used a temporary data folder (`--data`), not my real chats.
- [ ] I updated docs/README if behavior or usage changed.
- [ ] Anything that changes how agents are started (flags, permission or sandbox modes) is called out above.
- [ ] No unrelated files (build output, `signing.local.ps1`, local config, databases) are included in the diff.

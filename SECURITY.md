# Security

Rflow runs on people's own laptops with a global keyboard hook, the microphone, the clipboard, their API keys and an
updater, so a flaw can matter even in a small app. Thank you for reporting one privately.

## Reporting a flaw

**Please don't open a public issue.** Use GitHub's private reporting instead:
[Report a vulnerability](https://github.com/karthi-ai-engineer/rflow-ai/security/advisories/new) (the repository's
*Security* tab → *Report a vulnerability*). Only the maintainer sees it.

Say what an attacker could do, how to reproduce it, and which Rflow version you tried. You'll get an answer as soon as
possible; once a fix is released, the advisory is published, and you're credited if you wish.

## Supported versions

Only the latest release gets fixes. Rflow updates itself (Settings → Check for updates), so please check that the flaw
is still there in the latest version.

## What counts

For example:

- the updater installing anything but the published, checksum-verified `Rflow-Setup.exe`
- API keys readable by another Windows user, or written anywhere unencrypted (logs included)
- the local web page (`sst web`) reachable from another computer
- audio, dictated text or keys sent anywhere the user didn't choose
- text typed or pasted into a window other than the one the user was in
- a downloaded model or voice used without its checksum being checked

Bugs that need someone already signed in to your Windows account as you, or physical access to an unlocked laptop,
are usually out of scope. Report them anyway if you're unsure.

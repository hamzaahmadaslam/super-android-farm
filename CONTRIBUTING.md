# Contributing

For anyone who wants to fix a bug, add a feature or improve the documentation of Super Farm.

The development guide, [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md), has the set-up, the test layout and the recipes for adding a setting, a route, a command or a tool.

## Before you start

- Read [ACCEPTABLE_USE.md](ACCEPTABLE_USE.md). Contributions must follow it. A change whose main use is something that page rules out will not be accepted.
- Report a security problem as [SECURITY.md](SECURITY.md) describes, not in a public issue.
- For anything bigger than a bug fix, open an issue first and say what you want to change and why, so you do not build something that cannot be merged. The repository is `https://github.com/hamzaahmadaslam/super-android-farm`.

## Run the gates before you push

There is no hosted CI. The checks that matter most, the live tests, need a real Docker host with the binder kernel module and an Android image, so the project runs its gates on the contributor's machine, the same way for everyone, with one script:

```bash
scripts/check.sh --quick    # lint and static checks, seconds
scripts/check.sh            # also the unit tests, about 3 minutes
scripts/check.sh --live     # also the live tests, needs Docker and an Android image
scripts/check.sh --install-hook   # refuse a push while a gate fails
```

`make check`, `make quick` and `make live` do the same. Run at least `scripts/check.sh` before you open a pull request, and run the live gate when your change touches Docker, networking, the host setup or automation. Say in the pull request which gates you ran and on what hardware. A change that was not run live should say so.

## What a good change looks like

- It does one thing. A fix and a refactor are two changes.
- It comes with a test that fails without it. Unit tests need nothing but Python. See [Tests](docs/DEVELOPMENT.md#tests).
- It keeps generated documents in step. If you add a setting, a command, a route, a flow step or a tool, regenerate the reference and commit the result. [Keeping generated documents current](docs/DEVELOPMENT.md#keeping-generated-documents-current) lists the commands, and the gates fail when you forget.
- It does not name a vendor's AI product. The documents and code say "AI assistant" and "MCP client".
- It is plain. Prefer the direct version, and leave out options nobody asked for.
- Documentation is written for the reader who is in a hurry: say what a thing does and when to use it. Do not state something you did not check against the code or run. Mark anything unverified as "not verified".

## Commit messages

The history uses one plain line per commit that says what the change covers, in ordinary words. Do the same. Add a body when the reason for the change is not obvious from the diff. Do not put secrets, personal data, host names or other people's details in a commit.

## What must never be committed

Secrets of any kind (API tokens, VPN profiles or logins, keys), app files, kernel modules and other built binaries, a farm's data, and anything personal. `.gitignore` covers the common cases. The `no secrets or private data` gate in `scripts/check.sh` looks for private-key blocks, common token shapes, and files of kinds that must not be committed, and also checks a list of words that is private to your checkout in `.git/info/private-words` (an untracked file) so those words never appear in the repository themselves.

## Licence

Super Farm is released under Apache-2.0 (see [LICENSE](LICENSE)). Unless you state otherwise, a contribution you submit is licensed under the same terms (section 5 of the licence). The one exception is the patch in `native/v4l2loopback/`, which modifies a GPL-2.0-or-later work and carries that licence.

## Next

- [Development guide](docs/DEVELOPMENT.md): set-up, tests, conventions and recipes.
- [Architecture](docs/ARCHITECTURE.md): how the parts fit together.
- [Design notes](docs/DESIGN_NOTES.md): why things are the way they are.
- [Documentation index](docs/README.md): every document.

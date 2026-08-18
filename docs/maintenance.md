# Maintaining and releasing climits

Notes for whoever ships changes to this plugin. Everything here was learned the hard
way on a real install; none of it is obvious from the plugin documentation.

## Releasing: a version bump is what reaches users

`claude plugin update` compares the `version` field in `.claude-plugin/plugin.json`.
If the version has not changed, it answers `already at the latest version` and the
installed clone stays exactly as it was — even when new commits are sitting in
`main`. **Any change that should reach already-installed copies needs a version
bump, including a documentation-only change.**

A *fresh* install is different: it clones the current `main` and gets the current
content under whatever version number the manifest declares. So a stale version
number means old installs stay behind while new installs get the new files — under
the same version number. Bump it.

Keep the number in three places in sync, or `claude plugin tag` complains:

- `.claude-plugin/plugin.json` → `version`
- `.claude-plugin/marketplace.json` → `plugins[0].version`
- `bin/climits` → `VERSION`

The release order matters:

```bash
git push origin main                       # the marketplace is a git clone
claude plugin marketplace update climits   # refresh the catalog
claude plugin update climits@climits       # then the plugin itself
```

**Use the full `plugin@marketplace` identifier in `update`.** The bare name fails
with `Plugin "climits" not found`, even though `install`, `list` and `details` all
accept it.

## Installing: once per config directory

Claude Code profiles (`~/.claude/profiles/*`) each carry their own `enabledPlugins`
and their own plugin cache. Installing into `~/.claude` does **not** cover them.
Repeat for every profile you use:

```bash
CLAUDE_CONFIG_DIR=~/.claude/profiles/work claude plugin install climits@climits -y
```

The same applies to `claude plugin update` and to `claude plugin marketplace add`.

## The manifest must not declare hooks/hooks.json

`hooks/hooks.json` is discovered by convention. Naming it again in `plugin.json`
under `"hooks"` is a duplicate, not a safety net:

```
Hook load failed: Duplicate hooks file detected: ./hooks/hooks.json resolves to
already-loaded file .../hooks/hooks.json. The standard hooks/hooks.json is loaded
automatically, so manifest.hooks should only reference additional hook files.
```

The plugin then shows as `✘ failed to load` in `claude plugin list`. Confusingly the
hooks still fire — the conventional file loads fine, and only the redundant manifest
reference errors — so the symptom is a broken status line rather than broken
behaviour. Fixed in 0.1.1.

**`claude plugin validate` does not catch this.** It passed cleanly, with and
without `--strict`, while the plugin was failing to load. Validation is a check of
the manifest's shape, not evidence that the plugin works. The only real check is
installing it and reading `claude plugin list`.

## Verifying a release

1. `python3 tests/selftest.py` — 94 scenarios, no network, no live account.
2. `claude plugin validate . --strict` — necessary, not sufficient (see above).
3. Install it somewhere clean and confirm the status is `✔ enabled`:
   ```bash
   CLAUDE_CONFIG_DIR=/tmp/climits-check claude plugin marketplace add <path-or-repo>
   CLAUDE_CONFIG_DIR=/tmp/climits-check claude plugin install climits@climits -y
   CLAUDE_CONFIG_DIR=/tmp/climits-check claude plugin list
   ```
4. Confirm the hooks actually fire, and fire **once**. Count the lines in
   `~/.claude/state/limits/gate.log` before and after a throwaway session:
   ```bash
   claude -p "Reply with exactly: PING" < /dev/null
   ```
   One `UserPromptSubmit` record per run means the plugin's hooks are live and are
   not duplicating hooks declared in `settings.json`. Two records mean both are
   registered — remove the manual block from every `settings.json` that has it.

## Working copy vs installed clone

Hooks run the clone under `~/.claude/plugins/cache/climits/climits/<version>/`, not
your checkout. While developing, either test with

```bash
claude --plugin-dir /path/to/climits
```

which takes precedence over the installed copy for that session, or accept that your
checkout and the installed copy diverge until you bump the version and update.

## Distribution

- Repository: <https://github.com/likemusic/climits> (MIT).
- Submitted to Anthropic's community marketplace through the Console form at
  <https://platform.claude.com/plugins/submit>; submission status is visible at
  <https://platform.claude.com/plugins/submissions>. Approved plugins are pinned to a
  commit SHA in `anthropics/claude-plugins-community` and CI moves the pin as new
  commits land; the public catalog syncs nightly, so listing lags approval.
- The official marketplace (`claude-plugins-official`) has no application process —
  inclusion is Anthropic's decision, and the submission form does not feed it.
- Community directories such as ClaudePluginHub discover public plugins by scanning
  GitHub for valid manifests; their submission forms only shorten the queue.

#!/usr/bin/env bash
# Cut a release and publish it.
#
# During development: commit freely on main and push to origin. When you hit a
# meaningful boundary — a coherent batch of work worth marking as a checkpoint
# — run this script. It will:
#   1. Bump the version via python-semantic-release (if there are feat/fix
#      commits since the last tag; no-op otherwise) — via build_command in
#      pyproject.toml this ALSO runs `uv build`, producing the dispatch wheel
#      in dist/.
#   2. Build the protocol wheel into the same dist/ and verify that dist/ holds
#      exactly the two wheels at the release version.
#   3. Run the publish hook (below), BEFORE anything is pushed.
#   4. Push main + tags to origin and (if configured) github.
#   5. Run the optional post-push hook.
#
# Usage: ./release.sh [--no-publish]
#
# Maintainer-local publish hook
# -----------------------------
# Where the wheels go (a private package index, a fleet of dispatch hosts) is
# deployment-specific, so it lives outside this repo. If an executable
# ./release.local.sh exists (gitignored, never committed), it runs from the
# repo root as:
#
#     ./release.local.sh <version> <absolute-dist-dir>
#
# Exit 0 means the wheels are published (and deployed, if the hook deploys);
# exit 10 means they are published but a downstream deploy did not finish (the
# release still pushes, then exits 3); any other exit aborts the release.
# Publishing happens before the push so a remote never advertises a version
# whose artifacts did not ship. On any abort the local release tag is deleted,
# main is reset to where it was, and nothing has been pushed. With no hook
# present the release aborts too, unless --no-publish is given, which skips
# publishing (loudly) and pushes anyway.
#
# Optional maintainer-local post-push hook: if an executable
# ./release.post.local.sh exists (gitignored), it runs after the pushes as
# `./release.post.local.sh <version>`. A failure is reported and exits 4;
# nothing is rolled back.

set -euo pipefail

publish=1
for arg in "$@"; do
  case "$arg" in
    --no-publish) publish=0 ;;
    *)
      echo "Unknown argument: $arg (usage: ./release.sh [--no-publish])" >&2
      exit 2
      ;;
  esac
done

if [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree not clean — commit or stash first." >&2
  exit 1
fi

if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
  echo "error: must be on main branch." >&2
  exit 1
fi

# 1. Bump version + build the dispatch wheel (semantic-release's build_command
#    runs `uv lock && ... && uv build`, dropping the wheel in dist/). dist/ is
#    cleared first so the verification below sees only this release's wheels.
#    Remember where main was, so an abort can drop the release commit too.
pre_release_head=$(git rev-parse HEAD)
rm -rf dist
uv run semantic-release version --no-push --no-vcs-release

if [ "$(git rev-parse HEAD)" = "$pre_release_head" ]; then
  echo "No release-worthy commits since the last tag; nothing to release."
  exit 0
fi

new_version="$(uv run python -c 'import tomllib,pathlib;print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')"
new_tag="v${new_version}"

# Abort the release: drop the local tag AND the release commit, so a re-run
# starts from the same main (a leftover chore(release) commit would otherwise
# be stacked under a second one). The tree was asserted clean at the start, so
# the hard reset discards only what this script created. Nothing has been
# pushed at this point.
abort_release() {
  echo "$1" >&2
  git tag -d "$new_tag" >/dev/null 2>&1 || true
  git reset -q --hard "$pre_release_head"
  echo "Local tag $new_tag deleted and main reset to ${pre_release_head:0:7}; nothing was pushed." >&2
  exit 1
}

# 2. Build the protocol wheel into the same dist/, then verify exactly the two
#    wheels at the release version are there — the hook publishes the whole dir.
uv build packages/protocol -o dist || abort_release "Protocol wheel build failed."
actual_wheels=$(find dist -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')
[ "$actual_wheels" = "2" ] || abort_release "Expected exactly 2 wheels in dist/, found $actual_wheels."
for name in agent_gtd_dispatch agent_gtd_dispatch_protocol; do
  [ -f "dist/${name}-${new_version}-py3-none-any.whl" ] ||
    abort_release "Missing wheel dist/${name}-${new_version}-py3-none-any.whl."
done

# 3. Publish hook (see header). Runs before any push.
#    Exit codes: 0 = published (and deployed, if the hook deploys);
#    10 = wheels ARE published but a downstream deploy did not finish. A
#    published version is immutable, so aborting would leave the release
#    un-rerunnable: push the tags (the wheels shipped), then fail loudly so the
#    operator finishes the deploy. Any other code = nothing published: abort.
deploy_incomplete=0
if [ -x ./release.local.sh ]; then
  set +e
  ./release.local.sh "$new_version" "$PWD/dist"
  hook_rc=$?
  set -e
  case "$hook_rc" in
    0) ;;
    10) deploy_incomplete=1 ;;
    *) abort_release "release.local.sh failed (exit $hook_rc); aborting release." ;;
  esac
elif [ "$publish" = "1" ]; then
  abort_release "No executable ./release.local.sh publish hook found. It is a maintainer-local script, invoked as: release.local.sh <version> <abs-dist-dir>; exit 0 means the wheels are published, 10 means published but deploy incomplete, anything else means nothing was published. Create it, or re-run with --no-publish to release without publishing wheels."
else
  echo "!!! NO WHEELS PUBLISHED: --no-publish given and no ./release.local.sh hook; pushing tags anyway. !!!" >&2
fi

# 4. Push to origin, then github if configured.
git push origin main --tags
if git remote get-url github >/dev/null 2>&1; then
  git push github main --tags
else
  echo "!!! No 'github' remote configured: released to origin only. Publish later with: git push github main --tags !!!" >&2
fi

# 5. Optional post-push hook. The release is already public, so a failure
#    never rolls anything back; it is reported and makes the script exit 4.
post_rc=0
if [ -x ./release.post.local.sh ]; then
  set +e
  ./release.post.local.sh "$new_version"
  post_rc=$?
  set -e
  if [ "$post_rc" -ne 0 ]; then
    echo "!!! ${new_tag} is pushed, but ./release.post.local.sh failed (exit ${post_rc}). Rerun it by hand: ./release.post.local.sh ${new_version} !!!" >&2
  fi
fi

echo "Released ${new_tag}."
if [ "$deploy_incomplete" = "1" ]; then
  echo "!!! ${new_tag} is published and pushed, but the publish hook reported an INCOMPLETE deploy (exit 10). Finish the deploy before relying on it. !!!" >&2
  exit 3
fi
if [ "$post_rc" -ne 0 ]; then
  exit 4
fi

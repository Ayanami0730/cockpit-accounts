#!/usr/bin/env python3
"""Build the portable macOS fork without passing API credentials to build tools."""
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import json
import platform
import sys

root = Path(__file__).resolve().parents[1]
keep = ("PATH", "HOME", "TMPDIR", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL",
        "RUSTUP_HOME", "CARGO_HOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy", "DEVELOPER_DIR", "SDKROOT", "CARGO_NET_OFFLINE")
env = {key: os.environ[key] for key in keep if key in os.environ}
env.update(MACOSX_DEPLOYMENT_TARGET="12.0", RUSTUP_TOOLCHAIN="1.95.0", CARGO_BUILD_JOBS="2", GOMAXPROCS="2",
           RUSTFLAGS="--remap-path-prefix=" + str(Path.home()) + "=/build",
           CARGO_PROFILE_RELEASE_DEBUG="0", CARGO_PROFILE_RELEASE_OPT_LEVEL="0",
           CARGO_PROFILE_RELEASE_CODEGEN_UNITS="256", CARGO_PROFILE_RELEASE_STRIP="symbols")
mode = sys.argv[1] if len(sys.argv) > 1 else "build"
if mode == "build":
    command = ["node", "node_modules/@tauri-apps/cli/tauri.js", "build", "--bundles", "app"]
elif mode == "seal":
    command = None
elif mode == "rust-tests":
    command = ["cargo", "test", "--release", "-p", "cockpit-tools", "--features", "tauri/custom-protocol", "--lib", *sys.argv[2:], "--", "--test-threads=1"]
elif mode == "check":
    command = ["cargo", "check", "--release", "-p", "cockpit-tools", "--features", "tauri/custom-protocol", "--lib"]
else:
    raise SystemExit("usage: build-local-safe.py [build | seal | check | rust-tests FILTER]")
result = subprocess.call(command, cwd=root, env=env) if command else 0
if result == 0 and mode in ("build", "seal") and sys.platform == "darwin":
    # Sign outside synced folders: iCloud can immediately recreate FinderInfo
    # on .app directories. Only the resulting signed ZIP returns to the checkout.
    source = root / "target/release/bundle/macos/Cockpit Accounts.app"
    version = json.loads((root / "package.json").read_text())["version"]
    archive = root / f"target/release/bundle/Cockpit-Accounts-{version}-macos-{platform.machine()}.zip"
    with tempfile.TemporaryDirectory(prefix="cockpit-accounts-package-") as stage:
        app = Path(stage) / "Cockpit Accounts.app"
        shutil.copytree(source, app, copy_function=shutil.copy, symlinks=True)
        for executable in (app / "Contents/MacOS").iterdir():
            if executable.is_file() and not os.access(executable, os.X_OK):
                raise SystemExit("Packaged executable lost its executable permission")
        for notice in ("LICENSE", "NOTICE.md"):
            shutil.copyfile(root / notice, app / "Contents/Resources" / notice)
        # Do not remove quarantine or change Gatekeeper settings.
        for attribute in ("com.apple.FinderInfo", "com.apple.ResourceFork"):
            subprocess.run(["/usr/bin/xattr", "-dr", attribute, str(app)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        result = subprocess.call(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)], env=env)
        if result == 0:
            result = subprocess.call(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], env=env)
        if result == 0:
            result = subprocess.call(["/usr/bin/ditto", "-c", "-k", "--norsrc", "--keepParent", str(app), str(archive)], env=env)
        if result == 0:
            print("Signed archive:", archive)
raise SystemExit(result)

class Iterm2Harness < Formula
  desc "HTTP API for remote-controlling iTerm2 (auth + audit + AutoLaunch)"
  homepage "https://github.com/wsvn53/iterm2-harness"
  url "https://github.com/wsvn53/iterm2-harness/archive/refs/heads/main.tar.gz"
  version "0.1.0"
  license "MIT"
  head "https://github.com/wsvn53/iterm2-harness.git", branch: "main"

  depends_on :macos

  def install
    prefix.install "iterm2-harness.py", "config.json", "install.sh", "README.md"
    (prefix/"skills").install Dir["skills/*"] if Dir.exist?("skills")

    # Wrapper for manual (re)installation / uninstallation of the AutoLaunch link.
    (bin/"iterm2-harness-install").write <<~SH
      #!/bin/bash
      exec "#{prefix}/install.sh" --source "#{prefix}/iterm2-harness.py" "$@"
    SH
    chmod 0755, bin/"iterm2-harness-install"
  end

  def post_install
    # Auto-symlink into iTerm2's AutoLaunch folder so the service starts on the
    # next iTerm2 launch without an extra command. Falls back gracefully when
    # running headless (e.g. CI) — the symlink target is created on demand.
    autolaunch_dir = "#{Dir.home}/Library/Application Support/iTerm2/Scripts/AutoLaunch"
    system prefix/"install.sh",
           "--source", "#{prefix}/iterm2-harness.py",
           "--target", autolaunch_dir
  end

  def caveats
    <<~EOS
      iterm2-harness has been installed to:
        #{prefix}/iterm2-harness.py

      A symlink was created automatically at:
        ~/Library/Application Support/iTerm2/Scripts/AutoLaunch/iterm2-harness.py
      iTerm2 will run the service on its next launch.

      Run it now without restarting iTerm2:
        iTerm2 menu > Scripts > AutoLaunch > iterm2-harness.py

      Manage the AutoLaunch link manually:
        iterm2-harness-install              # re-create the symlink
        iterm2-harness-install --uninstall  # remove the symlink (keep the formula)

      Default config lives at #{prefix}/config.json. Override host/port via:
        ITERM2_HARNESS_HOST, ITERM2_HARNESS_PORT
      or by editing the config.json next to the script.

      Tokens and audit logs are kept in ~/.iterm2-harness/.
    EOS
  end

  test do
    assert_predicate prefix/"iterm2-harness.py", :exist?
    assert_predicate prefix/"install.sh", :executable?
    assert_predicate bin/"iterm2-harness-install", :executable?
  end
end

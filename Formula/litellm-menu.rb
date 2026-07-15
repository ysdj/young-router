class LitellmMenu < Formula
  desc "macOS menu bar app for running a local LiteLLM service"
  homepage "https://github.com/ysdj/litellm-menu"
  url "https://github.com/ysdj/litellm-menu/releases/download/v1.0.1/litellm-menu-1.0.1-macos-arm64.tar.zst"
  version "1.0.1"
  sha256 "21e3d7ffe3a47c4ad9007770369f715f2de5a6246e61b4d501b61dc64e2e71ed"
  license "MIT"
  head "https://github.com/ysdj/litellm-menu.git", branch: "main"

  depends_on arch: :arm64
  depends_on :macos

  def install
    app = libexec/"LiteLLM Menu.app"
    app.install "Contents"

    app_resources = app/"Contents/Resources/App"
    (bin/"litellm-menu").write <<~SH
      #!/bin/bash
      set -euo pipefail
      export LITELLM_APP_PATH="#{app}"
      if [ "$#" -eq 0 ]; then
        set -- open
      fi
      exec "#{app_resources}/app.sh" "$@"
    SH

    (bin/"litellm-menu-service").write <<~SH
      #!/bin/bash
      set -euo pipefail
      export LITELLM_APP_PATH="#{app}"
      exec "#{app_resources}/service.sh" "$@"
    SH
    chmod 0755, bin/"litellm-menu", bin/"litellm-menu-service"
  end

  def caveats
    <<~EOS
      Start the menu app with:
        litellm-menu open

      The Homebrew package includes its Python runtime and LiteLLM dependencies.
      First launch starts the local service without downloading or compiling them.

      Restart after upgrades with:
        litellm-menu restart

      Direct service control is available as:
        litellm-menu-service status
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/litellm-menu version")
    assert_path_exists libexec/"LiteLLM Menu.app/Contents/MacOS/LiteLLMMenu"
    assert_path_exists libexec/"LiteLLM Menu.app/Contents/Resources/App/service.sh"
    assert_path_exists libexec/"LiteLLM Menu.app/Contents/Resources/App/runtime/bin/python"
    assert_path_exists libexec/"LiteLLM Menu.app/Contents/Resources/App/runtime/bin/litellm"
  end
end

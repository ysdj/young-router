class LitellmMenu < Formula
  desc "macOS menu bar app for running a local LiteLLM service"
  homepage "https://github.com/ysdj/litellm-menu"
  url "https://github.com/ysdj/litellm-menu.git", tag: "v1.0.0"
  version "1.0.0"
  license "MIT"
  head "https://github.com/ysdj/litellm-menu.git", branch: "main"

  depends_on :macos
  depends_on "uv"

  def install
    app = libexec/"LiteLLM Menu.app"
    ENV["LITELLM_APP_PATH"] = app.to_s
    ENV["LITELLM_UV_BIN"] = (Formula["uv"].opt_bin/"uv").to_s
    system "./mac_menu/build.sh"

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

      LiteLLM Menu does not require macOS to provide Python. The formula
      installs uv, and the app uses uv on first launch to create its private
      Python runtime under ~/.litellm-menu.

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
  end
end

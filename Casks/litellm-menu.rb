cask "litellm-menu" do
  version "1.0.1"
  sha256 "21e3d7ffe3a47c4ad9007770369f715f2de5a6246e61b4d501b61dc64e2e71ed"

  url "https://github.com/ysdj/litellm-menu/releases/download/v#{version}/litellm-menu-#{version}-macos-arm64.tar.zst"
  name "LiteLLM Menu"
  desc "Menu bar app for running a local LiteLLM service"
  homepage "https://github.com/ysdj/litellm-menu"

  depends_on arch: :arm64
  depends_on macos: :ventura

  app "LiteLLM Menu.app"

  postflight do
    system_command "/usr/bin/xattr",
                   args: ["-dr", "com.apple.quarantine", "#{appdir}/LiteLLM Menu.app"]
  end

  uninstall quit: "menu.litellm.menu"
end

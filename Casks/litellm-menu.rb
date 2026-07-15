cask "litellm-menu" do
  version "1.0.1,5"
  sha256 "a77e651e3101aab988ce6726eefc05e03164718f2bf2f068694327d161796e65"

  url "https://github.com/ysdj/litellm-menu/releases/download/v#{version.csv.first}/litellm-menu-#{version.csv.first}-#{version.csv.second}-macos-arm64.tar.zst"
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

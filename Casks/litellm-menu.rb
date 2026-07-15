cask "litellm-menu" do
  version "1.0.1,4"
  sha256 "b54d70f3fda909d49cd1748338c85a43f6a1f002fc783e9f637f2d6eb170568a"

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

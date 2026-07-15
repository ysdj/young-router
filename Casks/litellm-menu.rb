cask "litellm-menu" do
  version "1.0.1,3"
  sha256 "941e013e7c91c7e41321aa38ede7401312f695bbbc3dd3adab1ac44480ce69e0"

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

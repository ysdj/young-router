cask "young-router" do
  version "1.0.1,5"
  sha256 "a77e651e3101aab988ce6726eefc05e03164718f2bf2f068694327d161796e65"

  url "https://github.com/ysdj/young-router/releases/download/v#{version.csv.first}/young-router-#{version.csv.first}-#{version.csv.second}-macos-arm64.tar.zst"
  name "Young Router"
  desc "Menu bar app for running a local LiteLLM service"
  homepage "https://github.com/ysdj/young-router"

  depends_on arch: :arm64
  depends_on macos: :ventura

  app "Young Router.app"

  postflight do
    system_command "/usr/bin/xattr",
                   args: ["-dr", "com.apple.quarantine", "#{appdir}/Young Router.app"]
  end

  uninstall launchctl: [
              "young.router.config-watch",
              "young.router.login",
            ],
            quit:      "young.router.app"
end

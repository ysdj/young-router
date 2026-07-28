const path = require("node:path");
const { getDefaultConfig, mergeConfig } = require("@react-native/metro-config");

const appRoot = __dirname;
const workspaceRoot = path.resolve(appRoot, "../..");
const sharedRoot = path.join(workspaceRoot, "packages/shared");
const rnMacosRoot = path.join(workspaceRoot, "vendor/react-native-macos-0.85/packages/react-native");
const rnMacosWorkspaceRoot = path.join(workspaceRoot, "vendor/react-native-macos-0.85");
const virtualizedListsRoot = path.join(rnMacosWorkspaceRoot, "packages/virtualized-lists");
// Resolve through the host's declared dependency so this remains valid with
// pnpm, Yarn, or npm rather than assuming a package-manager store layout.
const babelRuntimeRoot = path.dirname(
  require.resolve("@babel/runtime/package.json", { paths: [appRoot] }),
);
const reactRoot = path.dirname(
  require.resolve("react/package.json", { paths: [appRoot] }),
);
const rnMacosDependenciesRoot = path.join(rnMacosWorkspaceRoot, "node_modules/.store");
const normalizeColorsRoot = path.join(rnMacosWorkspaceRoot, "packages/normalize-color");
const assetsRegistryRoot = path.join(rnMacosWorkspaceRoot, "packages/assets");
const workspaceDependencyStore = path.join(workspaceRoot, "node_modules/.pnpm");

module.exports = mergeConfig(getDefaultConfig(appRoot), {
  projectRoot: appRoot,
  watchFolders: [
    sharedRoot,
    rnMacosRoot,
    virtualizedListsRoot,
    babelRuntimeRoot,
    reactRoot,
    rnMacosDependenciesRoot,
    normalizeColorsRoot,
    assetsRegistryRoot,
    workspaceDependencyStore,
  ],
  resolver: {
    // @react-native/metro-config's app default only enables iOS and Android.
    // The Release bundle script passes --platform macos, so retain RN macOS's
    // platform list when layering this workspace's resolver settings on top.
    platforms: ["ios", "macos", "android"],
    extraNodeModules: {
      // Shared TS sources are transformed outside the app root. Point Metro
      // at the workspace runtime explicitly so Babel helper imports resolve
      // in a Release bundle as well as during development.
      "@babel/runtime": babelRuntimeRoot,
      react: reactRoot,
      "@react-native/normalize-colors": normalizeColorsRoot,
      "@react-native/assets-registry": assetsRegistryRoot,
      "react-native": rnMacosRoot,
      "react-native-macos": rnMacosRoot,
      "@react-native-macos/virtualized-lists": virtualizedListsRoot,
    },
    nodeModulesPaths: [
      path.join(appRoot, "node_modules"),
      path.join(workspaceRoot, "node_modules"),
      path.join(rnMacosRoot, "node_modules"),
      path.join(rnMacosWorkspaceRoot, "node_modules"),
    ],
  },
});

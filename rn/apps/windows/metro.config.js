const { getDefaultConfig, mergeConfig } = require("@react-native/metro-config");

const fs = require("node:fs");
const path = require("node:path");
// Metro 0.84 no longer exports ./src/*; its exclusionList lives behind the
// ./private/* alias there, while the hoisted Metro 0.81 the macOS app uses
// still answers only the old path. Try the old specifier first and fall back
// to the exported alias so one config builds on both Metro versions.
function loadExclusionList() {
  try {
    return require("metro-config/src/defaults/exclusionList");
  } catch {
    const mod = require("metro-config/private/defaults/exclusionList");
    return mod.default ?? mod;
  }
}
const exclusionList = loadExclusionList();

const appRoot = __dirname;
const workspaceRoot = path.resolve(appRoot, "../..");
const sharedRoot = path.join(workspaceRoot, "packages/shared");

const rnwPath = fs.realpathSync(
  path.resolve(require.resolve("react-native-windows/package.json"), ".."),
);

//

/**
 * Metro configuration
 * https://facebook.github.io/metro/docs/configuration
 *
 * @type {import('metro-config').MetroConfig}
 */

const config = {
  projectRoot: appRoot,
  // The workspace hoists every dependency to rn/node_modules, which sits
  // outside this app root; Metro only crawls what is watched, so the whole
  // workspace is watched rather than the shared sources alone.
  watchFolders: [workspaceRoot],
  resolver: {
    // Metro 0.84 (the version React Native 0.85 pins) enables package-exports
    // resolution by default, while the Metro 0.81 the macOS host bundles does
    // not. Under exports resolution this workspace cannot resolve
    // "@babel/runtime/helpers/interopRequireDefault": the package's map answers
    // only the extensionless helper subpaths. Turning the feature off restores
    // the file-based lookup both platforms were built against.
    unstable_enablePackageExports: false,
    // The shared package lives outside the Windows app root, so Metro's
    // lookup from packages/shared/src never walks into the workspace's
    // hoisted node_modules. Map the runtime helpers the Babel transform emits
    // to the hoisted copy explicitly.
    extraNodeModules: {
      "@babel/runtime": path.join(workspaceRoot, "node_modules/@babel/runtime"),
    },
    nodeModulesPaths: [
      path.join(appRoot, "node_modules"),
      path.join(workspaceRoot, "node_modules"),
    ],
    blockList: exclusionList([
      // This stops the React Native Windows CLI from causing Metro to crash if it is already running.
      new RegExp(
        `${path.resolve(appRoot, "windows").replace(/[/\\]/g, "/")}.*`,
      ),
      // This prevents the React Native Windows CLI from hitting generated MSBuild-file locks.
      new RegExp(`${rnwPath}/build/.*`),
      new RegExp(`${rnwPath}/target/.*`),
      /.*\.ProjectImports\.zip/,
    ]),
    //
  },
  transformer: {
    getTransformOptions: async () => ({
      transform: {
        experimentalImportSupport: false,
        inlineRequires: true,
      },
    }),
  },
};

module.exports = mergeConfig(getDefaultConfig(appRoot), config);

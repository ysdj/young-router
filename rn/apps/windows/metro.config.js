const { getDefaultConfig, mergeConfig } = require("@react-native/metro-config");

const fs = require("node:fs");
const path = require("node:path");
const exclusionList = require("metro-config/src/defaults/exclusionList");

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
  watchFolders: [sharedRoot],
  resolver: {
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

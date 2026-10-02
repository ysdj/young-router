#!/usr/bin/env node
// Render both platforms' React Native codegen specs from one source of truth.
//
// Every native control has two spec files (ui/macos/<X>NativeComponent.ts and
// ui/windows/<X>NativeComponent.ts) that differ only in their registered
// component name.  Keeping both by hand is how the two hosts drifted apart:
// one prop added on macOS, another declaring a name Windows never read.
//
// The source of truth is ui/nativeSpecs/<X>.spec.ts: an ordinary codegen spec
// plus the two platform component names.  A `// @macos-only` or
// `// @windows-only` marker above a prop keeps a platform difference explicit
// instead of implicit.  `--check` fails when a rendered file is not what this
// script would write, so the contract test can catch a hand edit.

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import ts from "typescript";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sharedUi = path.join(root, "packages/shared/src/ui");
const specsDir = path.join(sharedUi, "nativeSpecs");
const outputDirs = {
  macos: path.join(sharedUi, "macos"),
  windows: path.join(sharedUi, "windows"),
};
const checkOnly = process.argv.includes("--check");

function fail(message) {
  console.error(message);
  process.exit(1);
}

function declaredName(source, variable, required = true) {
  const statement = source.statements.find(
    (node) =>
      ts.isVariableStatement(node) &&
      node.declarationList.declarations.some(
        (declaration) => declaration.name.text === variable,
      ),
  );
  if (!statement) {
    if (required) fail(`${variable} is missing from a native spec source`);
    return null;
  }
  const declaration = statement.declarationList.declarations.find(
    (item) => item.name.text === variable,
  );
  if (!declaration || !declaration.initializer || !ts.isStringLiteral(declaration.initializer)) {
    fail(`${variable} must be a string literal`);
  }
  return { value: declaration.initializer.text, statement };
}

function propsInterface(source) {
  const node = source.statements.find(
    (statement) => ts.isInterfaceDeclaration(statement) && /Props$/.test(statement.name.text),
  );
  if (!node) fail("a native spec source must declare one Props interface");
  return node.name.text;
}

// A spec may exist on one platform only, or render under a different file name
// (the macOS text field is the Windows text input).  Both are stated by the
// source itself rather than assumed from its file name.
function stringArray(source, variable) {
  const statement = source.statements.find(
    (node) =>
      ts.isVariableStatement(node) &&
      node.declarationList.declarations.some((declaration) => declaration.name.text === variable),
  );
  if (!statement) return null;
  const declaration = statement.declarationList.declarations.find(
    (item) => item.name.text === variable,
  );
  if (!declaration || !declaration.initializer || !ts.isArrayLiteralExpression(declaration.initializer)) {
    fail(`${variable} must be an array literal`);
  }
  return {
    statement,
    values: declaration.initializer.elements.map((element) => {
      if (!ts.isStringLiteral(element)) fail(`${variable} must contain string literals`);
      return element.text;
    }),
  };
}

function outputFiles(source) {
  const statement = source.statements.find(
    (node) =>
      ts.isVariableStatement(node) &&
      node.declarationList.declarations.some((declaration) => declaration.name.text === "OUTPUT_FILES"),
  );
  if (!statement) return { statement: null, files: {} };
  const declaration = statement.declarationList.declarations.find(
    (item) => item.name.text === "OUTPUT_FILES",
  );
  if (!declaration || !declaration.initializer || !ts.isObjectLiteralExpression(declaration.initializer)) {
    fail("OUTPUT_FILES must be an object literal");
  }
  const files = {};
  for (const property of declaration.initializer.properties) {
    if (!ts.isPropertyAssignment(property) || !ts.isStringLiteral(property.initializer)) {
      fail("OUTPUT_FILES must map a platform to a string literal");
    }
    files[property.name.text] = property.initializer.text;
  }
  return { statement, files };
}

function render(text, source, platform, componentName, specFile) {
  const lines = text.split("\n");
  const drop = new Set();
  for (const variable of ["APPKIT_COMPONENT_NAME", "WINUI_COMPONENT_NAME"]) {
    const declared = declaredName(source, variable, false);
    if (declared) dropStatement(source, declared.statement, drop);
  }
  const platforms = stringArray(source, "PLATFORMS");
  if (platforms) dropStatement(source, platforms.statement, drop);
  const outputs = outputFiles(source);
  if (outputs.statement) dropStatement(source, outputs.statement, drop);

  const rendered = [];
  let pendingPlatform = null;
  // The source's own header comment documents the source file; it does not
  // belong in the rendered pair (which carries its own generated header).
  let inSourceHeader = true;
  for (let index = 0; index < lines.length; index += 1) {
    if (drop.has(index)) continue;
    const line = lines[index];
    if (inSourceHeader) {
      if (line.startsWith("//") || line.trim() === "") continue;
      inSourceHeader = false;
    }
    const marker = /^\s*\/\/\s*@(macos|windows)-only\s*$/.exec(line);
    if (marker) {
      pendingPlatform = marker[1];
      continue;
    }
    if (pendingPlatform) {
      const trimmed = line.trim();
      if (
        trimmed === "" ||
        trimmed.startsWith("//") ||
        trimmed.startsWith("/*") ||
        trimmed.startsWith("*") ||
        trimmed.startsWith("*/")
      ) {
        rendered.push(line);
        continue;
      }
      // A marked prop covers the declaration through its terminating line.
      const block = [line];
      let cursor = index;
      while (!/;\s*$/.test(lines[cursor].trimEnd()) && cursor + 1 < lines.length) {
        cursor += 1;
        block.push(lines[cursor]);
      }
      if (pendingPlatform === platform) rendered.push(...block);
      index = cursor;
      pendingPlatform = null;
      continue;
    }
    if (line.trim() === "" && rendered.length > 0 && rendered[rendered.length - 1].trim() === "") {
      continue;
    }
    rendered.push(line);
  }

  const header =
    `// Generated by rn/scripts/gen-native-specs.mjs from ui/nativeSpecs/${specFile};\n` +
    "// edit the source file, not this one.\n\n";
  const body = rendered.join("\n").replace(/\s+$/, "");
  return (
    header +
    body +
    `\n\nexport default codegenNativeComponent<${propsInterface(source)}>(${JSON.stringify(componentName)});\n`
  );
}

const specFiles = fs
  .readdirSync(specsDir)
  .filter((name) => name.endsWith(".spec.ts"))
  .sort();
if (specFiles.length === 0) fail(`No native spec sources found in ${specsDir}`);

function dropStatement(source, statement, drop) {
  const startLine = source.getLineAndCharacterOfPosition(statement.getStart(source)).line;
  const endLine = source.getLineAndCharacterOfPosition(statement.getEnd()).line;
  for (let index = startLine; index <= endLine; index += 1) drop.add(index);
}

let stale = 0;
for (const specFile of specFiles) {
  const specPath = path.join(specsDir, specFile);
  const text = fs.readFileSync(specPath, "utf8");
  const source = ts.createSourceFile(specPath, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const base = specFile.replace(/\.spec\.ts$/, "");
  const platforms = stringArray(source, "PLATFORMS")?.values ?? ["macos", "windows"];
  const fileNames = outputFiles(source).files;
  const names = {
    macos: declaredName(source, "APPKIT_COMPONENT_NAME")?.value,
    windows: declaredName(source, "WINUI_COMPONENT_NAME", false)?.value,
  };
  for (const platform of ["macos", "windows"]) {
    if (!platforms.includes(platform)) continue;
    const target = path.join(
      outputDirs[platform],
      fileNames[platform] ?? `${base}NativeComponent.ts`,
    );
    if (!names[platform]) fail(`${base} declares no ${platform} component name`);
    const expected = render(text, source, platform, names[platform], specFile);
    if (checkOnly) {
      const current = fs.existsSync(target) ? fs.readFileSync(target, "utf8") : "";
      if (current !== expected) {
        console.error(`${path.relative(root, target)} is stale; run \`pnpm run specs:generate\`.`);
        stale += 1;
      }
    } else {
      fs.writeFileSync(target, expected);
    }
  }
}
if (checkOnly) {
  if (stale > 0) fail(`${stale} native spec file(s) are out of date.`);
  console.log(`native specs are current (${specFiles.length} sources).`);
} else {
  console.log(`rendered ${specFiles.length} native spec source(s).`);
}

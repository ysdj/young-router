#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import ts from "typescript";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const coreSchemaPath = path.resolve(root, "..", "litellm_menu", "core", "ipc-v1.schema.json");
const packageJson = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
const coreSchema = JSON.parse(fs.readFileSync(coreSchemaPath, "utf8"));
const typesPath = process.env.LITELLM_MENU_CONTRACT_TYPES || path.join(root, "packages/shared/src/types.ts");
const types = fs.readFileSync(typesPath, "utf8");
const ipc = fs.readFileSync(path.join(root, "packages/shared/src/ipc.ts"), "utf8");
const source = ts.createSourceFile(typesPath, types, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
const printer = ts.createPrinter({ removeComments: true });

const requiredRoutes = ["providers-models", "codex-settings", "claude-settings", "runtime-settings", "webdav-settings", "logs"];

function fail(message) {
  throw new Error(message);
}

function declaration(name, kind) {
  const node = source.statements.find((statement) => kind(statement) && statement.name?.text === name);
  if (!node) fail(`TypeScript declaration is missing: ${name}`);
  return node;
}

function compact(value) {
  return value.replace(/\s+/gu, "").replace(/;\}/gu, "}");
}

function printType(node) {
  return compact(printer.printNode(ts.EmitHint.Unspecified, node, source));
}

function propertyName(node) {
  if (ts.isIdentifier(node) || ts.isStringLiteral(node) || ts.isNumericLiteral(node)) return node.text;
  fail("IPC contract uses an unsupported TypeScript property name");
}

function interfaceMembers(name) {
  const node = declaration(name, ts.isInterfaceDeclaration);
  const result = new Map();
  for (const member of node.members) {
    if (!ts.isPropertySignature(member) || !member.type) fail(`${name} must contain only typed properties`);
    const key = propertyName(member.name);
    if (result.has(key)) fail(`${name} contains a duplicate property: ${key}`);
    result.set(key, member.type);
  }
  return result;
}

function unionStringLiterals(name) {
  const node = declaration(name, ts.isTypeAliasDeclaration);
  const members = ts.isUnionTypeNode(node.type) ? node.type.types : [node.type];
  return members.map((member) => {
    if (!ts.isLiteralTypeNode(member) || !ts.isStringLiteral(member.literal)) {
      fail(`${name} must be a string-literal union`);
    }
    return member.literal.text;
  });
}

function definition(schema) {
  if (schema?.$ref) {
    const prefix = "#/$defs/";
    if (!schema.$ref.startsWith(prefix)) fail(`unsupported schema reference: ${schema.$ref}`);
    const resolved = coreSchema.$defs?.[schema.$ref.slice(prefix.length)];
    if (!resolved) fail(`missing schema reference: ${schema.$ref}`);
    return resolved;
  }
  return schema;
}

function schemaType(schema) {
  const value = definition(schema);
  if (typeof value?.["x-typescript-type"] === "string") return compact(value["x-typescript-type"]);
  if (Object.hasOwn(value, "const")) {
    if (typeof value.const === "string") return JSON.stringify(value.const);
    return String(value.const);
  }
  if (Array.isArray(value?.enum)) return value.enum.map((item) => JSON.stringify(item)).join("|");
  if (Array.isArray(value?.oneOf) && value?.type !== "object") return value.oneOf.map(schemaType).join("|");
  if (Array.isArray(value?.type)) return value.type.map((item) => schemaType({ type: item })).join("|");
  if (value?.type === "string") return "string";
  if (value?.type === "integer" || value?.type === "number") return "number";
  if (value?.type === "boolean") return "boolean";
  if (value?.type === "null") return "null";
  if (value?.type === "array") return `${schemaType(value.items)}[]`;
  if (value?.type !== "object") fail("schema cannot be converted to a TypeScript type");
  const required = new Set(value.required ?? []);
  const properties = value.properties ?? {};
  const base = `{${Object.entries(properties).map(([name, child]) => `${name}${required.has(name) ? "" : "?"}:${schemaType(child)}`).join(";")}}`;
  if (!Array.isArray(value.oneOf)) return base;
  const choices = value.oneOf.map((choice) => {
    if (!Array.isArray(choice?.required) || choice.required.length !== 1) fail("object oneOf must select one property");
    const selected = choice.required[0];
    if (!properties[selected]) fail("object oneOf selects an unknown property");
    const excluded = value.oneOf
      .flatMap((other) => other?.required ?? [])
      .filter((name) => name !== selected);
    return `{${selected}:${schemaType(properties[selected])}${excluded.map((name) => `;${name}?:never`).join("")}}`;
  }).join("|");
  const shared = Object.entries(properties)
    .filter(([name]) => !value.oneOf.some((choice) => choice.required?.includes(name)))
    .map(([name, child]) => `${name}${required.has(name) ? "" : "?"}:${schemaType(child)}`)
    .join(";");
  return `(${choices})&{${shared}}`;
}

function assertMethodTypeMap(name, contractField) {
  const members = interfaceMembers(name);
  const methods = coreSchema.methods;
  if (members.size !== methods.length || methods.some((method) => !members.has(method))) {
    fail(`${name} method keys diverge from x-method-contracts`);
  }
  for (const method of methods) {
    const expected = schemaType(coreSchema["x-method-contracts"]?.[method]?.[contractField]);
    const actual = printType(members.get(method));
    if (actual !== expected) {
      fail(`${name}.${method} diverges from schema: expected ${expected}, received ${actual}`);
    }
  }
}

if (packageJson.private !== true || packageJson.name !== "@litellm-menu/rn-app") {
  fail("rn/package.json must remain a private @litellm-menu package");
}
if (!ipc.includes("createIpcClient")) fail("versioned typed IPC client is missing");
if (coreSchema.protocol_version !== 1 || coreSchema.request?.properties?.protocol_version?.const !== 1) {
  fail("Python Core IPC schema does not declare protocol v1");
}
const methods = coreSchema.methods;
if (!Array.isArray(methods) || methods.length === 0 || new Set(methods).size !== methods.length) {
  fail("Python Core IPC method set is invalid");
}
if (unionStringLiterals("IpcMethod").join("\u0000") !== methods.join("\u0000")) {
  fail("IpcMethod diverges from the schema method set");
}
const versionStatement = source.statements.find((statement) =>
  ts.isVariableStatement(statement)
  && statement.declarationList.declarations.some((item) => ts.isIdentifier(item.name) && item.name.text === "IPC_PROTOCOL_VERSION"),
);
const version = versionStatement?.declarationList.declarations.find((item) => ts.isIdentifier(item.name) && item.name.text === "IPC_PROTOCOL_VERSION")?.initializer;
if (!version || !types.slice(version.pos, version.end).includes(String(coreSchema.protocol_version))) {
  fail("IPC_PROTOCOL_VERSION diverges from the schema");
}
if (!Array.isArray(coreSchema.request?.allOf) || coreSchema.request.allOf.length !== methods.length) {
  fail("Python Core IPC schema must discriminate every method envelope");
}
for (const method of methods) {
  const contract = coreSchema["x-method-contracts"]?.[method];
  if (!contract?.params || !contract?.result) fail(`schema contract is missing: ${method}`);
  const params = definition(contract.params);
  if (params.additionalProperties !== false) fail(`strict IPC params schema is missing: ${method}`);
}
assertMethodTypeMap("IpcParams", "params");
assertMethodTypeMap("IpcResults", "result");
const dispatchAction = interfaceMembers("DispatchAction");
const expectedDispatchAction = schemaType(coreSchema.$defs.dispatchAction);
const actualDispatchAction = `{${[...dispatchAction.entries()].map(([name, type]) => `${name}${type.parent.questionToken ? "?" : ""}:${printType(type)}`).join(";")}}`;
const expandedDispatchAction = coreSchema.$defs.dispatchAction?.["x-typescript-type"] === "DispatchAction"
  ? actualDispatchAction
  : expectedDispatchAction;
if (compact(actualDispatchAction) !== compact(expandedDispatchAction)) {
  fail(`DispatchAction diverges from schema: expected ${expectedDispatchAction}, received ${actualDispatchAction}`);
}

const applyProperties = definition(coreSchema["x-method-contracts"].apply.params).properties ?? {};
if (!applyProperties.domain || !applyProperties.domains || !applyProperties.revision) {
  fail("single- and multi-domain Apply schemas are required");
}
const importDefinition = definition(coreSchema["x-method-contracts"].import.params);
if (!importDefinition.required?.includes("revision")) fail("Import must require a revision");
if (coreSchema.event?.properties?.event?.const !== "snapshot") fail("Python Core IPC event contract is invalid");
const event = interfaceMembers("IpcEvent").get("event");
if (!event || printType(event) !== '"snapshot"') fail("TypeScript IPC event contract diverges from schema");
const routes = unionStringLiterals("AppRoute");
for (const route of requiredRoutes) {
  if (!routes.includes(route)) fail(`route missing: ${route}`);
}
for (const forbidden of ["sk-", "ANTHROPIC_AUTH_TOKEN", "/Users/", "config.yaml", "settings.json"]) {
  if (types.includes(forbidden) || ipc.includes(forbidden)) fail(`sensitive/config value leaked into shared contract: ${forbidden}`);
}
console.log(`RN contract OK (IPC v${coreSchema.protocol_version}; ${methods.length} schema-checked methods; ${requiredRoutes.length} routes)`);

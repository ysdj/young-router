import {
  IPC_PROTOCOL_VERSION,
  type ConfigDomain,
  type DispatchAction,
  type IpcClient,
  type IpcEndpoint,
  type IpcEvent,
  type IpcMethod,
  type IpcParams,
  type IpcRequest,
  type IpcResponse,
  type IpcResults,
  type IpcTransport,
  type CoreSnapshot,
  type ValidationSummary,
} from "./types";

export class IpcProtocolError extends Error {
  readonly code: string;
  readonly retryable: boolean;

  constructor(code: string, message: string, retryable = false) {
    super(message);
    this.name = "IpcProtocolError";
    this.code = code;
    this.retryable = retryable;
  }
}

function requestId(): string {
  return `rn-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function isProtocolResponse<M extends IpcMethod>(response: IpcResponse<M>, expectedRequestId: string): boolean {
  return response.protocol_version === IPC_PROTOCOL_VERSION && response.request_id === expectedRequestId;
}

export function createIpcClient(transport: IpcTransport, endpoint?: IpcEndpoint): IpcClient {
  let latestSnapshot: CoreSnapshot | undefined;
  let initialSnapshotRequest: Promise<CoreSnapshot> | undefined;
  const snapshotListeners = new Set<(event: IpcEvent) => void>();
  let unsubscribeTransport: (() => void) | undefined;
  let subscriptionStarted = false;

  function rememberSnapshot(next: CoreSnapshot): CoreSnapshot {
    // Snapshot revisions order mutations. Keep same-revision projections too:
    // they may carry newer live log data.
    if (!latestSnapshot || next.revision >= latestSnapshot.revision) latestSnapshot = next;
    return latestSnapshot;
  }

  async function call<M extends IpcMethod>(method: M, params: IpcParams[M]): Promise<IpcResults[M]> {
    const request: IpcRequest<M> = {
      protocol_version: IPC_PROTOCOL_VERSION,
      request_id: requestId(),
      method,
      params,
    };
    const response = await transport.send(request);
    if (!isProtocolResponse(response, request.request_id)) {
      throw new IpcProtocolError("invalid_response", "The local core returned an invalid IPC response.", true);
    }
    if (!response.ok || response.result === undefined) {
      const error = response.error;
      throw new IpcProtocolError(error?.code ?? "core_error", error?.message ?? "The local core rejected the request.", error?.retryable ?? false);
    }
    return response.result;
  }

  return {
    endpoint,
    latestSnapshot: (): CoreSnapshot | undefined => latestSnapshot,
    snapshot: (): Promise<CoreSnapshot> => {
      if (latestSnapshot) return call("snapshot", {}).then(({ snapshot }) => rememberSnapshot(snapshot));
      if (!initialSnapshotRequest) {
        const promise = call("snapshot", {}).then(({ snapshot }) => rememberSnapshot(snapshot)).finally(() => {
          if (initialSnapshotRequest === promise) initialSnapshotRequest = undefined;
        });
        initialSnapshotRequest = promise;
      }
      return initialSnapshotRequest;
    },
    logs: async (tab, revision) => call("logs", revision === undefined ? { tab } : { tab, revision }),
    editor: async (domain, document): Promise<IpcResults["editor"]> => call("editor", { domain, document }),
    stageEditor: async (editorToken, text): Promise<IpcResults["editor"]> => call("editor", { editor_token: editorToken, text }),
    dispatch: async (action: DispatchAction, revision?: number): Promise<{ revision: number }> => call("dispatch", revision === undefined ? { action } : { action, revision }),
    subscribe: (listener: (event: IpcEvent) => void, topics?: string[]): (() => void) => {
      snapshotListeners.add(listener);
      if (!unsubscribeTransport) {
        unsubscribeTransport = transport.subscribe((event) => {
          rememberSnapshot(event.snapshot);
          for (const snapshotListener of snapshotListeners) snapshotListener(event);
        });
      }
      if (!subscriptionStarted) {
        subscriptionStarted = true;
        void call("subscribe", topics ? { topics } : {}).catch(() => {
          subscriptionStarted = false;
        });
      }
      return () => {
        snapshotListeners.delete(listener);
      };
    },
    validate: async (domain: ConfigDomain, revision?: number): Promise<ValidationSummary> => (await call("validate", revision === undefined ? { domain } : { domain, revision })).validate,
    apply: async (domain: ConfigDomain, revision: number, confirmation?: string | string[]): Promise<IpcResults["apply"]> => call("apply", confirmation === undefined ? { domain, revision } : { domain, revision, confirmation }),
    applyDomains: async (domains: ConfigDomain[], revision: number, confirmation?: string | string[]): Promise<IpcResults["apply"]> => call("apply", confirmation === undefined ? { domains, revision } : { domains, revision, confirmation }),
    reload: async (domain?: ConfigDomain, revision?: number): Promise<{ revision: number }> => call("reload", domain === undefined && revision === undefined ? {} : { ...(domain === undefined ? {} : { domain }), ...(revision === undefined ? {} : { revision }) }),
    probe: async (providerId?: string, modelId?: string, domain?: "providers_models" | "webdav"): Promise<IpcResults["probe"]> => call("probe", {
      ...(domain === undefined ? {} : { domain }),
      ...(providerId === undefined ? {} : { provider_id: providerId }),
      ...(modelId === undefined ? {} : { model_id: modelId }),
    }),
    export: async (sections: ConfigDomain[], destinationToken: string): Promise<IpcResults["export"]> => call("export", { sections, destination_token: destinationToken }),
    previewImport: async (sourceToken: string, revision: number): Promise<IpcResults["import_preview"]> => call("import_preview", { source_token: sourceToken, revision }),
    importPlan: async (importPlanToken: string, revision: number, sections: ConfigDomain[]): Promise<IpcResults["import"]> => call("import", { import_plan_token: importPlanToken, sections, revision }),
  };
}

import {
  IPC_PROTOCOL_VERSION,
  type ConfigDomain,
  type DispatchAction,
  type IpcClient,
  type IpcEndpoint,
  type IpcEvent,
  type IpcEventTopic,
  type IpcMethod,
  type IpcParams,
  type IpcRequest,
  type IpcResponse,
  type IpcResults,
  type IpcTransport,
  type JobEvent,
  type CoreSnapshot,
  type ValidationSummary,
} from "./types";
import { mergeSnapshot } from "./applyPatch";

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

/**
 * How long a probe's job may take to answer once it has started.
 *
 * This is the budget the native hosts grant the old synchronous probe request,
 * moved to where it belongs: the start of the work answers at once, so only
 * the wait for the job's own result is bounded by it.
 */
const JOB_RESULT_TIMEOUT_MS = 900_000;

/** How many finished jobs are kept so a late waiter still finds its answer. */
const JOB_EVENT_MEMORY = 32;

export function createIpcClient(transport: IpcTransport, endpoint?: IpcEndpoint): IpcClient {
  let latestSnapshot: CoreSnapshot | undefined;
  let initialSnapshotRequest: Promise<CoreSnapshot> | undefined;
  const snapshotListeners = new Set<(event: IpcEvent) => void>();
  let unsubscribeTransport: (() => void) | undefined;
  let subscriptionStarted = false;
  // Jobs this client is waiting on, and the answers that arrived before their
  // waiter was registered: a probe can finish between the dispatch response
  // and the next statement, and that answer must not be lost because of it.
  const jobWaiters = new Map<string, { settle: (event: JobEvent) => void; timer: ReturnType<typeof setTimeout> }>();
  const finishedJobs = new Map<string, JobEvent>();

  function rememberFinishedJob(event: JobEvent): void {
    if (event.state === "running") return;
    const waiter = jobWaiters.get(event.job_id);
    if (waiter) {
      jobWaiters.delete(event.job_id);
      clearTimeout(waiter.timer);
      waiter.settle(event);
      return;
    }
    // An answer nobody is waiting on yet: keep it for the waiter that is about
    // to be created, and drop the oldest so this stays bounded.
    finishedJobs.delete(event.job_id);
    finishedJobs.set(event.job_id, event);
    while (finishedJobs.size > JOB_EVENT_MEMORY) {
      const oldest = finishedJobs.keys().next().value;
      if (oldest === undefined) break;
      finishedJobs.delete(oldest);
    }
  }

  function jobProbeResult(event: JobEvent): IpcResults["probe"] {
    const result = event.result;
    if (event.state === "failed" || result === undefined) {
      const detail = typeof result?.detail === "string" ? result.detail : "The probe failed.";
      return { ok: false, protocols: [], detail };
    }
    // Core answers a probe job with the payload the synchronous probe used to
    // return; the cast is this boundary's only claim about it, and every
    // consumer already treats the fields as optional.
    return result as IpcResults["probe"];
  }

  function awaitJob(jobId: string): Promise<IpcResults["probe"]> {
    const finished = finishedJobs.get(jobId);
    if (finished) {
      finishedJobs.delete(jobId);
      return Promise.resolve(jobProbeResult(finished));
    }
    ensureEventStream();
    return new Promise<IpcResults["probe"]>((resolve, reject) => {
      const timer = setTimeout(() => {
        jobWaiters.delete(jobId);
        reject(new IpcProtocolError("probe_timeout", "The probe did not answer in time.", true));
      }, JOB_RESULT_TIMEOUT_MS);
      jobWaiters.set(jobId, {
        settle: (event) => {
          if (event.state === "failed") {
            reject(new IpcProtocolError("probe_failed", jobProbeResult(event).detail ?? "The probe failed.", true));
            return;
          }
          resolve(jobProbeResult(event));
        },
        timer,
      });
    });
  }

  function ensureEventStream(topics?: IpcEventTopic[]): void {
    if (!unsubscribeTransport) {
      unsubscribeTransport = transport.subscribe((event) => {
        // A snapshot is still the only state event this layer reads: patches
        // and resyncs describe the same state and are handed to the UI, which
        // is what decides how to apply or replace them.  A job is one caller's
        // operation, so it is joined back to that caller here.
        if (event.event === "snapshot") rememberSnapshot(event.snapshot);
        if (event.event === "job") rememberFinishedJob(event);
        for (const snapshotListener of snapshotListeners) snapshotListener(event);
      });
    }
    if (!subscriptionStarted) {
      subscriptionStarted = true;
      void call("subscribe", topics ? { topics } : {}).catch(() => {
        subscriptionStarted = false;
      });
    }
  }

  function rememberSnapshot(next: CoreSnapshot): CoreSnapshot {
    // Snapshot revisions order mutations.  The head orders the live fields and
    // the projection orders the domains, and a snapshot answered mid-write can
    // advance one without the other; merging keeps the newer half of each, so
    // a slow answer to an earlier read cannot walk the state back.  Same-
    // revision projections are still kept: they may carry newer live log data.
    latestSnapshot = mergeSnapshot(latestSnapshot, next);
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
    // A host-only route travels beside the protocol rather than through it,
    // so it is passed on only when this transport has one.
    hostRoute: transport.hostRoute ? (path, body) => transport.hostRoute!(path, body) : undefined,
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
    diskState: (domains: ConfigDomain[]): Promise<IpcResults["disk_state"]> => call("disk_state", { domains }),
    logs: async (tab, revision) => call("logs", revision === undefined ? { tab } : { tab, revision }),
    editor: async (domain, document): Promise<IpcResults["editor"]> => call("editor", { domain, document }),
    files: async (): Promise<IpcResults["files"]> => call("files", {}),
    stageEditor: async (editorToken, text): Promise<IpcResults["editor"]> => call("editor", { editor_token: editorToken, text }),
    dispatch: async (action: DispatchAction, revision?: number): Promise<IpcResults["dispatch"]> => call("dispatch", revision === undefined ? { action } : { action, revision }),
    subscribe: (listener: (event: IpcEvent) => void, topics?: IpcEventTopic[]): (() => void) => {
      snapshotListeners.add(listener);
      ensureEventStream(topics);
      return () => {
        snapshotListeners.delete(listener);
      };
    },
    validate: async (domain: ConfigDomain, revision?: number): Promise<ValidationSummary> => (await call("validate", revision === undefined ? { domain } : { domain, revision })).validate,
    apply: async (domain: ConfigDomain, revision: number, confirmation?: string | string[]): Promise<IpcResults["apply"]> => call("apply", confirmation === undefined ? { domain, revision } : { domain, revision, confirmation }),
    applyDomains: async (domains: ConfigDomain[], revision: number, confirmation?: string | string[]): Promise<IpcResults["apply"]> => call("apply", confirmation === undefined ? { domains, revision } : { domains, revision, confirmation }),
    reload: async (domain?: ConfigDomain, revision?: number): Promise<{ revision: number }> => call("reload", domain === undefined && revision === undefined ? {} : { ...(domain === undefined ? {} : { domain }), ...(revision === undefined ? {} : { revision }) }),
    // A probe is started as a job and joined to the event that answers it.
    // The synchronous probe request still exists for compatibility, but it
    // holds the whole request (and the native host's 900 s budget) for as long
    // as the upstream takes; this path answers with an id at once.
    probe: async (providerId?: string, modelId?: string, domain?: "providers_models" | "webdav", values?: Record<string, unknown>): Promise<IpcResults["probe"]> => {
      const target = domain ?? "providers_models";
      const started = await call("dispatch", {
        action: {
          domain: target,
          type: "probe",
          ...(providerId === undefined && modelId === undefined && values === undefined
            ? {}
            : {
              payload: {
                ...(providerId === undefined ? {} : { provider_id: providerId }),
                ...(modelId === undefined ? {} : { model_id: modelId }),
                ...(values === undefined ? {} : { values }),
              },
            }),
        },
      });
      const jobId = started.job_id;
      if (jobId === undefined) {
        throw new IpcProtocolError("probe_failed", "The local core did not start the probe.", true);
      }
      return awaitJob(jobId);
    },
    export: async (sections: ConfigDomain[], destinationToken: string): Promise<IpcResults["export"]> => call("export", { sections, destination_token: destinationToken }),
    previewImport: async (sourceToken: string, revision: number): Promise<IpcResults["import_preview"]> => call("import_preview", { source_token: sourceToken, revision }),
    webLaunchUrl: async (): Promise<string> => (await call("web_launch_url", {})).url,
    importPlan: async (importPlanToken: string, revision: number, sections: ConfigDomain[]): Promise<IpcResults["import"]> => call("import", { import_plan_token: importPlanToken, sections, revision }),
  };
}

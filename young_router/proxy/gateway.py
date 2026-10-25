"""The LiteLLM app plus the provider-side endpoints this app serves itself.

Codex derives a provider-relative route for standalone web search
(``{base_url}/alpha/search``) and calls it on whichever gateway the client
points at.  LiteLLM answers every unknown path with 404, so the route is added
to its own app object *before* uvicorn imports it -- the app keeps its own
lifespan, middleware and startup, which a mounted sub-application would lose.

Keep this module import-only: the launcher preloads it in the forkserver, so
everything it imports is imported once for every worker.
"""

from litellm.proxy.proxy_server import app as app  # noqa: F401  (uvicorn target)

from . import reasoning_capability as _reasoning_capability
from .search_endpoint import register as _register_search_endpoint

_register_search_endpoint(app)
_reasoning_capability.register()

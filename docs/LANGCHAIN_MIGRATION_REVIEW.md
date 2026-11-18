# LangChain migration review

The Web AI endpoint now constructs `ChatDeepSeek` and runs a LangChain
`create_agent` graph. Registered read-only diagnostic tools are exposed through
the RAB registry bridge. The bridge enforces the selected profile, maps
application targets to per-run aliases, and reduces observations before they
are sent to the model. LangChain tool-based structured output is validated
again against locally collected evidence. The model is limited to six calls,
one tool call per response, 30 seconds per request, zero SDK retries and 1024
output tokens per request. LangSmith tracing is disabled for these runs.

The legacy agent protocol remains as a compatibility path for existing
validation tools. SSH, tunnel, runtime and Doctor implementations are not
replaced. Failure of AI diagnostics does not stop those services.

## Offline validation

* Project virtual environment: Python tests 1003 passed; separate spike and
  validation suites 32 passed; dependency check passed.
* Frontend: 93 tests passed; production build passed.
* Actual LangChain graph and ChatDeepSeek adapter were exercised with an
  in-process fake chat model and an HTTP mock transport, including multi-step
  tool calls, grounded output, invalid evidence, parallel-call rejection,
  timeout, scope checks and a FastAPI request.
* Locked dependencies were installed in a newly created separate virtual
  environment, followed by installation of the local project.

## Limits

The user reports a successful real DeepSeek isolated probe on the new
ChatDeepSeek path: two fixture tools succeeded and the diagnosis was grounded
in fixture evidence. This does not validate a real Linux server or the complete
browser workflow. Real Linux and browser end-to-end verification remain necessary. Free-form
user questions cannot be guaranteed to contain no sensitive information;
the UI must obtain consent and users should avoid entering credentials.

Reproducible installation on the validated Windows/Python 3.10 environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest
```

# Environment notes

## PyPI download throughput

`files.pythonhosted.org` is heavily throttled from this network. Measured on
2026-10-22:

| Fetch | Result |
|---|---|
| `pypi.org/pypi/<pkg>/json` (index/metadata) | 200 in ~4.5s — fine |
| 1.2 MB wheel from `files.pythonhosted.org` | 200 in ~12s (~100 KB/s) |
| 11.8 MB wheel from `files.pythonhosted.org` | times out at 0 bytes, with both `uv` and `curl` |
| 11.8 MB wheel from `mirrors.aliyun.com` | 200 in ~12s (~1 MB/s) |
| 11.8 MB wheel from `pypi.tuna.tsinghua.edu.cn` | 200 in ~10s (~1.2 MB/s) |

The mirror was verified to serve **byte-identical** artifacts. For
`grpcio-1.84.0-cp312-cp312-macosx_11_0_universal2.whl`:

```
PyPI    size 12339195  sha256 406583b4e8fb2282ebd392e12b963e601c1f82e07125a8c2cb5b144e7e024796
Mirror  size 12339195  sha256 406583b4e8fb2282ebd392e12b963e601c1f82e07125a8c2cb5b144e7e024796
```

`uv` and `pip` both verify each downloaded artifact against the hash in the
index metadata, so a tampered file fails the install rather than landing in
the environment.

**How to install in this environment:**

```bash
UV_DEFAULT_INDEX="https://mirrors.aliyun.com/pypi/simple/" \
  uv pip install --python .venv/bin/python -e ".[dev]"
```

This is an environment workaround, not a project dependency. The project
itself declares no mirror and works against stock PyPI anywhere the
network is not throttled. If you prefer first-party PyPI, drop the
`UV_DEFAULT_INDEX` prefix and expect a slow install with possible
timeouts.

## Python version

3.12.13, managed by `uv`. Chosen for native-wheel coverage: `onnxruntime`
publishes cp311–cp314 and `livekit-local-inference` publishes cp310–cp314
for macOS arm64.

## LiveKit deployment

LiveKit Cloud only. The project URL, API key and API secret come from
`.env` (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`). The agent
worker runs locally and connects to the cloud project; there is no
container runtime and no local LiveKit server.

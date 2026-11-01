# m3u8-cli

English | [简体中文](README.zh-CN.md)

A Python-based HLS (m3u8) video downloader and format converter: it downloads segments concurrently, supports AES-128 decryption, keeps a local cache for resumable downloads, and finally remuxes the result into MP4 losslessly with FFmpeg. It can also convert local videos to HLS or other common formats.

## Features

- **Quality selection**: automatically parses the master playlist and picks the variant with the highest `BANDWIDTH`.
- **Concurrent downloads**: multi-threaded segment downloads, 8 by default, adjustable as needed.
- **AES-128 decryption**: supports standard HLS AES-128 encryption (`KEYFORMAT=identity`).
- **Resumable downloads**: segments are cached locally and reused when a download is run again after an interruption.
- **Task isolation**: the final video and intermediate cache live in a subdirectory derived from the output name.
- **Lossless remuxing**: segments are merged locally, then a single FFmpeg call packages them as MP4 with `-c copy` and `+faststart`.
- **Format conversion**: converts local videos to HLS, MP4, MKV or WebM, with H.264, HEVC and stream copy.

## Requirements

- Python 3.12+
- [FFmpeg](https://ffmpeg.org/) (`ffmpeg` must be available on `PATH`)
- [uv](https://docs.astral.sh/uv/) or pip (either one is enough for installation)

## Installation

### With uv (recommended)

Install from PyPI as a global command:

```bash
uv tool install m3u8-cli
```

Upgrade:

```bash
uv tool upgrade m3u8-cli
```

You can also clone the repository and install from source:

```bash
git clone <repository-url>
cd m3u8-cli
uv tool install .
```

After installation `m3u8-cli` is already on `PATH`, so you can run it directly without the `uv run` prefix.

For development, run `uv sync` inside the repository to install dependencies into the project `.venv`; in that case commands need the `uv run` prefix.

### With pip

Install from PyPI:

```bash
pip install m3u8-cli
```

Upgrade:

```bash
pip install --upgrade m3u8-cli
```

You can also clone the repository and install from source:

```bash
git clone <repository-url>
cd m3u8-cli
pip install .
```

If your system Python enables the externally-managed protection (the default on Arch and Debian, for example), create and activate a virtual environment before running the commands above.

## Usage

```bash
m3u8-cli download "<m3u8_url>"
```

You can also run it as a module:

```bash
python -m m3u8_cli download "<m3u8_url>"
```

Conversion supports the module form as well:

```bash
python -m m3u8_cli convert movie.mp4 -o out
```

Show the version:

```bash
m3u8-cli -v       # or --version
```

### Common examples

```bash
# Default output: output.mp4 in the current directory
m3u8-cli download "https://example.com/playlist.m3u8"

# Custom output name and concurrency
m3u8-cli download "https://example.com/playlist.m3u8" -o movie.mp4 -j 16

# When the server requires Referer / Cookie headers
m3u8-cli download "https://example.com/playlist.m3u8" \
  --referer "https://example.com/" \
  --cookie "session=xxxx"

# Overwrite an existing final file
m3u8-cli download "https://example.com/playlist.m3u8" -o movie.mp4 --overwrite
```

### Convert video

```bash
# Convert to HLS by default: creates movie/playlist.m3u8 and movie/ts/*.ts
m3u8-cli convert movie.mp4

# Choose the output directory, format and codec
m3u8-cli convert movie.mp4 -f mkv -c hevc -o out

# Convert to HLS with 10-second segments
m3u8-cli convert movie.mp4 -f hls --segment-time 10

# Overwrite existing output
m3u8-cli convert movie.mp4 -f mp4 --overwrite

# Encrypt HLS segments (generates key.bin)
m3u8-cli convert movie.mp4 --encrypt
```

| Option | Default | Description |
| --- | --- | --- |
| `input` | required | Path to a local video file |
| `-f, --format` | `hls` | `hls`, `mp4`, `mkv`, `webm` |
| `-c, --codec` | `h264` | `h264`, `hevc`, `copy` |
| `-o, --output-dir` | input file stem | Output directory |
| `--segment-time` | `6` | HLS only |
| `--overwrite` | off | Overwrite existing output |
| `--encrypt` | off | HLS only; generates `key.bin` and encrypts segments |

HLS output is always `playlist.m3u8` plus segments starting at `ts/000000.ts`. WebM re-encoding uses VP9 + Opus; with `-c copy` the source codecs are copied directly and FFmpeg decides whether they are compatible.

- `mp4`/`mkv`/`webm` output is `<output-dir>/<input stem>.<extension>`, e.g. `m3u8-cli convert movie.mp4 -f mkv -o out` produces `out/movie.mkv`; single files are first written as `.partial.*` and atomically replaced on success.
- With `-c copy`, HLS segment boundaries are determined by source keyframes; `--segment-time` is only an approximate target.
- `--encrypt` applies to HLS only: the output directory also gets a 16-byte `key.bin` referenced by a relative URI from the playlist, and it must be distributed alongside `playlist.m3u8`.
- FFmpeg/ffprobe reject the `key.bin` extension by default; local playback or probing needs `-allowed_extensions ALL`. Browser players are not affected.
- Every `--encrypt` conversion generates a new key. Without `--overwrite`, an existing `key.bin` or `.key_info.txt` blocks the conversion; with it, the old key is replaced and `key.bin` must be redistributed.

### Download options

| Option | Default | Description |
| --- | --- | --- |
| `url` | required | m3u8 playlist URL |
| `-f, --format` | `mp4` | `mp4` (merge and remux) or `hls` (keep segments, no merge) |
| `-o, --output` | `output.mp4` / `output` | Final file name for mp4; task directory for hls (defaults differ by format) |
| `-j, --jobs` | `8` | Number of concurrent download threads |
| `--retries` | `3` | Retries after a single segment download fails |
| `--timeout` | `30` | Network request timeout in seconds |
| `--referer` | none | Custom `Referer` header |
| `--cookie` | none | Custom `Cookie` header |
| `--user-agent` | `Mozilla/5.0` | Custom `User-Agent` header |
| `--overwrite` | off | Overwrite an existing final file |
| `-k, --insecure` | off | Skip TLS certificate verification; only for self-signed or intranet services |

### Keep HLS (no merge)

```bash
# Writes movie/playlist.m3u8, movie/000000.ts and other segments
m3u8-cli download "<m3u8_url>" -f hls -o movie
```

- `-f hls` does not invoke FFmpeg; segments keep the exact bytes returned by the server.
- Plain TS streams produce `playlist.m3u8` and `.ts` segments numbered with 6 digits; fMP4 streams produce `init.mp4` and `.m4s` segments.
- If the source is AES-128 encrypted, the ciphertext is kept and the key is saved as `key.bin` (or `key_000.bin`, `key_001.bin`, … when there are several distinct keys); `EXT-X-KEY` URIs in the playlist are rewritten to the local files.
- Segments, `playlist.m3u8` and key files live in the same directory; `--overwrite` only cleans up entries generated by this tool.

`--insecure` disables certificate and hostname verification for playlist, segment and key requests. Only use it against self-signed services on networks you trust; it does not affect the other information recorded in metadata.

### Start the download service

```bash
m3u8-cli serve --host 0.0.0.0 --port 8787 --output-dir ./downloads
```

The startup log prints the listening address, output directory and API token; the token is generated automatically when `--token` is omitted.

Requests from loopback addresses (`127.0.0.0/8`, `::1`, including IPv4-mapped IPv6) **skip the token check**, which makes it easy for local extensions and scripts to call the API; other sources still need `Authorization: Bearer <token>`. Note:

- This means other users or processes on the same machine can also call the API without a token; if a reverse proxy listening on localhost forwards requests, all traffic will appear to come from a loopback address.
- API CORS is `*`, so skipping the token also lets any web page in the browser call the local `serve` instance (read tasks, submit downloads). If that matters, keep a token (web pages cannot obtain it) or tighten CORS yourself.

```bash
# Create a task
curl -X POST http://127.0.0.1:8787/api/tasks \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/playlist.m3u8",
    "format": "hls",
    "output": "movie",
    "insecure": false,
    "headers": {"Referer": "https://example.com/"}
  }'

# List tasks / task details
curl http://127.0.0.1:8787/api/tasks -H "Authorization: Bearer <token>"
curl http://127.0.0.1:8787/api/tasks/<id> -H "Authorization: Bearer <token>"

# Server version
curl http://127.0.0.1:8787/api/version -H "Authorization: Bearer <token>"

# Cancel a task
curl -X DELETE http://127.0.0.1:8787/api/tasks/<id> \
  -H "Authorization: Bearer <token>"
```

- At most `--max-tasks` tasks run concurrently; the rest queue in FIFO order. Inside each task, `jobs` controls segment concurrency.
- Headers are passed via the `headers` object and used for playlist, segment and key requests; they are never persisted or returned.
- The state file defaults to `<output-dir>/.m3u8-serve-state.json`; unfinished tasks are marked `interrupted` after a restart.
- The task list supports `?state=running` and `?limit=50&offset=0`; `total` is the count after filtering.
- With `--port 0` the system assigns a port and prints the actual one in the startup log.

## Chrome extension

`chrome-extension/` provides a Manifest V3 extension that discovers m3u8 links on pages:

- monitors network requests and scans the DOM to list m3u8 URLs for the current tab;
- extracts the page title (`og:title`, Twitter Card, JSON-LD, `document.title`) as the default download name, or uses a short URL hash instead via settings;
- copies links or generates a `m3u8-cli download` command with `Referer`/`Cookie`/`User-Agent`;
- submits downloads to a local `m3u8-cli serve` instance with one click, showing progress and allowing cancellation in the popup;
- shows the extension version and the backend `serve` version in settings (via `GET /api/version`).

To install: open `chrome://extensions`, enable "Developer mode", click "Load unpacked", and select the repository's `chrome-extension/` directory.

We recommend starting the service with a fixed token listening on localhost, then entering the same address and token in the extension settings:

```bash
m3u8-cli serve --host 127.0.0.1 --port 8787 --token mytoken --output-dir ./downloads
```

See `chrome-extension/README.md` in the repository for permissions, cookie handling and troubleshooting.

To publish or distribute the extension, build a zip containing only the runtime files:

```bash
uv run python scripts/build_extension.py
# writes dist/m3u8-cli-chrome-extension-<manifest version>.zip
```

## Output layout

### MP4 (default, merged and remuxed)

With `-o movie.mp4`, for example, the output directory holds only the final artifact and its metadata:

```
movie.mp4
movie.mp4.meta.json     # ffprobe format/streams plus source info
movie.partial.mp4       # FFmpeg temporary output (exists only during remuxing)
```

Segment cache does not live in the output directory but in the system temporary directory:

```
<TMP>/m3u8-cli-<uid>/<hash of final path>/
├── state.json          # playlist fingerprint, used for cache invalidation
├── segment_000000.bin  # decrypted media segments
├── map_0000.bin        # fMP4 initialization segment (fMP4 streams only)
└── combined.ts|mp4     # locally merged intermediate file
```

- The whole cache directory is removed after a successful merge; it is kept on failure or cancellation so the download can resume.
- The same `-o` maps to the same cache directory; when the playlist changes, the `state.json` fingerprint triggers cleanup of old segments.
- Metadata is generated by running ffprobe on the artifact: top-level `format`/`streams` are the output of `ffprobe -show_format -show_streams -of json` (with `filename` path removed), while `source` records the URL, format, segment count, fingerprint, timestamp and whether TLS verification was enabled. It contains no `output` field and no `Referer`/`Cookie`/`User-Agent`.
- If ffprobe is not installed, a warning is printed to stderr, metadata keeps only the `source` part, and the download still completes.
- Any exception while generating metadata (including empty ffprobe output or invalid JSON) only prints a warning and never marks a completed download as failed.

### HLS (keep segments)

With `-o movie`, for example:

```
movie/
├── playlist.m3u8       # local playlist
├── 000000.ts           # segments (.m4s for fMP4, plus init.mp4)
├── key.bin             # when an AES-128 key exists
└── meta.json           # ffprobe media info plus source info
```

- Segments are first downloaded to the system temporary directory and cleaned up after a successful export; the output directory keeps only the final artifacts and metadata.
- On export failure or cancellation the cache is kept, so the same `-o` can resume.

## Cache and resume

- Segment caches for both formats live in `<system temp>/m3u8-cli-<uid>/<first 16 chars of SHA-256 of final path>/`; they are deleted after a successful download and kept on failure or cancellation.
- With the same `-o` and a surviving cache, existing non-empty segments are reused directly.
- `state.json` in the cache stores the SHA-256 fingerprint of the raw playlist; old segments are cleared when the playlist changes.
- If the final file already exists and `--overwrite` is not given, the program exits immediately.
- After the OS cleans the temporary directory, failed tasks must be downloaded again; to clean up thoroughly, delete the corresponding temporary cache directory.

## Known limitations

- `EXT-X-BYTERANGE` is not supported, nor are initialization segments with BYTERANGE.
- Only a single initialization segment (`EXT-X-MAP`) is supported.
- Only AES-128 (`KEYFORMAT=identity`) is supported; SAMPLE-AES and any DRM are not.
- The playlist is fetched only once; sliding windows of live or event playlists are not refreshed.
- Selective download of multiple audio or subtitle tracks is not supported.
- `convert` accepts local files only; `-c copy` does not pre-check source codec compatibility.
- HLS conversion with `--overwrite` first deletes the old `playlist.m3u8` and 6-digit segments under `ts/`, keeping other files (`key.bin` is replaced with the new key under `--encrypt`); a failed conversion loses the old output and may leave partial new segments, and a newly written `key.bin` is deleted as well.
- HLS AES-128 encryption is not DRM; any client that can access `key.bin` can decrypt it.
- FFmpeg's HLS muxer writes a fixed `IV=0`, so all segments share the same key and IV.
- Custom keys, key URIs or IVs are not supported.
- `serve` does not block internal addresses (no SSRF protection); loopback requests skip the token, and non-local sources still rely on the token and a LAN deployment.
- The loopback token exemption applies to all users on the same machine; be careful on multi-user hosts or behind a local reverse proxy, and listen on `127.0.0.1` only when necessary.
- Windows defaults to `MAX_PATH=260`: `serve` shortens filenames that are too long based on the output directory length (keeping the extension) and returns the actual name in the task response; the CLI does not rewrite `-o`.
- `serve` does not expose an endpoint for downloading artifacts; files stay on the server.
- After a `serve` restart, tasks are marked `interrupted` and are not resumed automatically.
- Headers are not persisted, so tasks cannot be replayed after a restart.

## Development

```bash
# Byte-compile check
uv run python -m compileall -q src

# Type check (using .venv)
pyright

# Unit tests
uv run python -m unittest discover -s tests -v

# Chrome extension: syntax check and unit tests
for f in chrome-extension/{lib,background,content,popup}.js; do node --check "$f"; done
node --test chrome-extension/lib.test.js

# Build the Python package (the sdist also includes chrome-extension/ sources and the build script)
uv build

# Build the Chrome extension zip (written to dist/)
uv run python scripts/build_extension.py

# Build everything at once
uv build && uv run python scripts/build_extension.py
```

The project uses GitHub Actions for testing and publishing: Python unit tests run on the standard library unittest, and the Chrome extension's pure-function tests run on Node's built-in test runner.

### CI and releases

CI is configured only on the GitHub remote (`github.com/zcq100/m3u8-cli`). Pushing to Gitea (`origin`) does **not** trigger any pipeline; run this as well:

```bash
git push github master
```

Pipeline behavior:

| Workflow | Trigger | Behavior |
|----------|---------|----------|
| `ci.yml` | any branch push, all pull requests | Python and extension syntax checks and unit tests |
| `publish-pypi.yml` | changes to `src/**`, `pyproject.toml` or `uv.lock` on `master` | run tests → check version consistency → `uv build` → upload to PyPI |
| `release-extension.yml` | changes to `chrome-extension/**` on `master` | run extension tests → build zip → create `chrome-extension-v<version>` tag and Release |

Release conventions:

- When changing `src/`, bump the version in both `pyproject.toml` and `src/m3u8_cli/__init__.py`, and run `uv lock`; the pipeline fails if the two versions differ or the version already exists on PyPI.
- When changing `chrome-extension/`, bump `version` in `chrome-extension/manifest.json`, otherwise the corresponding tag already exists and the Release fails.

The repository secret `PYPI_API_TOKEN` must be configured once (a PyPI API token scoped to the `m3u8-cli` project).

Code layout:

```
src/m3u8_cli/
├── cli.py           # command-line entry point and orchestration
├── convert.py       # video format conversion (FFmpeg)
├── playlist.py      # m3u8 parsing
├── download.py      # reusable download core (mp4/hls, progress, cancellation)
├── hls_export.py    # local HLS export
├── serve.py         # REST API and task management
├── downloader.py    # single-segment download, retries and AES-128 decryption
├── merge.py         # segment merging and FFmpeg remuxing
├── paths.py         # task directory derivation
├── errors.py        # shared exceptions
└── __main__.py      # python -m m3u8_cli
```

`chrome-extension/` is the companion Chrome extension (MV3, plain JS, no build step needed); see its README for details. `scripts/build_extension.py` produces the distributable extension zip.

`reference/hls_downloader.py` is an early lightweight implementation that hands the m3u8 directly to FFmpeg; it is kept for reference only and is no longer maintained.

## License

This project is licensed under the [MIT](https://opensource.org/licenses/MIT) license. See the `LICENSE` file in the repository root.

## Disclaimer

Only download content you have the right to access and save, and comply with the terms of service and copyright requirements of the sites involved.

# Vintage Movie Short Renderer

Creates a vertical 1080 × 1920 short with trailer footage above a HeyGen presenter.

## On-demand operation

The user can name a film or series in chat. The assistant finds and verifies a matching public trailer page, including the correct year and season, and supplies that source to Make. Users do not need to upload a trailer MP4 for supported sources. This is an assistant-driven, one-video-per-command flow; autonomous news selection or scheduled mass generation is not enabled.

1. Find a matching Apple TV `/clip/` trailer page or a direct public MP4.
2. Make calls `GET /prepare-trailer?url=...` **before** sending the job to n8n/HeyGen. Allow up to 300 seconds for the first download.
3. The renderer downloads the public preview, checks it with FFmpeg, caches the MP4, and returns `trailer_url`, duration, size and original source URL.
4. Make forwards the returned `trailer_url` to n8n. It must not forward the original Apple HTML page.
5. Pass explicit `titles_json` and, when desired, `clips_json`; do not leave production titles as template defaults.
6. Poll rendering until completed; only then download the final MP4 and save it durably.

## Supported sources and limits

- Public Apple TV trailer `/clip/` pages with unencrypted VideoPreview HLS; video and audio are downloaded and remuxed to MP4.
- Direct public MP4 or WebM files that pass a container and duration check.
- Apple media hosts and redirects are allowlisted. Protected, live, oversized and foreign-host playlists are rejected.
- YouTube and RUTUBE are not reliable download fallbacks in the currently tested environment. Do not substitute presenter footage or an unrelated film when a source is unavailable.
- Apple does not guarantee trailer coverage for every title or season. Source discovery is performed by the assistant; there is no standalone title-search API in this service.
- Cached trailers and completed jobs use `JOBS_DIR` (default: temporary storage). They can be lost after a service restart. Save final videos to durable storage; a cache URL is not a permanent archive.
- This change does not implement Google Drive uploads or repair all n8n polling behavior.
- Version 1.5.1 limits FFmpeg threads and processes trailer cuts sequentially, avoiding full-trailer frame buffering on the 512 MB server. An interrupted job is marked failed at startup, so a restart cannot leave it indefinitely reporting rendering.

## Endpoints

- `GET /prepare-trailer?url=...`: download and validate the trailer before paid generation.
- `GET /trailer-assets/{asset_id}`: serve a prepared trailer to the renderer.
- `POST /render`: accepts distinct presenter and trailer URLs. HeyGen URLs are rejected in the trailer field.
- `GET /status/{job_id}`: queued, downloading, rendering, completed, or an explicit failure.
- `GET /download/{job_id}`: waits for **completed** status before returning an MP4.
- `GET /public-download/{job_id}`: returns only finished jobs.
- `GET /health`: service version and render-lock status.

Existing `RENDER_TOKEN` authentication applies to preparation and rendering. Configure `PUBLIC_BASE_URL` if hosting this service at another address.

## Validation

Run `python -m unittest discover -s tests` after installing requirements.
The integration check downloads an Apple TV movie trailer (141.76 seconds), decodes it, and renders a 1080 × 1920 layout with distinct top and bottom sources, without a new HeyGen generation.

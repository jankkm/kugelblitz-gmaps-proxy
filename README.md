# MapProxy for Kugelblitz

A local [MapProxy](https://mapproxy.org/) that fetches Google, Esri, and Mapbox satellite, hybrid, and roadmap tiles and serves them to [Kugelblitz](https://apps.metaebene.me/en/kugelblitz) on a Mac.

Needs a local docker runtime.

## Start

```sh
docker compose up -d --build
```

The proxy listens on [http://127.0.0.1:8080](http://127.0.0.1:8080). The demo page is [http://127.0.0.1:8080/demo/](http://127.0.0.1:8080/demo/).

Stop it with `docker compose down`.

## Cache

Tiles and MapProxy's own files are on host directories, not in the container disk. Google tiles are in `tilecache/`. Esri and Mapbox tiles are in `cache/`. MapProxy's generated config and logs are in `mapproxy-data/`. Each tile directory is kept under 8 GiB. Past that, the least recently used tiles are deleted until the directory is back to about 90%. Expired tiles go first. Upscaled Google tiles, which can be rebuilt from the cached parent, go before native tiles of a similar age.

If the disk drops below 1 GiB free, those same jobs delete cached tiles until there is room again. A Google tile that still cannot be saved is served anyway.

Set `TILE_CACHE_MAX_BYTES` and `MAPPROXY_CACHE_MAX_BYTES` in `.env` to change the caps (`20GiB`, `500MB`, or `0` for no cap). `CACHE_MIN_FREE` is the free-space floor (`0` turns that off).

To put that data on another disk, set `DATA_DIR` in `.env` to the new location and move `tilecache/`, `cache/`, and `mapproxy-data/` there. Start the containers again after changing them.

## Use in Kugelblitz

Add `http://127.0.0.1:8080/ogcapi` in the Library. That is the OGC API landing page. Kugelblitz lists each layer as a map collection:

- Google Satellite, Hybrid, and Roadmap
- Esri Satellite, Hybrid, and Roadmap
- Mapbox Satellite, Hybrid, and Roadmap

`my_google_layers.geojson` still adds the six satellite and hybrid layers as WMTS, if you already have that file in the Library.

Leave the container running while you use the layers.

Mapbox needs an access token. Copy `.env.example` to `.env`, set `MAPBOX_ACCESS_TOKEN` to a public token (`pk.…`), and start the container again.

Google satellite has no native tile for open water and for a lot of higher zoom levels, and answers those requests with 404. MapProxy was returning that as HTTP 500, so a layer works over covered land and then fails once the view includes a gap. The `tileproxy` service fills each gap from the next lower zoom and keeps the response a tile.

Google, Esri, and Mapbox tile servers are not a public map API. This setup is for local use in Kugelblitz and is subject to their terms.

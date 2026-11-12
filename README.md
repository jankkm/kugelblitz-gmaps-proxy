# MapProxy for Kugelblitz

A local [MapProxy](https://mapproxy.org/) that fetches Google, Esri, and Mapbox satellite, hybrid, and roadmap tiles and serves them to [Kugelblitz](https://apps.metaebene.me/en/kugelblitz) on a Mac.

Needs a local docker runtime.

## Start

```sh
docker compose up -d --build
```

The proxy listens on [http://127.0.0.1:8080](http://127.0.0.1:8080). The demo page is [http://127.0.0.1:8080/demo/](http://127.0.0.1:8080/demo/).

Stop it with `docker compose down`.

## Use in Kugelblitz

Add `my_google_layers.geojson` as file in Library. That adds:

- Google Satellite
- Google Hybrid
- Esri Satellite
- Esri Hybrid
- Mapbox Satellite
- Mapbox Hybrid

Roadmap layers for all three are on the demo page.

Leave the container running while you use the layers.

Mapbox needs an access token. Copy `.env.example` to `.env`, set `MAPBOX_ACCESS_TOKEN` to a public token (`pk.…`), and start the container again.

Google satellite has no native tile for open water and for a lot of higher zoom levels, and answers those requests with 404. MapProxy was returning that as HTTP 500, so a layer works over covered land and then fails once the view includes a gap. The `tileproxy` service fills each gap from the next lower zoom and keeps the response a tile.

Google, Esri, and Mapbox tile servers are not a public map API. This setup is for local use in Kugelblitz and is subject to their terms.

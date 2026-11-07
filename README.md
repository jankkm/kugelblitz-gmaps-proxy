# MapProxy for Kugelblitz

A local [MapProxy](https://mapproxy.org/) that fetches Google satellite, hybrid, and roadmap tiles and serves them to [Kugelblitz](https://apps.metaebene.me/en/kugelblitz) on a Mac.

Needs a local docker runtime.

## Start

```sh
docker compose up -d
```

The proxy listens on [http://127.0.0.1:8080](http://127.0.0.1:8080). The demo page is [http://127.0.0.1:8080/demo/](http://127.0.0.1:8080/demo/).

Stop it with `docker compose down`.

## Use in Kugelblitz

Add `my_google_layers.geojson` as file in Library. That adds:

- Google Satellite
- Google Hybrid

Leave the container running while you use the layers.

Google’s tile servers are not a public map API. This setup is for local use in Kugelblitz and is subject to Google’s terms.

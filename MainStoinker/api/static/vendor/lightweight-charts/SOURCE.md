# TradingView Lightweight Charts (vendored)

- Package: `lightweight-charts` on npm, version 5.3.0
- Source URL: https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-5.3.0.tgz
  (tarball sha1 matched the registry: `f4a90dca971cf0727f9bbaceb1e5090cb3b5b4c9`)
- File: `lightweight-charts.standalone.production.js`, taken from `package/dist/` of the tarball, committed unchanged
- Size: 198,600 bytes
- sha256: `338a625ece52c5b85fb026e222dad5d0f365f745060afbfae96bda91636142e9`
- Licence: Apache License 2.0, see `LICENSE` in this folder. The npm package has no NOTICE file.
- Defines the global `LightweightCharts` (version 5 API: `chart.addSeries(CandlestickSeries, ...)`,
  markers through `createSeriesMarkers`).

## Attribution requirement

The library's terms (its README) require an attribution notice: a link to
https://www.tradingview.com/ on a page of the product, and the TradingView logo on the charts
(the library shows it by default; do not turn `attributionLogo` off). The dashboard footer carries
the text "Charts by TradingView Lightweight Charts" linking to https://www.tradingview.com/.

## Updating

Replace the file only with another pinned release, update the version, URL, size and sha256 here,
and update the sha256 in `tests/test_api_static.py`. `.gitattributes` marks this folder `-text`
so git never rewrites its line endings.

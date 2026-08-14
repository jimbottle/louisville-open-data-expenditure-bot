# Vendored third-party assets

## chart.umd.min.js — Chart.js v4.4.7 (MIT)

Self-hosted so the app depends on no third-party CDN at runtime (see the CSP in
`static/index.html` and `louisville-open-data-e8d`). The file was downloaded
from jsdelivr's dynamic endpoint, which wraps the official npm build with its
own banner.

**Provenance verified against the official npm registry** (not just the CDN):
the code is byte-identical to `dist/chart.umd.js` inside `npm pack chart.js@4.4.7`,
after stripping the leading banner comments (the only difference).

Checkable baselines:

- Vendored file sha256: `206b6e8bb00fc7bba2c7ee80ca41db3e9e05ba7be0aa35abeba9cfd5357f5d0e`
- Banner-stripped code sha256 (matches the npm tarball): `d7e5f4c61ed0d870bfced804e74f8fbd5b80aa30e8a83ec5f44cb033cf681a39`

To re-verify (or when re-vendoring a new version):

```bash
npm pack chart.js@4.4.7 --pack-destination /tmp/cj
tar -xzf /tmp/cj/chart.js-4.4.7.tgz -C /tmp/cj
python3 - /tmp/cj/package/dist/chart.umd.js static/vendor/chart.umd.min.js <<'PY'
import re, sys
def code(p):
    s = open(p, encoding='utf-8').read()
    while (m := re.match(r'\s*/\*.*?\*/\s*', s, flags=re.DOTALL)):
        s = s[m.end():]
    return s.strip()
print("identical:", code(sys.argv[1]) == code(sys.argv[2]))
PY
```

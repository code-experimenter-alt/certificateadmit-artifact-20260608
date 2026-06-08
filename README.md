# CertificateAdmit Reproducibility Artifact

This repository contains the anonymized artifact package for the ICDE submission on visible-state admission for LDP data products.

## Quick start

```bash
make smoke
```

The full non-AWS local reproduction path is:

```bash
make reproduce
```

Optional checks:

```bash
make postgres
make etcd
```

The artifact includes executable scripts under `artifact/`, public datasets under `data/`, generated CSV/JSON outputs, figure inputs under `figures/`, and the root `Makefile` used by the paper. It intentionally excludes paper source files and author-identifying metadata.

See `artifact/README.md` for script-by-script details.

# CertificateAdmit Reproducibility Artifact

This repository contains the anonymized local artifact package for the
budget/class-integrity certificate study. It includes executable scripts under
`artifact/`, public inputs under `data/`, generated CSV/JSON outputs, figure
inputs under `figures/`, and the root `Makefile` used for local reproduction.

Local smoke test:

```bash
make smoke
```

Full non-AWS reproduction path:

```bash
make reproduce
```

Optional PostgreSQL serializable admission check:

```bash
make postgres
```

Optional etcd linearizable admission check:

```bash
make etcd
```

This target expects `etcd` and `etcdctl` on `PATH` or `ETCD_BIN_DIR` pointing
to their directory; it runs the 1,000-attempt three-node check reported in the
paper.

Generated LaTeX build files and transient verification outputs are excluded.

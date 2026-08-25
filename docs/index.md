# Ocura OSS documentation

Ocura OSS records trusted local command runs, the reasons branches are created, and the evidence produced by later runs. State remains under `.ocura-oss/` in the project directory.

The package provides two interfaces:

- the `ocura-oss` command-line interface
- a typed Python API for programmatic workflows and state inspection

The Python API is provisional during the 0.x series. Documented names are supported for programmatic use but may change before version 1.0. Names not included in `ocura_oss.__all__` are implementation details.

## Hosted reference

- [Overview](https://ocuna-ai.com/docs): installation, concepts, local state, verification, and operating boundaries
- [CLI reference](https://ocuna-ai.com/docs/cli): commands, options, output formats, and exit status
- [Python API reference](https://ocuna-ai.com/docs/api): functions, parameters, return types, `Store`, records, and exceptions

## Repository references

- [README](../README.md): installation, command workflow, and operating boundaries
- [Python API](python-api.md): supported functions, `Store`, result types, and exceptions
- [State and verification](state-and-verification.md): record layout, checks, and runtime boundaries
- [Contributing](../CONTRIBUTING.md): development setup and required checks
- [Security policy](../SECURITY.md): supported reporting process

The installed command also provides command-specific syntax and exit semantics:

```console
ocura-oss --help
ocura-oss run --help
```

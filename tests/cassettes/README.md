# tests/cassettes/

VCR cassettes for Medilink API v5 integration tests.

## What these are

Synthetic YAML files that reproduce the real Medilink API response shape without
hitting the real API. They are hand-crafted — never recorded against production.

Tests that use these cassettes run in `record_mode: none` by default, so CI
never makes network calls to Medilink.

## Files

| Cassette | Endpoint | Description |
|---|---|---|
| `buscar_proxima_fecha_mg.yaml` | GET `/especialidades/73/proxima` | Próxima fecha disponible Medicina General (Dr. Abarca, ID 73) |
| `buscar_paciente_rut.yaml` | GET `/pacientes?rut=11111111-1` | Paciente de prueba RUT 11.111.111-1 |
| `crear_cita_mg.yaml` | POST `/citas` | Creación de cita Medicina General, 16/05/2026 09:00, 15 min |

## How to use in tests

```python
import pytest

@pytest.mark.vcr("buscar_proxima_fecha_mg.yaml")
def test_proxima_fecha_mg(vcr):
    # Any HTTP call to Medilink is served from the cassette
    ...
```

Or with the module-level config from conftest_medilink.py:

```python
# conftest.py in the test file's directory (or conftest_medilink.py)
@pytest.fixture(scope="module")
def vcr_config():
    return {"cassette_library_dir": "tests/cassettes", "record_mode": "none"}
```

## Cassette format notes

- Dates are in DD/MM/YYYY (Medilink API format), not ISO 8601.
- `Authorization` header is always scrubbed to `Bearer MEDILINK_TOKEN_REDACTED`.
- `id_sucursal=1` is the CMC branch ID.
- Slot duration (`duracion`) is in minutes and must match `hora_fin - hora_inicio`.
- Cita creation requires `id_estado: 2` (Confirmado). Cancellation uses `id_estado: 1` (Anulado).

## Adding cassettes

1. Hand-craft a YAML following the vcrpy 8.x schema (see existing files).
2. Match the real API response shape from `docs/medilink_gotchas.md`.
3. Never use real patient RUTs or phone numbers — use the test set:
   - RUT: `11.111.111-1` (patient ID 12345, "Juan Prueba Test")
   - Phone: `56900000001` through `56900000099` (reserved for tests)

## Re-recording (only when Medilink API shape changes)

```bash
# Record a single cassette against staging (never production):
pytest tests/test_medilink_integration.py::test_proxima_fecha \
  --vcr-record=new_episodes \
  --medilink-token=$MEDILINK_TOKEN_STAGING
```

Never run `--vcr-record=all` in CI.

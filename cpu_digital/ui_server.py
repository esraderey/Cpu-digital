"""Servidor web local para inspeccionar y controlar Tramoya VM32."""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from tramoya import MachineError

from .vm32 import VMConfig, VMRuntimeError, TramoyaVM32
from .vm32_assembler import VM32Assembler, VMAssemblyError, VMAssemblyResult
from .vm32_isa import VM32_MNEMONICS


UI_DIR = Path(__file__).resolve().parent / "ui"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024

DEMOS: Mapping[str, tuple[str, str]] = {
    "hola": ("Hola, VM32", "hola.tasm"),
    "factorial": ("Factorial de 10", "factorial_10.tasm"),
    "fibonacci": ("Fibonacci · 20 términos", "fibonacci_20.tasm"),
    "array": ("Suma de arreglo", "array_sum.tasm"),
    "entrada": ("Entrada cooperativa", "entrada.tasm"),
    "interrupcion": ("Interrupción", "interrupcion.tasm"),
    "proteccion": ("Protección de código", "proteccion.tasm"),
}


def _integer(value: Any, name: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} debe ser un entero")
    try:
        parsed = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} debe ser un entero") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{name} debe ser mayor o igual que {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{name} debe ser menor o igual que {maximum}")
    return parsed


def _integer_list(value: Any, name: str) -> list[int]:
    if value in (None, ""):
        return []
    values = value if isinstance(value, list) else str(value).replace(",", " ").split()
    if not isinstance(values, list):
        raise ValueError(f"{name} debe ser una lista")
    return [_integer(item, name) for item in values]


class ConsoleSession:
    """Una sesión VM32 aislada y serializada para la UI local."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.assembler = VM32Assembler()
        self.vm = TramoyaVM32()
        self.source = ""
        self.demo = "hola"
        self.assembly: VMAssemblyResult | None = None
        self.revision = 0
        self._load_demo_unlocked("hola")

    @staticmethod
    def _demo_source(name: str) -> str:
        if name not in DEMOS:
            raise ValueError(f"Ejemplo desconocido: {name}")
        filename = DEMOS[name][1]
        return resources.files("vm_programs").joinpath(filename).read_text(encoding="utf-8")

    def _load_demo_unlocked(self, name: str) -> None:
        source = self._demo_source(name)
        assembly = self.assembler.assemble(source, source_name=f"demo:{name}")
        self.vm = TramoyaVM32()
        self.vm.load_program(assembly.program)
        self.source = source
        self.demo = name
        self.assembly = assembly
        self.revision += 1

    @staticmethod
    def _config_from(payload: Mapping[str, Any]) -> VMConfig:
        defaults = VMConfig()
        config = payload.get("config", {})
        if not isinstance(config, Mapping):
            raise ValueError("config debe ser un objeto")
        return VMConfig(
            memory_words=_integer(config.get("memory_words", defaults.memory_words), "Memoria", minimum=256, maximum=16_777_216),
            gas_limit=_integer(config.get("gas_limit", defaults.gas_limit), "Gas", minimum=1),
            stack_limit=_integer(config.get("stack_limit", defaults.stack_limit), "Pila", minimum=1),
            interrupt_depth=defaults.interrupt_depth,
            trace_size=_integer(config.get("trace_size", defaults.trace_size), "Traza", minimum=0, maximum=100_000),
            output_limit=_integer(config.get("output_limit", defaults.output_limit), "Salida", minimum=1),
            protect_code=bool(config.get("protect_code", defaults.protect_code)),
            capabilities=defaults.capabilities,
        )

    def bootstrap(self, memory_start: int = 0, memory_count: int = 64) -> dict[str, Any]:
        with self.lock:
            demos = [
                {"id": key, "label": label, "source": self._demo_source(key)}
                for key, (label, _) in DEMOS.items()
            ]
            isa = [
                {
                    "mnemonic": spec.mnemonic,
                    "form": spec.form,
                    "cost": spec.cost,
                    "description": spec.description,
                }
                for spec in VM32_MNEMONICS.values()
            ]
            return {
                "ok": True,
                "version": "2.0.0",
                "demos": demos,
                "isa": isa,
                "source": self.source,
                "demo": self.demo,
                "listing": self._listing_unlocked(),
                "lifecycle": self.vm.lifecycle_mermaid(),
                "state": self._state_unlocked(memory_start, memory_count),
            }

    def load(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self.lock:
            source = payload.get("source")
            if not isinstance(source, str) or not source.strip():
                raise ValueError("El código fuente está vacío")
            if len(source.encode("utf-8")) > MAX_JSON_BYTES:
                raise ValueError("El código fuente excede el límite local")
            config = self._config_from(payload)
            assembly = self.assembler.assemble(
                source,
                source_name="consola.tasm",
                max_words=config.memory_words,
            )
            vm = TramoyaVM32(config)
            vm.load_program(assembly.program, inputs=_integer_list(payload.get("inputs"), "Entrada"))
            self.vm = vm
            self.source = source
            self.demo = str(payload.get("demo") or "personalizado")
            self.assembly = assembly
            self.revision += 1
            return {
                "ok": True,
                "message": "Programa ensamblado y cargado",
                "listing": self._listing_unlocked(),
                "lifecycle": self.vm.lifecycle_mermaid(),
                "state": self._state_unlocked(),
            }

    def action(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self.lock:
            action = str(payload.get("action", "")).lower()
            message = ""
            if action == "step":
                if self.vm.state == "PAUSED":
                    self.vm.resume()
                self.vm.step()
                message = "Una instrucción ejecutada"
            elif action == "run":
                limit = _integer(payload.get("max_instructions", 2_000), "Límite", minimum=1, maximum=25_000)
                breakpoints = self._resolve_many(payload.get("breakpoints", []))
                self.vm.run(max_instructions=limit, breakpoints=breakpoints)
                message = f"Bloque de hasta {limit} instrucciones ejecutado"
            elif action == "pause":
                self.vm.pause("Pausa solicitada desde la consola")
                message = "Ejecución pausada"
            elif action == "resume":
                self.vm.resume()
                message = "Ejecución reanudada"
            elif action == "input":
                values = _integer_list(payload.get("values"), "Entrada")
                if not values:
                    raise ValueError("Escribe al menos un valor de entrada")
                self.vm.provide_input(*values)
                message = f"{len(values)} valor(es) entregados a la VM"
            elif action == "interrupt":
                vector = _integer(payload.get("vector"), "Vector", minimum=0, maximum=255)
                self.vm.request_interrupt(vector)
                message = f"Interrupción {vector} encolada"
            elif action == "set_vector":
                vector = _integer(payload.get("vector"), "Vector", minimum=0, maximum=255)
                target = self._resolve(payload.get("target"))
                self.vm.set_interrupt_vector(vector, target)
                message = f"Vector {vector} → {target:#x}"
            elif action == "reset":
                inputs = _integer_list(payload.get("inputs"), "Entrada")
                self.vm.reload(inputs=inputs)
                message = "VM restablecida"
            elif action == "write_memory":
                address = _integer(payload.get("address"), "Dirección", minimum=0)
                value = _integer(payload.get("value"), "Valor")
                self.vm.write_memory(address, value)
                message = f"Memoria[{address:#x}] actualizada"
            else:
                raise ValueError(f"Acción desconocida: {action or '(vacía)'}")
            self.revision += 1
            memory_start = _integer(payload.get("memory_start", 0), "Inicio de memoria", minimum=0)
            memory_count = _integer(payload.get("memory_count", 64), "Cantidad de memoria", minimum=1, maximum=256)
            return {
                "ok": True,
                "message": message,
                "state": self._state_unlocked(memory_start, memory_count),
            }

    def state(self, memory_start: int = 0, memory_count: int = 64) -> dict[str, Any]:
        with self.lock:
            return {"ok": True, "state": self._state_unlocked(memory_start, memory_count)}

    def bytecode(self) -> bytes:
        with self.lock:
            if self.vm.program is None:
                raise VMRuntimeError("No hay programa cargado")
            return self.vm.program.to_bytes()

    def snapshot_bytes(self) -> bytes:
        with self.lock:
            return self.vm.snapshot_bytes()

    def restore(self, raw: bytes) -> dict[str, Any]:
        with self.lock:
            try:
                self.vm.restore_bytes(raw)
            except ValueError as exc:
                if "otro tamaño de memoria" not in str(exc):
                    raise
                self.vm = TramoyaVM32.from_snapshot_bytes(raw)
            self.assembly = None
            self.demo = "snapshot"
            self.revision += 1
            return {
                "ok": True,
                "message": "Snapshot restaurado",
                "listing": [],
                "state": self._state_unlocked(),
            }

    def _resolve(self, value: Any) -> int:
        if value is None or value == "":
            raise ValueError("Falta una dirección o etiqueta")
        text = str(value).strip()
        program = self.vm.program
        symbols = program.symbols if program else {}
        if text.upper() in symbols:
            return int(symbols[text.upper()])
        return _integer(text, "Dirección", minimum=0)

    def _resolve_many(self, values: Any) -> set[int]:
        if values in (None, ""):
            return set()
        if isinstance(values, str):
            values = values.replace(",", " ").split()
        if not isinstance(values, list):
            raise ValueError("Los breakpoints deben ser una lista")
        return {self._resolve(item) for item in values if str(item).strip()}

    def _listing_unlocked(self) -> list[dict[str, Any]]:
        if self.assembly is None:
            return []
        return [
            {
                "line": item.line,
                "address": item.address,
                "words": [word & 0xFFFFFFFF for word in item.words],
                "source": item.source,
            }
            for item in self.assembly.listing
        ]

    def _state_unlocked(self, memory_start: int = 0, memory_count: int = 64) -> dict[str, Any]:
        result = self.vm.result()
        config = self.vm.config
        program = self.vm.program
        memory_start = max(0, min(int(memory_start), config.memory_words - 1))
        memory_count = max(1, min(int(memory_count), 256, config.memory_words - memory_start))
        memory = self.vm.memory_slice(memory_start, memory_count)
        ctx = self.vm.machine.ctx
        output_text = "".join(value if isinstance(value, str) else f"{value}\n" for value in result.output)
        return {
            "revision": self.revision,
            "state": result.state,
            "pc": self.vm.pc,
            "registers": list(self.vm.registers),
            "flags": dict(self.vm.flags),
            "stack": list(reversed(self.vm.stack[-128:])),
            "output": list(result.output),
            "output_text": output_text,
            "trace": [
                {
                    "sequence": entry.sequence,
                    "pc": entry.pc,
                    "opcode": entry.opcode,
                    "operands": list(entry.operands),
                    "cycles": entry.cycles,
                    "gas_remaining": entry.gas_remaining,
                    "detail": entry.detail,
                }
                for entry in self.vm.trace[-160:]
            ],
            "memory": {
                "start": memory_start,
                "count": memory_count,
                "values": list(memory),
            },
            "program": None if program is None else {
                "name": program.source_name,
                "entry": program.entry,
                "code_size": program.code_size,
                "data_size": program.data_size,
                "words": len(program.words),
                "symbols": dict(program.symbols),
            },
            "resources": {
                "instructions": result.instructions,
                "cycles": result.cycles,
                "gas_remaining": result.gas_remaining,
                "gas_limit": config.gas_limit,
                "memory_words": config.memory_words,
                "memory_allocated_words": self.vm.allocated_memory_words,
                "memory_allocated_bytes": self.vm.allocated_memory_bytes,
                "memory_allocated_pages": self.vm.allocated_memory_pages,
                "stack_limit": config.stack_limit,
                "trace_size": config.trace_size,
                "output_limit": config.output_limit,
                "protect_code": config.protect_code,
                "heap_pointer": self.vm._heap_ptr,
                "input_depth": len(self.vm._input),
                "output_units": self.vm._output_units,
                "pending_interrupts": list(self.vm._pending_interrupts),
                "interrupt_depth": len(self.vm._interrupt_stack),
                "interrupts_enabled": self.vm._interrupts_enabled,
                "interrupt_vectors": dict(self.vm._interrupt_vectors),
            },
            "reason": result.fault or result.wait_reason or result.pause_reason,
            "exit_code": result.exit_code,
            "last_instruction": ctx.get("last_instruction"),
            "last_transition": ctx.get("last_transition"),
            "controls": list(self.vm.available_controls()),
        }


class ConsoleHandler(BaseHTTPRequestHandler):
    """HTTP JSON + archivos estáticos, deliberadamente limitado a la consola."""

    server_version = "TramoyaConsole/2.0"

    @property
    def session(self) -> ConsoleSession:
        return self.server.session  # type: ignore[attr-defined]

    def _headers(self, status: int, content_type: str, length: int, *, disposition: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        )
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()

    def _send_json(self, payload: Mapping[str, Any], status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(raw))
        self.wfile.write(raw)

    def _read_body(self, maximum: int) -> bytes:
        length = _integer(self.headers.get("Content-Length", "0"), "Content-Length", minimum=0, maximum=maximum)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("Cuerpo HTTP truncado")
        return raw

    def _read_json(self) -> Mapping[str, Any]:
        raw = self._read_body(MAX_JSON_BYTES)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("JSON inválido") from exc
        if not isinstance(payload, dict):
            raise ValueError("El cuerpo JSON debe ser un objeto")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/bootstrap":
                query = parse_qs(parsed.query)
                start = _integer(query.get("memory_start", [0])[0], "Inicio", minimum=0)
                count = _integer(query.get("memory_count", [64])[0], "Cantidad", minimum=1, maximum=256)
                self._send_json(self.session.bootstrap(start, count))
            elif parsed.path == "/api/state":
                query = parse_qs(parsed.query)
                start = _integer(query.get("memory_start", [0])[0], "Inicio", minimum=0)
                count = _integer(query.get("memory_count", [64])[0], "Cantidad", minimum=1, maximum=256)
                self._send_json(self.session.state(start, count))
            elif parsed.path == "/api/bytecode":
                self._send_download(self.session.bytecode(), "application/octet-stream", "programa.tvm")
            elif parsed.path == "/api/snapshot":
                self._send_download(self.session.snapshot_bytes(), "application/octet-stream", "tramoya-vm32.tvms")
            elif parsed.path == "/health":
                self._send_json({"ok": True, "service": "tramoya-vm32-console"})
            else:
                self._send_static(parsed.path)
        except Exception as exc:  # la frontera HTTP convierte fallos controlados en JSON
            self._send_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/load":
                self._send_json(self.session.load(self._read_json()))
            elif parsed.path == "/api/action":
                self._send_json(self.session.action(self._read_json()))
            elif parsed.path == "/api/restore":
                self._send_json(self.session.restore(self._read_body(MAX_SNAPSHOT_BYTES)))
            else:
                self._send_json({"ok": False, "error": "Ruta no encontrada"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_error(exc)

    def _send_download(self, raw: bytes, content_type: str, filename: str) -> None:
        self._headers(
            HTTPStatus.OK,
            content_type,
            len(raw),
            disposition=f'attachment; filename="{filename}"',
        )
        self.wfile.write(raw)

    def _send_static(self, path: str) -> None:
        names = {
            "/": "index.html",
            "/index.html": "index.html",
            "/app.js": "app.js",
            "/styles.css": "styles.css",
            "/og.png": "og.png",
        }
        filename = names.get(path)
        if filename is None:
            self._send_json({"ok": False, "error": "Ruta no encontrada"}, HTTPStatus.NOT_FOUND)
            return
        raw = (UI_DIR / filename).read_bytes()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._headers(HTTPStatus.OK, content_type, len(raw))
        self.wfile.write(raw)

    def _send_error(self, exc: Exception) -> None:
        expected = (ValueError, VMAssemblyError, VMRuntimeError, MachineError, OSError)
        status = HTTPStatus.BAD_REQUEST if isinstance(exc, expected) else HTTPStatus.INTERNAL_SERVER_ERROR
        self._send_json({"ok": False, "error": str(exc) or type(exc).__name__}, status)

    def log_message(self, format: str, *args: object) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)


class ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], *, verbose: bool = False):
        super().__init__(address, ConsoleHandler)
        self.session = ConsoleSession()
        self.verbose = verbose


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consola web local de Tramoya VM32")
    parser.add_argument("--host", default="127.0.0.1", help="Interfaz de escucha (por defecto solo local)")
    parser.add_argument("--port", type=int, default=8765, help="Puerto local")
    parser.add_argument("--no-open", action="store_true", help="No abrir el navegador automáticamente")
    parser.add_argument("--verbose", action="store_true", help="Mostrar peticiones HTTP")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 <= args.port <= 65_535:
        raise SystemExit("El puerto debe estar entre 0 y 65535")
    server = ConsoleHTTPServer((args.host, args.port), verbose=args.verbose)
    host, port = server.server_address[:2]
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{display_host}:{port}/"
    print(f"Tramoya VM32 Console · {url}")
    print("Ctrl+C para detener")
    if not args.no_open:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nConsola detenida")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
